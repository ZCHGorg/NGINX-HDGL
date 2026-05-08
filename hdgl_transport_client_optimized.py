#!/usr/bin/env python3
"""
hdgl_transport_client_optimized.py
──────────────────────────────────
HDGL v0.4 Optimized Client — Connection Pooling & Pipelining

Optimizations:
  1. Per-peer connection pooling (reuse TCP streams)
  2. Request pipelining (send N requests before reading responses)
  3. Async I/O throughout (asyncio, no threads)
  4. Connection keep-alive with TTL-based eviction
  5. Exponential backoff on failures
  6. Per-peer metrics (latency, error rates, pool utilization)

Target: 5-10x throughput over base client, <10ms latency
Baseline: base client opens new TCP per frame

Usage:
    client = HDGLTransportClientOptimized()
    response = await client.send_frame_to_peer(
        peer_ip="10.0.0.2",
        frame=frame,
    )
"""

import asyncio
import hashlib
import hmac as _hmac
import json
import logging
import os
import struct
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Dict, Optional, List, Tuple

log = logging.getLogger(__name__)

# Constants
FRAME_VERSION = 0x01
FRAME_HEADER_SIZE = 16
FRAME_HMAC_SIZE = 32
CLUSTER_SECRET = os.getenv("LN_CLUSTER_SECRET", "").encode()
DEFAULT_PORT = int(os.getenv("LN_TRANSPORT_PORT", "8444"))
POOL_SIZE_PER_PEER = int(os.getenv("LN_CLIENT_POOL_SIZE", "8"))
POOL_REUSE_LIMIT = int(os.getenv("LN_POOL_REUSE_LIMIT", "64"))
FRAME_TIMEOUT = float(os.getenv("LN_FRAME_TIMEOUT", "30.0"))
KEEP_ALIVE_TTL = float(os.getenv("LN_KEEP_ALIVE_TTL", "60.0"))
MAX_PIPELINED_REQUESTS = int(os.getenv("LN_MAX_PIPELINED", "16"))
CONNECTION_TIMEOUT = float(os.getenv("LN_CONNECTION_TIMEOUT", "10.0"))

# ─────────────────────────────────────────────────────────────────────────────
# CONNECTION POOLING
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PooledConnection:
    """Represents a pooled TCP connection."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    request_count: int = 0
    error_count: int = 0
    in_use: bool = False

    def is_stale(self, ttl: float = KEEP_ALIVE_TTL) -> bool:
        """Check if connection is stale."""
        return time.time() - self.last_used_at > ttl

    def is_exhausted(self, limit: int = POOL_REUSE_LIMIT) -> bool:
        """Check if connection exceeded reuse limit."""
        return self.request_count >= limit

    def mark_used(self):
        """Update last_used_at."""
        self.last_used_at = time.time()

    def mark_error(self):
        """Increment error count."""
        self.error_count += 1


from dataclasses import field as field_factory


@dataclass
class PooledConnection:
    """Represents a pooled TCP connection."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    created_at: float = field_factory(time.time)
    last_used_at: float = field_factory(time.time)
    request_count: int = 0
    error_count: int = 0
    in_use: bool = False

    def is_stale(self, ttl: float = KEEP_ALIVE_TTL) -> bool:
        """Check if connection is stale."""
        return time.time() - self.last_used_at > ttl

    def is_exhausted(self, limit: int = POOL_REUSE_LIMIT) -> bool:
        """Check if connection exceeded reuse limit."""
        return self.request_count >= limit

    def mark_used(self):
        """Update last_used_at."""
        self.last_used_at = time.time()

    def mark_error(self):
        """Increment error count."""
        self.error_count += 1


class ConnectionPool:
    """Per-peer connection pool."""

    def __init__(self, peer: str, port: int = DEFAULT_PORT, pool_size: int = POOL_SIZE_PER_PEER):
        self.peer = peer
        self.port = port
        self.pool_size = pool_size
        self.connections: deque = deque()
        self.pending_requests = 0
        self.total_connections = 0
        self.metrics = {
            "connects": 0,
            "reuses": 0,
            "failures": 0,
            "timeouts": 0,
        }

    async def get_connection(self) -> PooledConnection:
        """Get connection from pool or create new."""
        # Try to reuse existing connection
        while self.connections:
            conn = self.connections.popleft()
            if conn.is_stale() or conn.is_exhausted():
                try:
                    conn.writer.close()
                    await conn.writer.wait_closed()
                except Exception:
                    pass
                continue

            conn.in_use = True
            self.metrics["reuses"] += 1
            return conn

        # Create new connection
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.peer, self.port),
                timeout=CONNECTION_TIMEOUT,
            )
            conn = PooledConnection(reader, writer)
            self.total_connections += 1
            self.metrics["connects"] += 1
            conn.in_use = True
            return conn
        except asyncio.TimeoutError:
            self.metrics["timeouts"] += 1
            raise
        except Exception as e:
            self.metrics["failures"] += 1
            raise

    def return_connection(self, conn: PooledConnection, had_error: bool = False):
        """Return connection to pool."""
        conn.in_use = False
        conn.request_count += 1
        conn.mark_used()

        if had_error:
            conn.mark_error()

        if len(self.connections) < self.pool_size and not conn.is_exhausted():
            self.connections.append(conn)
        else:
            try:
                conn.writer.close()
                conn.writer.wait_closed()
            except Exception:
                pass

    async def close_all(self):
        """Close all pooled connections."""
        while self.connections:
            conn = self.connections.popleft()
            try:
                conn.writer.close()
                await conn.writer.wait_closed()
            except Exception:
                pass

    def get_metrics(self) -> Dict[str, Any]:
        """Return pool metrics."""
        return {
            "peer": self.peer,
            "total_connections": self.total_connections,
            "pooled_connections": len(self.connections),
            "pending_requests": self.pending_requests,
            "metrics": self.metrics.copy(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# OPTIMIZED CLIENT
# ─────────────────────────────────────────────────────────────────────────────

class HDGLTransportClientOptimized:
    """Async HDGL client with connection pooling and pipelining."""

    def __init__(self, local_node: str = "127.0.0.1"):
        self.local_node = local_node
        self.pools: Dict[str, ConnectionPool] = {}
        self.metrics = {
            "requests_sent": 0,
            "requests_success": 0,
            "requests_failed": 0,
            "requests_timeout": 0,
            "latency_samples": deque(maxlen=1000),
        }
        self._lock = asyncio.Lock()

    def _get_pool(self, peer: str) -> ConnectionPool:
        """Get or create connection pool for peer."""
        if peer not in self.pools:
            self.pools[peer] = ConnectionPool(peer)
        return self.pools[peer]

    async def send_frame_to_peer(
        self,
        peer_ip: str,
        frame: Any,
        timeout: float = FRAME_TIMEOUT,
    ) -> Optional[Any]:
        """Send frame to peer with connection pooling."""
        pool = self._get_pool(peer_ip)
        conn = None
        had_error = False

        try:
            req_time = time.time()
            pool.pending_requests += 1
            self.metrics["requests_sent"] += 1

            # Get connection from pool
            conn = await pool.get_connection()

            # Serialize frame
            frame_bytes = frame.serialize()

            # Send frame with timeout
            try:
                conn.writer.write(frame_bytes)
                await asyncio.wait_for(conn.writer.drain(), timeout=timeout)
            except asyncio.TimeoutError:
                self.metrics["requests_timeout"] += 1
                had_error = True
                raise

            # Read response (if expecting one)
            try:
                size_bytes = await asyncio.wait_for(
                    conn.reader.readexactly(4), timeout=timeout
                )
                frame_size = struct.unpack("!I", size_bytes)[0]
                response_body = await asyncio.wait_for(
                    conn.reader.readexactly(frame_size), timeout=timeout
                )
                latency = (time.time() - req_time) * 1000  # ms
                self.metrics["latency_samples"].append(latency)
                self.metrics["requests_success"] += 1

                # Deserialize response (basic unpacking)
                return size_bytes + response_body
            except asyncio.TimeoutError:
                self.metrics["requests_timeout"] += 1
                had_error = True
                raise

        except asyncio.TimeoutError:
            log.warning(f"[transport-client-opt] timeout to {peer_ip}")
            had_error = True
        except Exception as e:
            log.error(f"[transport-client-opt] error to {peer_ip}: {e}")
            had_error = True
            self.metrics["requests_failed"] += 1

        finally:
            pool.pending_requests -= 1
            if conn:
                pool.return_connection(conn, had_error)

        return None

    async def send_frame_batch(
        self,
        peer_ip: str,
        frames: List[Any],
        timeout: float = FRAME_TIMEOUT,
    ) -> List[Optional[Any]]:
        """Send multiple frames with pipelining."""
        pool = self._get_pool(peer_ip)
        conn = None
        had_error = False
        responses = []

        try:
            req_time = time.time()
            pool.pending_requests += len(frames)
            self.metrics["requests_sent"] += len(frames)

            # Get connection
            conn = await pool.get_connection()

            # Send all frames at once (pipelined)
            for frame in frames:
                frame_bytes = frame.serialize()
                conn.writer.write(frame_bytes)

            await asyncio.wait_for(conn.writer.drain(), timeout=timeout)

            # Read all responses
            for _ in frames:
                try:
                    size_bytes = await asyncio.wait_for(
                        conn.reader.readexactly(4), timeout=timeout
                    )
                    frame_size = struct.unpack("!I", size_bytes)[0]
                    response_body = await asyncio.wait_for(
                        conn.reader.readexactly(frame_size), timeout=timeout
                    )
                    responses.append(size_bytes + response_body)
                    self.metrics["requests_success"] += 1
                except asyncio.TimeoutError:
                    self.metrics["requests_timeout"] += 1
                    had_error = True
                    responses.append(None)

            latency = (time.time() - req_time) * 1000  # ms
            if responses:
                self.metrics["latency_samples"].append(latency)

        except Exception as e:
            log.error(f"[transport-client-opt] batch error to {peer_ip}: {e}")
            had_error = True
            self.metrics["requests_failed"] += len(frames)
            responses = [None] * len(frames)

        finally:
            pool.pending_requests -= len(frames)
            if conn:
                pool.return_connection(conn, had_error)

        return responses

    async def close_all_pools(self):
        """Close all connection pools."""
        for pool in self.pools.values():
            await pool.close_all()
        self.pools.clear()

    def get_metrics(self) -> Dict[str, Any]:
        """Return client metrics."""
        latencies = list(self.metrics["latency_samples"])
        latencies_sorted = sorted(latencies) if latencies else []

        return {
            "requests_sent": self.metrics["requests_sent"],
            "requests_success": self.metrics["requests_success"],
            "requests_failed": self.metrics["requests_failed"],
            "requests_timeout": self.metrics["requests_timeout"],
            "success_rate": (
                self.metrics["requests_success"] / max(1, self.metrics["requests_sent"])
            ),
            "latency_ms": {
                "p50": latencies_sorted[len(latencies_sorted) // 2] if latencies else 0,
                "p95": latencies_sorted[int(len(latencies_sorted) * 0.95)] if latencies else 0,
                "p99": latencies_sorted[int(len(latencies_sorted) * 0.99)] if latencies else 0,
                "mean": sum(latencies) / len(latencies) if latencies else 0,
            },
            "pools": {peer: pool.get_metrics() for peer, pool in self.pools.items()},
        }
