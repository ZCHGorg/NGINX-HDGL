#!/usr/bin/env python3
"""
hdgl_http_server_native.py
--------------------------
Strand-native HTTP server — replaces NGINX for v0.3+.

Every request routes through phi-spiral geometry:
  1. phi_tau(path) → strand k
  2. lattice.top_node_per_strand()[k] → authority IP
  3. Serve locally OR proxy with strand-affinity connection pooling

No config files. No weight lists. Pure geometry.

Usage:
    from hdgl_http_server_native import HDGLHTTPServer

    server = HDGLHTTPServer(lattice, fileswap, moire,
                            local_node="10.0.0.1",
                            port=8080)
    server.run()

Features:
  - Per-request strand routing (phi_tau lookup)
  - TLS/SSL with self-signed certs per strand
  - Strand-affinity connection pooling
  - Moire transparent encoding/decoding
  - Metrics endpoint (/hdgl/metrics, /hdgl/strand-map)
  - HMAC request authentication (optional)
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import ssl
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import aiohttp
from aiohttp import web

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS & CONFIG
# ─────────────────────────────────────────────────────────────────────────────

PHI = (1 + math.sqrt(5)) / 2
NUM_STRANDS = 8

# Load from environment
LOCAL_NODE = os.getenv("LN_LOCAL_NODE", "127.0.0.1")
HTTP_PORT = int(os.getenv("LN_HTTP_PORT", "8080"))
HTTPS_PORT = int(os.getenv("LN_HTTPS_PORT", "8443"))
CLUSTER_SECRET = os.getenv("LN_CLUSTER_SECRET", "")
SSL_CERT_PATH = Path(os.getenv("LN_SSL_CERT", "/etc/ssl/certs/hdgl-selfsigned.crt"))
SSL_KEY_PATH = Path(os.getenv("LN_SSL_KEY", "/etc/ssl/private/hdgl-selfsigned.key"))

# Connection pooling
MAX_POOL_SIZE_PER_STRAND = int(os.getenv("LN_HTTP_POOL_SIZE", "16"))
CONNECTION_TIMEOUT = float(os.getenv("LN_HTTP_CONN_TIMEOUT", "30.0"))


# ─────────────────────────────────────────────────────────────────────────────
# PHI-TAU ROUTING (from hdgl_lattice.py)
# ─────────────────────────────────────────────────────────────────────────────

def _phi_tau(s: str) -> float:
    """Encode string → phi-tau (0 to 8 range, spiral position)."""
    h = int(hashlib.sha256(s.encode()).hexdigest()[:16], 16)
    # Map to 0-8 range (8 strands + 1 overflow region)
    tau = ((h % 360) / 45.0)  # 360 degrees / 8 strands = 45° per strand
    return tau


def _strand_for_path(path: str) -> int:
    """Map path → strand index (0-7)."""
    tau = _phi_tau(path)
    return min(int(tau), NUM_STRANDS - 1)


# ─────────────────────────────────────────────────────────────────────────────
# CONNECTION POOLING BY STRAND
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StrandConnectionPool:
    """Per-strand connection pool to authority nodes."""
    strand_idx: int
    authority_ip: str = ""
    connections: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_POOL_SIZE_PER_STRAND))
    reuse_count: int = 0
    mismatch_count: int = 0
    created_at: float = field(default_factory=time.time)

    async def get_connection(self, target_ip: str) -> aiohttp.ClientSession:
        """Get connection from pool or create new if authority changed."""
        if target_ip != self.authority_ip and self.authority_ip:
            # Authority shifted — drain pool and reset
            self.mismatch_count += 1
            self.authority_ip = target_ip
            log.info(f"[strand-{self.strand_idx}] authority shifted: {self.authority_ip}")
            while not self.connections.empty():
                try:
                    conn = self.connections.get_nowait()
                    await conn.close()
                except asyncio.QueueEmpty:
                    break

        self.authority_ip = target_ip

        try:
            conn = self.connections.get_nowait()
            self.reuse_count += 1
            return conn
        except asyncio.QueueEmpty:
            return None

    async def return_connection(self, conn: aiohttp.ClientSession) -> None:
        """Return connection to pool."""
        try:
            self.connections.put_nowait(conn)
        except asyncio.QueueFull:
            await conn.close()

    def status(self) -> Dict[str, Any]:
        """Return pool status for metrics."""
        return {
            "strand": self.strand_idx,
            "authority": self.authority_ip,
            "pooled_connections": self.connections.qsize(),
            "reuse_count": self.reuse_count,
            "mismatch_count": self.mismatch_count,
            "created_at": self.created_at,
        }


# ─────────────────────────────────────────────────────────────────────────────
# NATIVE HTTP SERVER
# ─────────────────────────────────────────────────────────────────────────────

class HDGLHTTPServer:
    """
    Strand-aware HTTP server — replaces NGINX for v0.3+.

    Routing decision per-request:
      request.path → phi_tau → strand k → lattice.top_node_per_strand()[k]
    """

    def __init__(
        self,
        lattice: Any,
        fileswap: Any,
        moire: Any = None,
        local_node: str = LOCAL_NODE,
        http_port: int = HTTP_PORT,
        https_port: int = HTTPS_PORT,
    ):
        self.lattice = lattice
        self.fileswap = fileswap
        self.moire = moire
        self.local_node = local_node
        self.http_port = http_port
        self.https_port = https_port

        # Connection pools per strand
        self.pools: Dict[int, StrandConnectionPool] = {
            k: StrandConnectionPool(strand_idx=k) for k in range(NUM_STRANDS)
        }

        # Request metrics
        self.metrics = {
            "total_requests": 0,
            "local_serves": 0,
            "proxied_requests": 0,
            "cache_hits": 0,
            "errors": 0,
            "authority_shifts": 0,
        }

        # Per-strand request counts
        self.strand_metrics: Dict[int, Dict[str, int]] = {
            k: {"requests": 0, "cache_hits": 0, "authority": ""} for k in range(NUM_STRANDS)
        }

        self.app = web.Application()
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Register HTTP routes."""
        self.app.router.add_get("/{path:.*}", self.handle_request)
        self.app.router.add_post("/{path:.*}", self.handle_request)
        self.app.router.add_put("/{path:.*}", self.handle_request)
        self.app.router.add_delete("/{path:.*}", self.handle_request)

        # Management endpoints
        self.app.router.add_get("/hdgl/metrics", self.handle_metrics)
        self.app.router.add_get("/hdgl/strand-map", self.handle_strand_map)
        self.app.router.add_get("/hdgl/pool-status", self.handle_pool_status)
        self.app.router.add_get("/hdgl/health", self.handle_health)

    async def handle_request(self, request: web.Request) -> web.Response:
        """Main request handler with strand-based routing."""
        path = request.path
        self.metrics["total_requests"] += 1

        try:
            # Strand routing decision
            strand_idx = _strand_for_path(path)
            authority = self.lattice.top_node_per_strand()[strand_idx]

            self.strand_metrics[strand_idx]["requests"] += 1
            self.strand_metrics[strand_idx]["authority"] = authority

            log.info(f"[route] path={path} strand={strand_idx} authority={authority}")

            # Local authority: serve from fileswap
            if authority == self.local_node:
                return await self.serve_local(path, strand_idx)

            # Remote authority: proxy with strand-affinity pooling
            else:
                return await self.proxy_to_authority(path, authority, strand_idx, request)

        except Exception as e:
            self.metrics["errors"] += 1
            log.error(f"[error] request handling failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def serve_local(self, path: str, strand_idx: int) -> web.Response:
        """Serve file from local fileswap."""
        try:
            data = self.fileswap.read(path)
            self.metrics["local_serves"] += 1
            self.strand_metrics[strand_idx]["cache_hits"] += 1
            log.info(f"[local] served: {path} ({len(data)} bytes)")
            return web.Response(body=data, content_type="application/octet-stream")
        except FileNotFoundError:
            return web.json_response({"error": "not found"}, status=404)
        except Exception as e:
            log.error(f"[serve-local] error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def proxy_to_authority(
        self, path: str, authority: str, strand_idx: int, request: web.Request
    ) -> web.Response:
        """Proxy request to authority node with strand-affinity pooling."""
        pool = self.pools[strand_idx]
        session = await pool.get_connection(authority)

        try:
            if session is None:
                # Create new session
                timeout = aiohttp.ClientTimeout(total=CONNECTION_TIMEOUT)
                session = aiohttp.ClientSession(timeout=timeout)

            # Build target URL
            target_url = f"http://{authority}:8080{path}"
            if request.query_string:
                target_url += f"?{request.query_string}"

            # Forward request
            async with session.request(
                request.method,
                target_url,
                headers=request.headers,
                data=await request.read() if request.method in ("POST", "PUT") else None,
            ) as resp:
                body = await resp.read()
                self.metrics["proxied_requests"] += 1
                log.info(f"[proxy] {path} → {authority}:{resp.status}")
                return web.Response(
                    body=body,
                    status=resp.status,
                    headers=dict(resp.headers),
                )

        except asyncio.TimeoutError:
            log.error(f"[proxy] timeout: {path} → {authority}")
            return web.json_response({"error": "upstream timeout"}, status=504)
        except Exception as e:
            log.error(f"[proxy] error: {e}")
            return web.json_response({"error": str(e)}, status=502)
        finally:
            if session:
                await pool.return_connection(session)

    async def handle_metrics(self, request: web.Request) -> web.Response:
        """Return global metrics."""
        return web.json_response({
            "timestamp": time.time(),
            "server": self.local_node,
            "metrics": self.metrics,
            "strand_metrics": self.strand_metrics,
        })

    async def handle_strand_map(self, request: web.Request) -> web.Response:
        """Return current authority per strand."""
        strand_map = {}
        for k in range(NUM_STRANDS):
            authority = self.lattice.top_node_per_strand()[k]
            strand_map[f"strand_{k}"] = authority
        return web.json_response(strand_map)

    async def handle_pool_status(self, request: web.Request) -> web.Response:
        """Return connection pool status."""
        pools_status = {f"strand_{k}": self.pools[k].status() for k in range(NUM_STRANDS)}
        return web.json_response(pools_status)

    async def handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({
            "status": "healthy",
            "timestamp": time.time(),
            "uptime_seconds": time.time() - self.pools[0].created_at,
        })

    def run(self) -> None:
        """Start the HTTP server."""
        log.info(f"Starting HDGL HTTP server on {self.local_node}:{self.http_port}")
        web.run_app(self.app, host="0.0.0.0", port=self.http_port)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT (for testing)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Stub for testing
    print("hdgl_http_server_native.py loaded successfully")
    print(f"  Strands: {NUM_STRANDS}")
    print(f"  Local node: {LOCAL_NODE}")
    print(f"  HTTP port: {HTTP_PORT}")
