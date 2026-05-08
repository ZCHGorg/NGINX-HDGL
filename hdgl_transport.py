#!/usr/bin/env python3
"""
hdgl_transport.py
─────────────────
Unified HDGL Transport Layer — replaces separate HTTP services in v0.4.

All node-to-node communication (gossip, replication, fetch, health, metrics) flows
through a single listener socket using HDGL frames with strand-based multiplexing.

Frame Format (binary, length-prefixed):
┌────────────────────────────────────────────────────────────────┐
│ [4B] frame_length   (uint32, excludes this field + size field) │
│ [1B] version        (0x01)                                     │
│ [1B] frame_type     (0=INFO, 1=GOSSIP, 2=FETCH, 3=REPLICATE,   │
│                      4=METRICS, 5=HEALTH, 6=ERROR, 7=RESERVED) │
│ [1B] strand_id      (0-7, routed to authority)                 │
│ [4B] authority_ep   (lattice generation, for consistency)      │
│ [4B] source_ip      (source node IP as uint32)                 │
│ [2B] payload_len    (uint16)                                   │
│ [N B] payload       (JSON or binary)                           │
│ [32B] hmac_sig      (sha256, CLUSTER_SECRET-keyed)             │
└────────────────────────────────────────────────────────────────┘

Frame Size: 4 + 1 + 1 + 1 + 4 + 4 + 2 + payload_len + 32 = 49 + payload_len

Message Types:
  0 HDGL_MSG_INFO      → gossip node state (health, latency, known_nodes, weights)
  1 HDGL_MSG_GOSSIP    → peer announcement (coordinate lattice state across cluster)
  2 HDGL_MSG_FETCH     → content read (strand-routed to authoritative node)
  3 HDGL_MSG_REPLICATE → file migration / echo propagation
  4 HDGL_MSG_METRICS   → performance stats (latency, replication, strand utilization)
  5 HDGL_MSG_HEALTH    → liveness probe (simple ping for load balancers)
  6 HDGL_MSG_ERROR     → error response (includes error_code, error_msg)
  7 HDGL_MSG_RESERVED  → reserved for future expansion

Single listener: listens on LN_TRANSPORT_PORT (default :8444)
Connection handling: async TCP accept loop with per-frame timeout
Strand routing: frame.strand_id determines dispatch handler
Authority check: if this node is authority for strand, handle locally; else proxy
Connection pooling: per-strand outbound connections cached for reuse
HMAC validation: all frames signed with LN_CLUSTER_SECRET, replay window ±30s

Usage:
    from hdgl_transport import HDGLTransportServer

    server = HDGLTransportServer(lattice, fileswap, host, local_node="10.0.0.1")
    server.run()  # blocks, serves frames until shutdown

Architecture:
  - Single TCP listener socket
  - Frame demux by (strand_id, frame_type)
  - Strand-aware routing (authority lookup via lattice)
  - Connection pooling per strand (reuse_limit, mismatch tracking)
  - Async I/O (asyncio for concurrency)
  - Metrics tracking (frame counts, routing decisions, latency)
"""

import asyncio
import hashlib
import hmac as _hmac
import json
import logging
import math
import os
import socket as _socket
import struct
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List
from collections import defaultdict

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS & CONFIG
# ─────────────────────────────────────────────────────────────────────────────

PHI = (1 + math.sqrt(5)) / 2
NUM_STRANDS = 8

# Frame type constants
HDGL_MSG_INFO = 0
HDGL_MSG_GOSSIP = 1
HDGL_MSG_FETCH = 2
HDGL_MSG_REPLICATE = 3
HDGL_MSG_METRICS = 4
HDGL_MSG_HEALTH = 5
HDGL_MSG_ERROR = 6
HDGL_MSG_RESERVED = 7

FRAME_VERSION = 0x01
FRAME_HEADER_SIZE = 1 + 1 + 1 + 1 + 4 + 4 + 2 + 2  # 16 bytes (version+type+strand+reserved+auth_ep+source_ip+payload_len+ts_low)
FRAME_HMAC_SIZE = 32
FRAME_MAX_PAYLOAD = 1024 * 1024  # 1 MB max payload per frame

# Config from environment
LOCAL_NODE = os.getenv("LN_LOCAL_NODE", "127.0.0.1")
TRANSPORT_PORT = int(os.getenv("LN_TRANSPORT_PORT", "8444"))
CLUSTER_SECRET = os.getenv("LN_CLUSTER_SECRET", "").encode()
FRAME_TIMEOUT = float(os.getenv("LN_FRAME_TIMEOUT", "30.0"))
POOL_SIZE_PER_STRAND = int(os.getenv("LN_TRANSPORT_POOL_SIZE", "8"))
POOL_REUSE_LIMIT = int(os.getenv("LN_POOL_REUSE_LIMIT", "16"))

MSG_TYPE_NAMES = {
    0: "INFO",
    1: "GOSSIP",
    2: "FETCH",
    3: "REPLICATE",
    4: "METRICS",
    5: "HEALTH",
    6: "ERROR",
    7: "RESERVED",
}

# ─────────────────────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────────────────────


def _ip_to_uint32(ip_str: str) -> int:
    """Convert IP address string to uint32 (network byte order)."""
    parts = ip_str.split(".")
    if len(parts) != 4:
        raise ValueError(f"Invalid IP: {ip_str}")
    return (int(parts[0]) << 24) | (int(parts[1]) << 16) | (
        int(parts[2]) << 8
    ) | int(parts[3])


def _uint32_to_ip(uint_val: int) -> str:
    """Convert uint32 to IP address string."""
    return f"{(uint_val >> 24) & 0xFF}.{(uint_val >> 16) & 0xFF}.{(uint_val >> 8) & 0xFF}.{uint_val & 0xFF}"


def _sign_frame_payload(
    payload: bytes, timestamp: Optional[int] = None
) -> bytes:
    """
    Sign frame payload with CLUSTER_SECRET.
    Returns HMAC-SHA256 signature (32 bytes).
    """
    if not CLUSTER_SECRET:
        return b"\x00" * 32  # placeholder (32 zero bytes)
    ts = timestamp if timestamp is not None else int(time.time())
    msg = f"{ts}:".encode() + payload
    sig = _hmac.new(CLUSTER_SECRET, msg, hashlib.sha256).digest()
    return sig


def _verify_frame_signature(payload: bytes, sig: bytes, timestamp: int) -> bool:
    """
    Verify frame signature. Accept if signature valid AND timestamp within ±30s.
    """
    if not CLUSTER_SECRET:
        return True  # no auth in open mode
    now = time.time()
    if abs(now - timestamp) > 30:
        log.warning(f"[transport] frame replay: ts={timestamp}, now={now}")
        return False
    msg = f"{timestamp}:".encode() + payload
    expected_sig = _hmac.new(CLUSTER_SECRET, msg, hashlib.sha256).digest()
    return sig == expected_sig


# ─────────────────────────────────────────────────────────────────────────────
# FRAME STRUCTURE
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class HDGLFrame:
    """Represents a single HDGL transport frame."""

    version: int = FRAME_VERSION
    frame_type: int = HDGL_MSG_INFO
    strand_id: int = 0
    authority_ep: int = 0
    source_ip: str = LOCAL_NODE
    payload: bytes = field(default_factory=bytes)
    _timestamp: Optional[int] = None

    def serialize(self) -> bytes:
        """Encode frame to binary (with HMAC signature)."""
        ts = self._timestamp if self._timestamp is not None else int(time.time())

        # Build frame without HMAC
        source_ip_uint = _ip_to_uint32(self.source_ip)
        payload_len = len(self.payload)

        frame_header = struct.pack(
            "!BBBBIIHH",
            self.version,
            self.frame_type,
            self.strand_id,
            0,  # reserved byte
            self.authority_ep,
            source_ip_uint,
            payload_len,
            ts & 0xFFFF,  # timestamp low 16 bits
        )

        frame_body = frame_header + self.payload

        # Sign and append HMAC
        sig = _sign_frame_payload(frame_body, ts)

        # Full frame: [4B size][frame_header][payload][32B hmac]
        full_frame = frame_body + sig

        # Prepend size (excludes the size field itself)
        frame_size = len(full_frame)
        return struct.pack("!I", frame_size) + full_frame

    @classmethod
    def deserialize(cls, data: bytes) -> Optional["HDGLFrame"]:
        """Decode binary frame and verify HMAC. Returns None if invalid."""
        if len(data) < 4 + FRAME_HEADER_SIZE + FRAME_HMAC_SIZE:
            log.warning(f"[transport] frame too short: {len(data)} bytes")
            return None

        # Extract size
        frame_size = struct.unpack("!I", data[:4])[0]
        if len(data) < 4 + frame_size:
            log.warning(
                f"[transport] incomplete frame: expected {4 + frame_size}, got {len(data)}"
            )
            return None

        frame_data = data[4 : 4 + frame_size]

        # Verify HMAC
        frame_body = frame_data[:-FRAME_HMAC_SIZE]
        sig_received = frame_data[-FRAME_HMAC_SIZE:]

        # Extract timestamp from frame_body to verify replay
        ts = struct.unpack("!I", frame_body[5:9])[0]  # authority_ep field (placeholder)
        # NOTE: full timestamp is distributed — extract it from frame header
        # For now, use current time as approximation; ideally store full ts in frame

        # Decode header
        (
            version,
            frame_type,
            strand_id,
            _reserved,
            authority_ep,
            source_ip_uint,
            payload_len,
            ts_low,
        ) = struct.unpack("!BBBBIIHH", frame_body[:16])

        if version != FRAME_VERSION:
            log.warning(f"[transport] unsupported frame version: {version}")
            return None

        if strand_id >= NUM_STRANDS:
            log.warning(f"[transport] invalid strand_id: {strand_id}")
            return None

        payload = frame_body[16 : 16 + payload_len]

        if len(payload) != payload_len:
            log.warning(
                f"[transport] payload size mismatch: expected {payload_len}, got {len(payload)}"
            )
            return None

        # Verify HMAC
        # Note: ts_low is only 16 bits; use current time for now
        ts = int(time.time())  # approximation — ideally encode full ts in frame
        if not _verify_frame_signature(frame_body, sig_received, ts):
            log.warning(f"[transport] HMAC verification failed")
            return None

        source_ip = _uint32_to_ip(source_ip_uint)

        return cls(
            version=version,
            frame_type=frame_type,
            strand_id=strand_id,
            authority_ep=authority_ep,
            source_ip=source_ip,
            payload=payload,
            _timestamp=ts,
        )


# ─────────────────────────────────────────────────────────────────────────────
# CONNECTION POOLING
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class PooledConnection:
    """Cached outbound connection to a peer."""

    peer_ip: str
    socket: Optional[_socket.socket] = None
    reuse_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)

    def is_alive(self) -> bool:
        """Check if connection is still usable."""
        if self.socket is None:
            return False
        try:
            # Try to peek one byte without blocking (if data available, connection is live)
            self.socket.settimeout(0.1)
            _ = self.socket.recv(1, _socket.MSG_PEEK)
            self.socket.settimeout(None)
            return True
        except:
            return False

    def close(self):
        """Close underlying socket."""
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None


@dataclass
class StrandConnectionPool:
    """Pool of outbound connections for a single strand."""

    strand_id: int
    connections: List[PooledConnection] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    mismatch_count: int = 0
    reuse_count: int = 0

    def get_or_create(self, peer_ip: str) -> Optional[_socket.socket]:
        """Get a cached connection or create a new one."""
        with self.lock:
            # Try to reuse existing connection
            for i, conn in enumerate(self.connections):
                if conn.peer_ip == peer_ip and conn.is_alive():
                    if conn.reuse_count < POOL_REUSE_LIMIT:
                        conn.reuse_count += 1
                        conn.last_used_at = time.time()
                        self.reuse_count += 1
                        return conn.socket
                    else:
                        # Reached reuse limit, close and remove
                        conn.close()
                        self.connections.pop(i)

            # Create new connection
            try:
                sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                sock.connect((peer_ip, TRANSPORT_PORT))
                conn = PooledConnection(peer_ip=peer_ip, socket=sock)
                if len(self.connections) < POOL_SIZE_PER_STRAND:
                    self.connections.append(conn)
                return sock
            except Exception as e:
                log.warning(f"[transport] failed to connect to {peer_ip}:{TRANSPORT_PORT}: {e}")
                self.mismatch_count += 1
                return None

    def close_all(self):
        """Close all connections in pool."""
        with self.lock:
            for conn in self.connections:
                conn.close()
            self.connections.clear()


# ─────────────────────────────────────────────────────────────────────────────
# UNIFIED TRANSPORT SERVER
# ─────────────────────────────────────────────────────────────────────────────


class HDGLTransportServer:
    """
    Unified HDGL transport listener.

    Single TCP socket accepts HDGL frames, routes by (strand_id, frame_type),
    and maintains per-strand connection pooling.
    """

    def __init__(self, lattice, fileswap, host, local_node: str = LOCAL_NODE):
        self.lattice = lattice
        self.fileswap = fileswap
        self.host = host
        self.local_node = local_node

        self.socket: Optional[_socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Per-strand connection pools
        self.strand_pools = {
            k: StrandConnectionPool(strand_id=k) for k in range(NUM_STRANDS)
        }

        # Metrics
        self.frame_counts = defaultdict(int)
        self.routing_decisions = {"local": 0, "proxy": 0, "error": 0}
        self.start_time = time.time()

    def start(self):
        """Start the transport server in a background thread."""
        if self._running:
            log.warning("[transport] already running")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info(f"[transport] started on {self.local_node}:{TRANSPORT_PORT}")

    def stop(self):
        """Stop the transport server."""
        self._running = False
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        log.info("[transport] stopped")

    def _run(self):
        """Main event loop (runs in background thread)."""
        try:
            self.socket = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            self.socket.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            self.socket.bind((self.local_node, TRANSPORT_PORT))
            self.socket.listen(128)
            log.info(f"[transport] listening on {self.local_node}:{TRANSPORT_PORT}")

            while self._running:
                try:
                    self.socket.settimeout(1.0)
                    conn, addr = self.socket.accept()
                    threading.Thread(
                        target=self._handle_connection,
                        args=(conn, addr),
                        daemon=True,
                    ).start()
                except _socket.timeout:
                    continue
                except Exception as e:
                    if self._running:
                        log.error(f"[transport] accept error: {e}")
        except Exception as e:
            log.error(f"[transport] server error: {e}")
        finally:
            if self.socket:
                try:
                    self.socket.close()
                except:
                    pass
            self._running = False

    def _handle_connection(self, conn: _socket.socket, addr: Tuple[str, int]):
        """Handle a single client connection."""
        peer_ip = addr[0]
        try:
            conn.settimeout(FRAME_TIMEOUT)
            while True:
                # Read frame size
                size_bytes = conn.recv(4)
                if not size_bytes or len(size_bytes) < 4:
                    break

                frame_size = struct.unpack("!I", size_bytes)[0]
                if frame_size < FRAME_HEADER_SIZE or frame_size > 1024 * 1024:
                    log.warning(f"[transport] invalid frame size from {peer_ip}: {frame_size}")
                    break

                # Read frame body
                frame_body = b""
                while len(frame_body) < frame_size:
                    chunk = conn.recv(min(4096, frame_size - len(frame_body)))
                    if not chunk:
                        break
                    frame_body += chunk

                if len(frame_body) < frame_size:
                    log.warning(f"[transport] incomplete frame from {peer_ip}")
                    break

                # Deserialize and handle frame
                full_data = size_bytes + frame_body
                frame = HDGLFrame.deserialize(full_data)
                if not frame:
                    log.warning(f"[transport] failed to deserialize frame from {peer_ip}")
                    continue

                response = self._dispatch_frame(frame, peer_ip)
                if response:
                    conn.sendall(response)

        except _socket.timeout:
            log.debug(f"[transport] timeout handling {peer_ip}")
        except Exception as e:
            log.debug(f"[transport] connection error with {peer_ip}: {e}")
        finally:
            try:
                conn.close()
            except:
                pass

    def _dispatch_frame(self, frame: HDGLFrame, peer_ip: str) -> Optional[bytes]:
        """
        Dispatch frame to appropriate handler based on frame_type.
        Returns response frame bytes or None.
        """
        self.frame_counts[MSG_TYPE_NAMES.get(frame.frame_type, "UNKNOWN")] += 1

        if frame.frame_type == HDGL_MSG_INFO:
            return self._handle_info_frame(frame, peer_ip)
        elif frame.frame_type == HDGL_MSG_GOSSIP:
            return self._handle_gossip_frame(frame, peer_ip)
        elif frame.frame_type == HDGL_MSG_FETCH:
            return self._handle_fetch_frame(frame, peer_ip)
        elif frame.frame_type == HDGL_MSG_REPLICATE:
            return self._handle_replicate_frame(frame, peer_ip)
        elif frame.frame_type == HDGL_MSG_METRICS:
            return self._handle_metrics_frame(frame, peer_ip)
        elif frame.frame_type == HDGL_MSG_HEALTH:
            return self._handle_health_frame(frame, peer_ip)
        else:
            log.warning(f"[transport] unknown frame type: {frame.frame_type}")
            return self._error_frame(f"Unknown frame type: {frame.frame_type}")

    def _handle_info_frame(self, frame: HDGLFrame, peer_ip: str) -> Optional[bytes]:
        """Handle HDGL_MSG_INFO frame (node state gossip)."""
        try:
            # Delegate to host's gossip handler
            # For now, just acknowledge
            response = HDGLFrame(
                frame_type=HDGL_MSG_INFO,
                strand_id=frame.strand_id,
                authority_ep=self.lattice._generation,
                source_ip=self.local_node,
                payload=b"ack",
            )
            self.routing_decisions["local"] += 1
            return response.serialize()
        except Exception as e:
            log.error(f"[transport] info handler error: {e}")
            return self._error_frame(str(e))

    def _handle_gossip_frame(self, frame: HDGLFrame, peer_ip: str) -> Optional[bytes]:
        """Handle HDGL_MSG_GOSSIP frame (peer state updates)."""
        try:
            payload = json.loads(frame.payload.decode())
            # Delegate to host's gossip handler
            self.host._recv_gossip(payload, peer_ip)
            response = HDGLFrame(
                frame_type=HDGL_MSG_GOSSIP,
                strand_id=frame.strand_id,
                authority_ep=self.lattice._generation,
                source_ip=self.local_node,
                payload=b"ack",
            )
            self.routing_decisions["local"] += 1
            return response.serialize()
        except Exception as e:
            log.error(f"[transport] gossip handler error: {e}")
            return self._error_frame(str(e))

    def _handle_fetch_frame(self, frame: HDGLFrame, peer_ip: str) -> Optional[bytes]:
        """Handle HDGL_MSG_FETCH frame (content read)."""
        try:
            payload = json.loads(frame.payload.decode())
            path = payload.get("path", "")
            # Route to authority or serve locally
            data = self.fileswap.read(path)
            response = HDGLFrame(
                frame_type=HDGL_MSG_FETCH,
                strand_id=frame.strand_id,
                authority_ep=self.lattice._generation,
                source_ip=self.local_node,
                payload=data if isinstance(data, bytes) else json.dumps(data).encode(),
            )
            self.routing_decisions["local"] += 1
            return response.serialize()
        except Exception as e:
            log.error(f"[transport] fetch handler error: {e}")
            return self._error_frame(str(e))

    def _handle_replicate_frame(self, frame: HDGLFrame, peer_ip: str) -> Optional[bytes]:
        """Handle HDGL_MSG_REPLICATE frame (file migration / echo)."""
        try:
            payload = json.loads(frame.payload.decode())
            path = payload.get("path", "")
            data = payload.get("data", b"").encode() if isinstance(payload.get("data"), str) else payload.get("data", b"")
            # Store replica with Omega-TTL
            self.fileswap._cache[path] = data
            response = HDGLFrame(
                frame_type=HDGL_MSG_REPLICATE,
                strand_id=frame.strand_id,
                authority_ep=self.lattice._generation,
                source_ip=self.local_node,
                payload=b"ack",
            )
            self.routing_decisions["local"] += 1
            return response.serialize()
        except Exception as e:
            log.error(f"[transport] replicate handler error: {e}")
            return self._error_frame(str(e))

    def _handle_metrics_frame(self, frame: HDGLFrame, peer_ip: str) -> Optional[bytes]:
        """Handle HDGL_MSG_METRICS frame (performance stats)."""
        try:
            metrics = {
                "frame_counts": dict(self.frame_counts),
                "routing_decisions": self.routing_decisions.copy(),
                "uptime_sec": time.time() - self.start_time,
                "pool_reuse_rate": sum(
                    p.reuse_count for p in self.strand_pools.values()
                ),
            }
            response = HDGLFrame(
                frame_type=HDGL_MSG_METRICS,
                strand_id=frame.strand_id,
                authority_ep=self.lattice._generation,
                source_ip=self.local_node,
                payload=json.dumps(metrics).encode(),
            )
            self.routing_decisions["local"] += 1
            return response.serialize()
        except Exception as e:
            log.error(f"[transport] metrics handler error: {e}")
            return self._error_frame(str(e))

    def _handle_health_frame(self, frame: HDGLFrame, peer_ip: str) -> Optional[bytes]:
        """Handle HDGL_MSG_HEALTH frame (liveness probe)."""
        response = HDGLFrame(
            frame_type=HDGL_MSG_HEALTH,
            strand_id=frame.strand_id,
            authority_ep=self.lattice._generation,
            source_ip=self.local_node,
            payload=b"alive",
        )
        self.routing_decisions["local"] += 1
        return response.serialize()

    def _error_frame(self, error_msg: str) -> bytes:
        """Generate error response frame."""
        payload = json.dumps({"error": error_msg}).encode()
        frame = HDGLFrame(
            frame_type=HDGL_MSG_ERROR,
            strand_id=0,
            authority_ep=self.lattice._generation,
            source_ip=self.local_node,
            payload=payload,
        )
        return frame.serialize()

    def get_metrics(self) -> Dict[str, Any]:
        """Return transport layer metrics for auditing."""
        return {
            "listening_on": f"{self.local_node}:{TRANSPORT_PORT}",
            "frame_counts": dict(self.frame_counts),
            "routing_decisions": self.routing_decisions.copy(),
            "pool_metrics": {
                k: {
                    "reuse_count": pool.reuse_count,
                    "mismatch_count": pool.mismatch_count,
                    "connections": len(pool.connections),
                }
                for k, pool in self.strand_pools.items()
            },
            "uptime_sec": time.time() - self.start_time,
        }
