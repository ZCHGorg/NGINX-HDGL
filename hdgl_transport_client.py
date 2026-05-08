#!/usr/bin/env python3
"""
hdgl_transport_client.py
────────────────────────
Client-side HDGL frame sender for peer-to-peer communication.

Used by hdgl_host.py to send frames to remote peers:
  - gossip announcements (HDGL_MSG_GOSSIP)
  - node info queries (HDGL_MSG_INFO)
  - content fetches (HDGL_MSG_FETCH)
  - replication / echo propagation (HDGL_MSG_REPLICATE)
  - metrics requests (HDGL_MSG_METRICS)
  - health checks (HDGL_MSG_HEALTH)

Usage:
    from hdgl_transport_client import HDGLTransportClient

    client = HDGLTransportClient(local_node="10.0.0.1")
    response = client.send_gossip(peer_ip="10.0.0.2", gossip_data={...})
    if response:
        print(response)

Architecture:
  - Connection pooling per remote peer (reuse sockets for multiple frames)
  - Timeout handling and retry logic
  - Frame serialization and HMAC signing
  - Response deserialization and error handling
"""

import json
import logging
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from hdgl_transport import (
    HDGLFrame,
    HDGL_MSG_GOSSIP,
    HDGL_MSG_INFO,
    HDGL_MSG_FETCH,
    HDGL_MSG_REPLICATE,
    HDGL_MSG_METRICS,
    HDGL_MSG_HEALTH,
    TRANSPORT_PORT,
    FRAME_TIMEOUT,
)

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CLIENT CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

LOCAL_NODE = os.getenv("LN_LOCAL_NODE", "127.0.0.1")
CONNECT_TIMEOUT = float(os.getenv("LN_FRAME_TIMEOUT", "10.0"))
RETRY_MAX_ATTEMPTS = 3
RETRY_DELAY_MS = 100


# ─────────────────────────────────────────────────────────────────────────────
# CONNECTION CACHE
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class CachedSocket:
    """Cached outbound connection to a peer."""

    peer_ip: str
    sock: socket.socket
    created_at: float
    last_used_at: float = None

    def is_alive(self) -> bool:
        """Check if socket is still connected."""
        if not self.sock:
            return False
        try:
            self.sock.settimeout(0.5)
            self.sock.recv(0)  # peek — returns 0 if alive, raises if dead
            self.sock.settimeout(None)
            return True
        except:
            return False

    def close(self):
        """Close socket."""
        try:
            self.sock.close()
        except:
            pass


class HDGLTransportClient:
    """
    HDGL frame sender for peer-to-peer communication.

    Maintains connection cache, handles retries, and manages frame serialization.
    """

    def __init__(self, local_node: str = LOCAL_NODE):
        self.local_node = local_node
        self.peer_cache: Dict[str, CachedSocket] = {}
        self.cache_lock = threading.Lock()

    def send_gossip(
        self, peer_ip: str, gossip_data: Dict[str, Any], strand_id: int = 0
    ) -> Optional[Dict[str, Any]]:
        """Send HDGL_MSG_GOSSIP frame to peer and return response."""
        try:
            frame = HDGLFrame(
                frame_type=HDGL_MSG_GOSSIP,
                strand_id=strand_id,
                source_ip=self.local_node,
                payload=json.dumps(gossip_data).encode(),
            )
            response = self._send_frame(peer_ip, frame)
            if response:
                return json.loads(response.payload.decode())
            return None
        except Exception as e:
            log.warning(f"[client] gossip to {peer_ip} failed: {e}")
            return None

    def send_info_query(self, peer_ip: str, strand_id: int = 0) -> Optional[Dict[str, Any]]:
        """Send HDGL_MSG_INFO frame (query node state) and return response."""
        try:
            frame = HDGLFrame(
                frame_type=HDGL_MSG_INFO,
                strand_id=strand_id,
                source_ip=self.local_node,
                payload=b"",
            )
            response = self._send_frame(peer_ip, frame)
            if response:
                try:
                    return json.loads(response.payload.decode())
                except:
                    return {"status": "ok"}
            return None
        except Exception as e:
            log.warning(f"[client] info query to {peer_ip} failed: {e}")
            return None

    def send_fetch(
        self, peer_ip: str, path: str, strand_id: int = 0
    ) -> Optional[bytes]:
        """Send HDGL_MSG_FETCH frame (read content) and return data."""
        try:
            frame = HDGLFrame(
                frame_type=HDGL_MSG_FETCH,
                strand_id=strand_id,
                source_ip=self.local_node,
                payload=json.dumps({"path": path}).encode(),
            )
            response = self._send_frame(peer_ip, frame)
            if response:
                return response.payload
            return None
        except Exception as e:
            log.warning(f"[client] fetch {path} from {peer_ip} failed: {e}")
            return None

    def send_replicate(
        self, peer_ip: str, path: str, data: bytes, strand_id: int = 0
    ) -> bool:
        """Send HDGL_MSG_REPLICATE frame (propagate echo/replica)."""
        try:
            payload = json.dumps(
                {"path": path, "data": data.hex()}
            ).encode()
            frame = HDGLFrame(
                frame_type=HDGL_MSG_REPLICATE,
                strand_id=strand_id,
                source_ip=self.local_node,
                payload=payload,
            )
            response = self._send_frame(peer_ip, frame)
            return response is not None
        except Exception as e:
            log.warning(f"[client] replicate {path} to {peer_ip} failed: {e}")
            return False

    def send_metrics(self, peer_ip: str, strand_id: int = 0) -> Optional[Dict[str, Any]]:
        """Send HDGL_MSG_METRICS frame (request stats) and return metrics."""
        try:
            frame = HDGLFrame(
                frame_type=HDGL_MSG_METRICS,
                strand_id=strand_id,
                source_ip=self.local_node,
                payload=b"",
            )
            response = self._send_frame(peer_ip, frame)
            if response:
                return json.loads(response.payload.decode())
            return None
        except Exception as e:
            log.warning(f"[client] metrics from {peer_ip} failed: {e}")
            return None

    def send_health_probe(self, peer_ip: str, strand_id: int = 0) -> bool:
        """Send HDGL_MSG_HEALTH frame (liveness check)."""
        try:
            frame = HDGLFrame(
                frame_type=HDGL_MSG_HEALTH,
                strand_id=strand_id,
                source_ip=self.local_node,
                payload=b"ping",
            )
            response = self._send_frame(peer_ip, frame, timeout=3.0)
            return response is not None
        except Exception:
            return False

    def _send_frame(
        self, peer_ip: str, frame: HDGLFrame, timeout: float = FRAME_TIMEOUT
    ) -> Optional[HDGLFrame]:
        """
        Send a frame and receive response with retries.

        Returns response frame or None if all retries exhausted.
        """
        for attempt in range(RETRY_MAX_ATTEMPTS):
            try:
                sock = self._get_socket(peer_ip, timeout)
                if not sock:
                    continue

                # Send frame
                sock.settimeout(timeout)
                sock.sendall(frame.serialize())

                # Receive response size
                size_bytes = sock.recv(4)
                if not size_bytes:
                    self._invalidate_socket(peer_ip)
                    if attempt < RETRY_MAX_ATTEMPTS - 1:
                        time.sleep(RETRY_DELAY_MS / 1000.0)
                    continue

                frame_size = struct.unpack("!I", size_bytes)[0]
                if frame_size > 1024 * 1024:
                    log.warning(f"[client] frame too large from {peer_ip}: {frame_size}")
                    self._invalidate_socket(peer_ip)
                    continue

                # Receive frame body
                frame_body = b""
                while len(frame_body) < frame_size:
                    chunk = sock.recv(min(4096, frame_size - len(frame_body)))
                    if not chunk:
                        break
                    frame_body += chunk

                if len(frame_body) < frame_size:
                    log.warning(f"[client] incomplete frame from {peer_ip}")
                    self._invalidate_socket(peer_ip)
                    continue

                # Parse response
                response = HDGLFrame.deserialize(size_bytes + frame_body)
                return response

            except socket.timeout:
                log.debug(f"[client] timeout sending to {peer_ip} (attempt {attempt + 1})")
                self._invalidate_socket(peer_ip)
            except Exception as e:
                log.debug(f"[client] error sending to {peer_ip}: {e}")
                self._invalidate_socket(peer_ip)

            if attempt < RETRY_MAX_ATTEMPTS - 1:
                time.sleep(RETRY_DELAY_MS / 1000.0)

        log.warning(
            f"[client] exhausted {RETRY_MAX_ATTEMPTS} retries sending to {peer_ip}"
        )
        return None

    def _get_socket(
        self, peer_ip: str, timeout: float = CONNECT_TIMEOUT
    ) -> Optional[socket.socket]:
        """Get cached socket or create new one."""
        with self.cache_lock:
            # Try cache
            if peer_ip in self.peer_cache:
                cached = self.peer_cache[peer_ip]
                if cached.is_alive():
                    cached.last_used_at = time.time()
                    return cached.sock

                # Socket died, remove from cache
                cached.close()
                del self.peer_cache[peer_ip]

            # Create new connection
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                sock.connect((peer_ip, TRANSPORT_PORT))
                sock.settimeout(None)

                cached = CachedSocket(
                    peer_ip=peer_ip,
                    sock=sock,
                    created_at=time.time(),
                    last_used_at=time.time(),
                )
                self.peer_cache[peer_ip] = cached
                return sock
            except Exception as e:
                log.debug(f"[client] failed to connect to {peer_ip}: {e}")
                return None

    def _invalidate_socket(self, peer_ip: str):
        """Remove socket from cache and close it."""
        with self.cache_lock:
            if peer_ip in self.peer_cache:
                cached = self.peer_cache[peer_ip]
                cached.close()
                del self.peer_cache[peer_ip]

    def close_all(self):
        """Close all cached sockets."""
        with self.cache_lock:
            for cached in self.peer_cache.values():
                cached.close()
            self.peer_cache.clear()
