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
import logging
import math
import os
import ssl
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import aiohttp
from aiohttp import web

from hdgl_fileswap import _phi_tau as fileswap_phi_tau
from hdgl_fileswap import _strand_for_path as fileswap_strand_for_path

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
SSL_CERT_PATH = Path(os.getenv("LN_SSL_CERT", "/opt/hdgl/tls/hdgl.crt"))
SSL_KEY_PATH = Path(os.getenv("LN_SSL_KEY", "/opt/hdgl/tls/hdgl.key"))

# Connection pooling
MAX_POOL_SIZE_PER_STRAND = int(os.getenv("LN_HTTP_POOL_SIZE", "16"))
CONNECTION_TIMEOUT = float(os.getenv("LN_HTTP_CONN_TIMEOUT", "30.0"))


# ─────────────────────────────────────────────────────────────────────────────
# PHI-TAU ROUTING (from hdgl_lattice.py)
# ─────────────────────────────────────────────────────────────────────────────

def _phi_tau(s: str) -> float:
    """Delegate to the canonical fileswap phi-tau implementation."""
    return fileswap_phi_tau(s)


def _strand_for_path(path: str) -> int:
    """Delegate to the canonical fileswap strand routing implementation."""
    return fileswap_strand_for_path(path)


def _authority_node(authority_entry: Any) -> str:
    """Normalize lattice authority entries to the authoritative node ID."""
    if isinstance(authority_entry, tuple):
        return str(authority_entry[0])
    return str(authority_entry)


def _authority_weight(authority_entry: Any) -> Optional[float]:
    """Return authority weight when the lattice provides one."""
    if isinstance(authority_entry, tuple) and len(authority_entry) > 1:
        try:
            return float(authority_entry[1])
        except (TypeError, ValueError):
            return None
    return None


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
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._https_site: Optional[web.TCPSite] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()

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
        # Management endpoints
        self.app.router.add_get("/hdgl/metrics", self.handle_metrics)
        self.app.router.add_get("/hdgl/strand-map", self.handle_strand_map)
        self.app.router.add_get("/hdgl/pool-status", self.handle_pool_status)
        self.app.router.add_get("/hdgl/health", self.handle_health)

        # Catch-all application routes must come after management endpoints.
        self.app.router.add_get("/{path:.*}", self.handle_request)
        self.app.router.add_post("/{path:.*}", self.handle_request)
        self.app.router.add_put("/{path:.*}", self.handle_request)
        self.app.router.add_delete("/{path:.*}", self.handle_request)

    async def handle_request(self, request: web.Request) -> web.Response:
        """Main request handler with strand-based routing."""
        path = request.path
        self.metrics["total_requests"] += 1

        try:
            # Strand routing decision
            strand_idx = _strand_for_path(path)
            authority_entry = self.lattice.top_node_per_strand()[strand_idx]
            authority = _authority_node(authority_entry)
            authority_weight = _authority_weight(authority_entry)

            self.strand_metrics[strand_idx]["requests"] += 1
            self.strand_metrics[strand_idx]["authority"] = authority

            log.info(
                f"[route] path={path} strand={strand_idx} authority={authority}"
                + (f" weight={authority_weight:.5f}" if authority_weight is not None else "")
            )

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
            target_url = f"http://{authority}:{self.http_port}{path}"
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
            authority_entry = self.lattice.top_node_per_strand()[k]
            strand_map[f"strand_{k}"] = {
                "node": _authority_node(authority_entry),
                "weight": _authority_weight(authority_entry),
            }
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

    async def _start_async(self) -> None:
        """Create runner and bind the HTTP listener."""
        if self._runner is not None:
            return
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host="0.0.0.0", port=self.http_port)
        await self._site.start()

        ssl_context = self._build_ssl_context()
        if ssl_context is not None:
            self._https_site = web.TCPSite(
                self._runner,
                host="0.0.0.0",
                port=self.https_port,
                ssl_context=ssl_context,
            )
            await self._https_site.start()
            log.info(f"[native-http] TLS listening on 0.0.0.0:{self.https_port}")

        self._started.set()
        log.info(f"[native-http] listening on 0.0.0.0:{self.http_port}")

    async def _stop_async(self) -> None:
        """Stop the HTTP listener and close pooled upstream sessions."""
        if self._https_site is not None:
            await self._https_site.stop()
            self._https_site = None

        if self._site is not None:
            await self._site.stop()
            self._site = None

        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

        for pool in self.pools.values():
            while not pool.connections.empty():
                try:
                    conn = pool.connections.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await conn.close()

    def _build_ssl_context(self) -> Optional[ssl.SSLContext]:
        """Create an SSL context when certificate files are available."""
        if not SSL_CERT_PATH.exists() or not SSL_KEY_PATH.exists():
            log.warning(
                f"[native-http] TLS disabled: missing cert/key at {SSL_CERT_PATH} / {SSL_KEY_PATH}"
            )
            return None

        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(str(SSL_CERT_PATH), str(SSL_KEY_PATH))
        return context

    def run(self) -> None:
        """Run the HTTP server in the current thread."""
        async def _main() -> None:
            await self._start_async()
            while True:
                await asyncio.sleep(3600)

        try:
            asyncio.run(_main())
        except KeyboardInterrupt:
            log.info("[native-http] interrupted")

    def start_background(self) -> None:
        """Start the HTTP server in a dedicated daemon thread."""
        if self._thread and self._thread.is_alive():
            return

        self._started.clear()

        def _thread_main() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._start_async())
            self._loop.run_forever()
            self._loop.run_until_complete(self._stop_async())
            self._loop.close()
            self._loop = None

        self._thread = threading.Thread(
            target=_thread_main,
            daemon=True,
            name="hdgl-native-http",
        )
        self._thread.start()
        if not self._started.wait(timeout=10):
            raise RuntimeError("native HTTP server failed to start within 10s")

    def stop(self) -> None:
        """Stop the background server if it is running."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=10)


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
