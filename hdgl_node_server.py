#!/usr/bin/env python3
"""
hdgl_node_server.py
───────────────────
Per-node HTTP server for the HDGL distributed host.

Each node runs this. There is no master. The lattice is the host.

Endpoints:
  GET  /node_info              → health, latency, storage, known_nodes, strand weights
  GET  /serve/<path>           → strand-addressed file read (proxy to authority if not local)
  POST /swap_invalidate        → receive cache invalidation from peers
  GET  /metrics                → per-strand weight, EMA, fingerprint
  POST /gossip                 → receive peer announcements
  GET  /strand_map             → current phi-tau → strand → authority routing table
  GET  /health                 → simple liveness probe for load balancers

Request routing:
  1. Compute phi_tau(path) → strand k
  2. Look up authoritative node for strand k via lattice
  3. If this node IS authority → serve from local swap
  4. If this node IS NOT authority → proxy to authority
  5. Cache response locally with Omega-TTL
  6. Return with X-HDGL-* headers showing routing decisions

All nodes run identical code. Authority shifts as lattice weights change.
No configuration tells a node what it is — the geometry determines it.
"""

import json
import logging
import math
import os
import sys
import time
import threading
import socket
import socketserver
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen, Request
from urllib.error import URLError

log = logging.getLogger("hdgl.node")

# ── HMAC Cluster Authentication ───────────────────────────────────────────────
# Shared cluster secret — set via env var LN_CLUSTER_SECRET.
# All nodes must share the same secret.
# If unset, authentication is disabled with a warning (dev/private clusters only).
import hmac as _hmac
import hashlib as _hashlib

_CLUSTER_SECRET = os.getenv("LN_CLUSTER_SECRET", "").encode()
_HMAC_HEADER    = "X-HDGL-Signature"
_HMAC_ALGO      = "sha256"
_HMAC_MAX_AGE   = 30   # seconds — replay window

def _sign_payload(payload: bytes, timestamp: int = None) -> str:
    """
    Produce HMAC-SHA256 signature for a payload.
    Format: "t={timestamp};sig={hexdigest}"
    """
    if not _CLUSTER_SECRET:
        return ""
    ts  = timestamp if timestamp is not None else int(time.time())
    msg = f"{ts}:".encode() + payload
    sig = _hmac.new(_CLUSTER_SECRET, msg, _hashlib.sha256).hexdigest()
    return f"t={ts};sig={sig}"


def _verify_signature(payload: bytes, header: str) -> bool:
    """
    Verify HMAC signature from X-HDGL-Signature header.
    Returns True if:
      - No cluster secret configured (open mode, warns once)
      - Signature is valid and within replay window
    Returns False if secret is set but signature is invalid/missing/replayed.
    """
    if not _CLUSTER_SECRET:
        return True   # open mode — private clusters only

    if not header:
        log.warning("[auth] request missing X-HDGL-Signature — rejected")
        return False

    try:
        parts = dict(p.split("=", 1) for p in header.split(";"))
        ts    = int(parts["t"])
        sig   = parts["sig"]
    except Exception:
        log.warning(f"[auth] malformed signature header: {header!r}")
        return False

    # Replay window check
    age = abs(int(time.time()) - ts)
    if age > _HMAC_MAX_AGE:
        log.warning(f"[auth] signature too old: age={age}s > {_HMAC_MAX_AGE}s")
        return False

    # Constant-time comparison
    msg      = f"{ts}:".encode() + payload
    expected = _hmac.new(_CLUSTER_SECRET, msg, _hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(sig, expected):
        log.warning("[auth] invalid signature — request rejected")
        return False

    return True

# ── CONFIG (all overridable via env) ─────────────────────────────────────────
NODE_IP       = os.getenv("LN_LOCAL_NODE", socket.gethostbyname(socket.gethostname()))
NODE_PORT     = int(os.getenv("LN_NODE_PORT", "8090"))
SWAP_ROOT     = Path(os.getenv("LN_FILESWAP_ROOT", "/opt/hdgl_swap"))
PROXY_TIMEOUT = int(os.getenv("LN_PROXY_TIMEOUT", "5"))
PHI           = (1 + math.sqrt(5)) / 2


# ── REQUEST HANDLER ───────────────────────────────────────────────────────────
class HDGLNodeHandler(BaseHTTPRequestHandler):
    """
    Handles incoming HTTP requests for a single HDGL node.
    lattice and swap are injected by HDGLNodeServer at startup.
    """

    # Class-level references injected by server
    lattice = None
    swap    = None
    started = time.time()

    def log_message(self, fmt, *args):
        log.debug(f"[{NODE_IP}:{NODE_PORT}] {fmt % args}")

    def _send(self, code: int, body: Any, ctype: str = "application/json",
              headers: Dict[str, str] = None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, indent=2).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.send_header("X-HDGL-Node", NODE_IP)
        if headers:
            for k, v in headers.items():
                self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")

        if path == "/health":
            self._handle_health()
        elif path == "/node_info":
            self._handle_node_info()
        elif path == "/metrics":
            self._handle_metrics()
        elif path == "/strand_map":
            self._handle_strand_map()
        elif path.startswith("/serve"):
            file_path = path[len("/serve"):]
            self._handle_serve(file_path or "/")
        else:
            # Default: treat the whole path as a file serve request
            self._handle_serve(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length) if length else b""

        if path == "/swap_invalidate":
            self._handle_invalidate(body)
        elif path == "/gossip":
            self._handle_gossip(body)
        else:
            self._send(404, {"error": "not found"})

    # ── ENDPOINT HANDLERS ─────────────────────────────────────────────────────

    def _handle_health(self):
        """Simple liveness probe — used by load balancers and NGINX upstream checks."""
        alive_nodes = [
            nid for nid, s in self.lattice._states.items()
            if self.lattice._latency_ema.get(nid, 9999) < 5000
        ]
        status = "ok" if len(alive_nodes) >= 1 else "degraded"
        self._send(200 if status == "ok" else 503, {
            "status":   status,
            "node":     NODE_IP,
            "uptime_s": round(time.time() - self.started, 1),
            "peers":    len(alive_nodes),
        })

    def _handle_node_info(self):
        """
        Full node info — consumed by peers during health checks.
        Mirrors the /node_info contract expected by living_network_daemon.py.
        """
        top     = self.lattice.top_node_per_strand()
        weights = {
            chr(65 + k): round(self.lattice.strand_weight(NODE_IP, k), 6)
            for k in range(8)
        }
        ema = self.lattice._latency_ema.get(NODE_IP, 0)
        fp  = self.lattice.fingerprint(NODE_IP)

        self._send(200, {
            "node":                NODE_IP,
            "health":              "ok",
            "latency":             round(ema, 2),
            "storage_available_gb": self._local_storage_gb(),
            "fingerprint":         fp,
            "excitation":          round(
                self.lattice._states[NODE_IP].excitation
                if NODE_IP in self.lattice._states else 0, 4
            ),
            "strand_weights":      weights,
            "known_nodes":         list(self.lattice._states.keys()),
            "authority_strands":   [
                k for k, (n, _) in top.items() if n == NODE_IP
            ],
            "uptime_s":            round(time.time() - self.started, 1),
        })

    def _handle_serve(self, file_path: str):
        """
        Core distributed host logic.

        1. phi_tau(path) → strand k
        2. Who is authoritative for strand k?
        3. If us → serve from local swap
        4. If not us → proxy to authority
        """
        if not file_path or file_path == "/":
            self._send(400, {"error": "path required"})
            return

        # Path traversal guard — strip any .. components before serving
        safe_parts = [p for p in file_path.split("/") if p and p not in ("..", ".")]
        file_path   = "/" + "/".join(safe_parts)
        if not file_path or file_path == "/":
            self._send(400, {"error": "invalid path"})
            return

        from hdgl_fileswap import _strand_for_path, _omega_ttl
        strand    = _strand_for_path(file_path)
        top       = self.lattice.top_node_per_strand()
        authority = top.get(strand, (NODE_IP, 0))[0] or NODE_IP
        ttl       = _omega_ttl(strand)
        tau       = _phi_tau_local(file_path)

        hdgl_headers = {
            "X-HDGL-Strand":    strand,
            "X-HDGL-Authority": authority,
            "X-HDGL-Tau":       round(tau, 4),
            "X-HDGL-TTL":       round(ttl, 1),
            "X-HDGL-Node":      NODE_IP,
        }

        # ── LOCAL AUTHORITY ───────────────────────────────────────────────────
        if authority == NODE_IP:
            data = self.swap.read(file_path)
            if data:
                hdgl_headers["X-HDGL-Served-By"] = "local-authority"
                self._send(200, data,
                           ctype=_guess_mime(file_path),
                           headers=hdgl_headers)
                return

            # Authority but file not found — 404
            hdgl_headers["X-HDGL-Served-By"] = "local-authority-miss"
            self._send(404, {"error": "file not found", "path": file_path,
                             "strand": strand, "authority": authority},
                       headers=hdgl_headers)
            return

        # ── PROXY TO AUTHORITY ────────────────────────────────────────────────
        hdgl_headers["X-HDGL-Served-By"] = f"proxy-to-{authority}"
        proxy_url = f"http://{authority}:{NODE_PORT}/serve{file_path}"

        try:
            req  = Request(proxy_url, headers={"X-HDGL-Forwarded-By": NODE_IP})
            resp = urlopen(req, timeout=PROXY_TIMEOUT)
            data = resp.read()

            # Cache locally with Omega-TTL
            self.swap._cache[file_path] = _make_cache_entry(
                data, file_path, strand, authority
            )

            hdgl_headers["X-HDGL-Cache"] = "MISS"
            self._send(200, data,
                       ctype=_guess_mime(file_path),
                       headers=hdgl_headers)

        except URLError as e:
            # Authority unreachable — try mirror (echo fallback)
            mirror = self.swap._mirror_for(strand)
            if mirror != authority and mirror != NODE_IP:
                try:
                    mirror_url = f"http://{mirror}:{NODE_PORT}/serve{file_path}"
                    req  = Request(mirror_url, headers={"X-HDGL-Forwarded-By": NODE_IP})
                    resp = urlopen(req, timeout=PROXY_TIMEOUT)
                    data = resp.read()
                    hdgl_headers["X-HDGL-Served-By"] = f"mirror-{mirror}"
                    hdgl_headers["X-HDGL-Cache"]     = "ECHO"
                    self._send(200, data,
                               ctype=_guess_mime(file_path),
                               headers=hdgl_headers)
                    return
                except URLError:
                    pass

            # Check local cache as last resort
            entry = self.swap._cache.get(file_path)
            if entry and not entry.expired():
                hdgl_headers["X-HDGL-Cache"]     = "STALE-OK"
                hdgl_headers["X-HDGL-Served-By"] = "local-stale-cache"
                self._send(200, entry.data,
                           ctype=_guess_mime(file_path),
                           headers=hdgl_headers)
                return

            log.error(f"[serve] all paths failed for {file_path}: {e}")
            self._send(503, {
                "error":     "authority and mirror unreachable",
                "authority": authority,
                "mirror":    mirror,
                "path":      file_path,
            }, headers=hdgl_headers)

    def _handle_invalidate(self, body: bytes):
        """
        Receive cache invalidation from a peer write.
        Requires X-HDGL-Signature header when LN_CLUSTER_SECRET is set.
        """
        sig = self.headers.get(_HMAC_HEADER, "")
        if not _verify_signature(body, sig):
            self._send(403, {"error": "invalid or missing cluster signature"})
            return
        try:
            data = json.loads(body)
            path     = data.get("path", "")
            checksum = data.get("checksum", "")
            if path and checksum:
                self.swap.invalidate(path, checksum)
            self._send(200, {"ok": True, "path": path})
        except Exception as e:
            self._send(400, {"error": str(e)})

    def _handle_gossip(self, body: bytes):
        """
        Receive peer node announcement.
        Accepts both binary (16-byte struct) and JSON formats.
        Requires X-HDGL-Signature header when LN_CLUSTER_SECRET is set.
        Binary: IP(4) + latency(f) + storage(f) + fingerprint(I) = 16 bytes
        """
        sig = self.headers.get(_HMAC_HEADER, "")
        if not _verify_signature(body, sig):
            self._send(403, {"error": "invalid or missing cluster signature"})
            return
        try:
            from hdgl_fileswap import decode_gossip, _GOSSIP_SIZE
            if len(body) == _GOSSIP_SIZE:
                # Binary gossip (16 bytes — 83% smaller than JSON)
                peer = decode_gossip(body)
                nid  = peer["node_str"]
                lat  = peer["latency"]
                stor = peer["storage_available_gb"]
                fp   = peer["fingerprint"]
                protocol = "binary"
            else:
                # JSON fallback (backwards compatible)
                peer = json.loads(body)
                nid  = peer.get("node") or peer.get("node_str", "")
                lat  = peer.get("latency", 1000)
                stor = peer.get("storage_available_gb", 1.0)
                fp   = peer.get("fingerprint", "0x00000000")
                protocol = "json"

            if not nid:
                self._send(400, {"error": "missing node identifier"})
                return

            is_new = nid not in self.lattice._states
            self.lattice.update(nid, lat, stor)
            if is_new:
                log.info(
                    f"[gossip/{protocol}] discovered {nid} "                    f"lat={lat}ms stor={stor}GB fp={fp}"                )
            self._send(200, {"ok": True, "new_peer": nid if is_new else None,
                             "protocol": protocol})
        except Exception as e:
            self._send(400, {"error": str(e)})

    def _handle_metrics(self):
        """Per-strand weights, EMA, fingerprint — for monitoring dashboards."""
        top     = self.lattice.top_node_per_strand()
        metrics = {
            "node":      NODE_IP,
            "timestamp": time.time(),
            "fingerprint": self.lattice.fingerprint(NODE_IP),
            "cluster_fingerprint": self.lattice.cluster_fingerprint(),
            "strands": [
                {
                    "index":     k,
                    "label":     chr(65 + k),
                    "authority": top.get(k, (NODE_IP, 0))[0],
                    "is_local":  top.get(k, (NODE_IP, 0))[0] == NODE_IP,
                    "weight":    round(self.lattice.strand_weight(NODE_IP, k), 6),
                    "ema_ms":    round(self.lattice._latency_ema.get(NODE_IP, 0), 2),
                    "ttl_s":     round(_omega_ttl_local(k), 1),
                }
                for k in range(8)
            ],
        }
        self._send(200, metrics)

    def _handle_strand_map(self):
        """Current phi-tau → strand → authority routing table."""
        top = self.lattice.top_node_per_strand()
        rows = []
        for k in range(8):
            from hdgl_fileswap import STRAND_GEOMETRY, alpha_ttl, TTL_BASE
            alpha, verts, poly = STRAND_GEOMETRY[k]
            auth, weight = top.get(k, (NODE_IP, 0.0))
            rows.append({
                "strand":    k,
                "label":     chr(65 + k),
                "polytope":  poly,
                "alpha":     alpha,
                "authority": auth,
                "is_local":  auth == NODE_IP,
                "weight":    round(weight, 6),
                "ttl_s":     round(alpha_ttl(k, TTL_BASE), 1),
                "verts":     verts,
            })
        self._send(200, {"node": NODE_IP, "strand_map": rows})

    def _local_storage_gb(self) -> float:
        try:
            st = os.statvfs(str(SWAP_ROOT))
            return round(st.f_bavail * st.f_frsize / 1e9, 2)
        except Exception:
            return 0.0


# ── HELPERS ───────────────────────────────────────────────────────────────────
def _guess_mime(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".mp3": "audio/mpeg",  ".mp4": "video/mp4",
        ".html": "text/html",  ".json": "application/json",
        ".png":  "image/png",  ".jpg":  "image/jpeg",
        ".css":  "text/css",   ".js":   "application/javascript",
        ".svg":  "image/svg+xml",
    }.get(ext, "application/octet-stream")


def _phi_tau_local(path: str) -> float:
    PHI = (1 + math.sqrt(5)) / 2
    segments = [s for s in path.strip("/").split("/") if s]
    tau = 0.0
    for depth, seg in enumerate(segments):
        intra = (sum(ord(c) for c in seg) % 1000) / 1000.0
        tau  += (PHI ** depth) * (depth + intra)
    return tau


def _omega_ttl_local(strand_idx: int) -> float:
    from hdgl_fileswap import _omega_ttl
    return _omega_ttl(strand_idx)


def _make_cache_entry(data: bytes, path: str, strand: int, authority: str):
    """Build a CacheEntry-compatible object for local caching of proxied responses."""
    import hashlib
    from hdgl_fileswap import CacheEntry
    return CacheEntry(
        data=data,
        checksum=hashlib.sha256(data).hexdigest(),
        strand_idx=strand,
        authority=authority,
    )


# ── SERVER ────────────────────────────────────────────────────────────────────
class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """
    Multi-threaded HTTP server — each request handled in its own thread.
    Eliminates single-threaded queue under concurrent load (production fix).
    daemon_threads=True ensures clean shutdown without waiting for requests.
    """
    daemon_threads      = True
    allow_reuse_address = True


class HDGLNodeServer:
    """
    Wraps ThreadedHTTPServer, injects lattice + swap into handler,
    runs in a background thread so HDGLHost can drive it.
    """

    def __init__(self, lattice, swap, port: int = NODE_PORT):
        self.lattice = lattice
        self.swap    = swap
        self.port    = port
        self._server: Optional[_ThreadedHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

        # Inject into handler class
        HDGLNodeHandler.lattice = lattice
        HDGLNodeHandler.swap    = swap
        HDGLNodeHandler.started = time.time()

    def start(self):
        self._server = _ThreadedHTTPServer(("0.0.0.0", self.port), HDGLNodeHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="hdgl-node-server",
        )
        self._thread.start()
        log.info(f"[node-server] multi-threaded, listening on 0.0.0.0:{self.port}")

    def stop(self):
        if self._server:
            self._server.shutdown()
            log.info("[node-server] stopped")


# ── STANDALONE ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile, shutil
    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")

    _td = Path(tempfile.mkdtemp(prefix="hdgl_node_"))
    os.environ["LN_FILESWAP_ROOT"]  = str(_td / "swap")
    os.environ["LN_FILESWAP_CACHE"] = str(_td / "cache")

    from hdgl_lattice  import HDGLLattice
    from hdgl_fileswap import HDGLFileswap

    lat  = HDGLLattice()
    lat.update(NODE_IP, 50.0, 10.0)
    swap = HDGLFileswap(lat, local_node=NODE_IP)
    swap._dry_run_override = True
    swap.write("/test/hello.txt", b"hello from hdgl node")

    srv = HDGLNodeServer(lat, swap, port=NODE_PORT)
    srv.start()

    log.info(f"Node server running — test with:")
    log.info(f"  curl http://localhost:{NODE_PORT}/health")
    log.info(f"  curl http://localhost:{NODE_PORT}/node_info")
    log.info(f"  curl http://localhost:{NODE_PORT}/strand_map")
    log.info(f"  curl http://localhost:{NODE_PORT}/serve/test/hello.txt")
    log.info("Press Ctrl+C to stop")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()
        shutil.rmtree(_td, ignore_errors=True)
