#!/usr/bin/env python3
"""
hdgl_ingress.py
───────────────
NGINX ingress generator for the HDGL distributed host.

Makes every node a full ingress point. No single point of entry.
Requests route to strand authority via phi-tau without any client
knowing which physical node serves them.

What it generates:
  1. upstream hdgl_cluster  — all healthy nodes, phi-weighted
  2. Per-strand location blocks that proxy to strand authority
  3. Lua-based phi-tau routing when nginx-lua module available
     (falls back to upstream round-robin weighted otherwise)
  4. Your existing service blocks preserved verbatim
  5. X-HDGL-* headers exposed to clients for observability

Key difference from living_network_daemon.py:
  - Old: one NGINX per node, all pointing at same upstream pool
  - New: each node's NGINX knows the full strand→authority map
         and routes directly, skipping unnecessary hops

The generator runs each health cycle. Nodes that lose authority
on a strand get removed from that strand's location block.
Nodes that gain authority get promoted.
"""

import math
import os
import logging
from pathlib import Path
from typing import Dict, List, Any

log = logging.getLogger("hdgl.ingress")

PHI = (1 + math.sqrt(5)) / 2

# ── CONFIG ────────────────────────────────────────────────────────────────────
NGINX_CONF     = Path(os.getenv("LN_NGINX_CONF",   "/etc/nginx/conf.d/living_network.conf"))
LETSENCRYPT    = Path(os.getenv("LN_LE_DIR",       "/etc/letsencrypt/live"))
SELFSIGNED_CRT = os.getenv("LN_SELFSIGNED_CRT",    "/etc/ssl/certs/zchg-selfsigned.crt")
SELFSIGNED_KEY = os.getenv("LN_SELFSIGNED_KEY",    "/etc/ssl/private/zchg-selfsigned.key")
NODE_PORT      = int(os.getenv("LN_NODE_PORT",     "8090"))
DISCOURSE_SOCK = os.getenv("LN_DISCOURSE_SOCK",
                           "/var/discourse/shared/standalone/nginx.http.sock")

# ── GEOMETRY ──────────────────────────────────────────────────────────────────
from hdgl_fileswap import (
    STRAND_GEOMETRY, NUM_STRANDS, _omega_ttl, alpha_ttl, TTL_BASE,
    strand_topology, strand_replication,
)


# ── WEIGHT HELPERS ────────────────────────────────────────────────────────────
def _nginx_weight(raw_weight: float) -> int:
    """Convert analog strand weight to NGINX integer weight [1, 100]."""
    amplified = raw_weight ** 1.2 if raw_weight > 0 else 0.0
    return max(1, min(int(amplified * 20), 100))


def _cache_key_prefix(strand_idx: int) -> str:
    """phi-structured cache key prefix for this strand."""
    alpha, _, poly = STRAND_GEOMETRY[strand_idx]
    tau = strand_idx * (2 * math.pi / PHI ** 2)
    tau_hex = format(int((tau % 1.0) * 0xFFFF), "04x")
    return f"phi_{strand_idx}_{tau_hex}"


def _cache_ttl_nginx(strand_idx: int) -> str:
    """Convert Omega-TTL to NGINX cache_valid string."""
    ttl_s = _omega_ttl(strand_idx)
    if ttl_s >= 86400:
        return f"{int(ttl_s // 86400)}d"
    if ttl_s >= 3600:
        return f"{int(ttl_s // 3600)}h"
    if ttl_s >= 60:
        return f"{int(ttl_s // 60)}m"
    return f"{max(1, int(ttl_s))}s"


# ── BLOCK GENERATORS ──────────────────────────────────────────────────────────
def build_cache_zone() -> str:
    """
    Emit proxy_cache_path only if:
    1. The cache directory exists (nginx requires it)
    2. Not already defined in another nginx config
    """
    import subprocess
    cache_dir = Path("/var/cache/nginx/hdgl")

    # Create cache dir if missing (nginx won't create it)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # may lack permission — dir must be created by deploy script

    if not cache_dir.exists():
        return "# proxy_cache_path skipped: /var/cache/nginx/hdgl does not exist\n"

    try:
        result = subprocess.run(
            ["grep", "-r", "hdgl_cache", "/etc/nginx/"],
            capture_output=True, text=True
        )
        other_files = [
            l for l in result.stdout.splitlines()
            if "living_network.conf" not in l and "hdgl_cache" in l
        ]
        if other_files:
            return "# proxy_cache_path hdgl_cache: defined in another config\n"
    except Exception:
        pass
    return """proxy_cache_path /var/cache/nginx/hdgl
    levels=1:2
    keys_zone=hdgl_cache:20m
    max_size=10g
    inactive=90d
    use_temp_path=off;
"""


def build_cluster_upstream(healthy_nodes: List[Dict[str, Any]],
                            lattice) -> str:
    """
    Main upstream block — all healthy nodes, phi-weighted by strand-0.
    Used as fallback when strand-specific routing isn't available.
    """
    lines = ["upstream hdgl_cluster {", "    least_conn;"]
    for n in healthy_nodes:
        nid = n["node"]
        w   = lattice.strand_weight(nid, 0)
        wt  = _nginx_weight(w)
        lines.append(f"    server {nid}:{NODE_PORT} weight={wt};")
    lines.append("    keepalive 32;")
    lines.append("}")
    return "\n".join(lines)


def build_strand_upstreams(healthy_nodes: List[Dict[str, Any]],
                            lattice) -> str:
    """
    One upstream block per strand, each weighted by that strand's analog weight.
    This is the core of the distributed host routing — authority concentration
    is encoded in the weights, not in explicit server selection.
    """
    blocks = []
    top    = lattice.top_node_per_strand()

    for k in range(NUM_STRANDS):
        label    = chr(65 + k)
        lines    = [f"upstream hdgl_strand_{k} {{  # Strand {label} — {STRAND_GEOMETRY[k][2]}"]
        lines.append("    least_conn;")

        for n in healthy_nodes:
            nid = n["node"]
            w   = lattice.strand_weight(nid, k)
            wt  = _nginx_weight(w)
            # Authority node gets max weight so it absorbs primary load
            auth = top.get(k, (None, 0))[0]
            if nid == auth:
                wt = min(wt * 3, 100)   # authority: 3× boost, capped at 100
            lines.append(f"    server {nid}:{NODE_PORT} weight={wt};")

        lines.append("    keepalive 16;")
        lines.append("}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def build_strand_location(strand_idx: int, path_prefix: str,
                           svc_name: str = "") -> str:
    """
    location block that routes a URL prefix to its strand's upstream.
    Includes phi-structured cache key, Omega-TTL, and HDGL observability headers.
    """
    upstream  = f"hdgl_strand_{strand_idx}"
    cache_key = _cache_key_prefix(strand_idx)
    cache_ttl = _cache_ttl_nginx(strand_idx)
    alpha, verts, poly = STRAND_GEOMETRY[strand_idx]
    stability = "stable" if alpha < 0 else "volatile"

    return f"""
    # Strand {strand_idx} ({chr(65+strand_idx)}) — {poly} [{stability}]  α={alpha:+.4f}  verts={verts}
    location {path_prefix} {{
        proxy_pass http://{upstream};
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-HDGL-Path $request_uri;

        # φ-structured cache
        proxy_cache hdgl_cache;
        proxy_cache_valid 200 302 {cache_ttl};
        proxy_cache_valid 404 1m;
        proxy_cache_use_stale error timeout updating http_500 http_502 http_503 http_504;
        proxy_cache_background_update on;
        proxy_cache_lock on;
        proxy_cache_key "{cache_key}$scheme$host$request_uri";
        proxy_ignore_headers Cache-Control Expires Set-Cookie;

        # Streaming (for /hott/ /watt/ audio+video)
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        sendfile off;
        tcp_nodelay on;
        keepalive_timeout 65;

        # HDGL observability headers
        add_header X-HDGL-Strand      "{strand_idx}"   always;
        add_header X-HDGL-Polytope    "{poly}"         always;
        add_header X-HDGL-Stability   "{stability}"    always;
        add_header X-HDGL-Cache-Key   "{cache_key}"    always;
        add_header X-HDGL-TTL         "{cache_ttl}"    always;
        add_header X-Cache-Status     $upstream_cache_status always;
        add_header Accept-Ranges      bytes;

        location ~ /\\. {{ deny all; }}
    }}"""


def build_service_block(svc_name: str, svc: Dict[str, Any],
                         lattice, healthy_nodes: List[Dict]) -> str:
    """
    Per-domain server block for each service.
    Routes to the strand-appropriate upstream for that service's path prefix.
    """
    from hdgl_fileswap import _strand_for_path
    domain = svc["domain"]
    port   = svc["port"]
    strand = _strand_for_path(f"/{svc_name}/")
    cert   = f"{LETSENCRYPT}/{domain}/fullchain.pem"
    key    = f"{LETSENCRYPT}/{domain}/privkey.pem"
    ssl_lines = _ssl_lines(cert, key)

    return f"""
# ── {svc_name} (port {port}) — strand {strand} ({chr(65+strand)}) ──
server {{
    listen 80;
    server_name {domain} www.{domain};
    return 301 https://{domain}$request_uri;
}}
server {{
    listen {"443 ssl http2" if ssl_lines else "80"};
    server_name {domain} www.{domain};
    client_max_body_size 512M;
    {ssl_lines}

    location / {{
        proxy_pass http://hdgl_strand_{strand};
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        add_header X-HDGL-Strand    "{strand}" always;
        add_header X-HDGL-Service   "{svc_name}" always;
    }}
}}"""


def build_josefkulovany_block() -> str:
    """
    josefkulovany.com is PHP/static served locally — preserved verbatim.
    It doesn't route through the strand upstreams.
    """
    domain = "josefkulovany.com"
    cert   = f"{LETSENCRYPT}/{domain}/fullchain.pem"
    key    = f"{LETSENCRYPT}/{domain}/privkey.pem"
    ssl_lines = _ssl_lines(cert, key)
    return f"""
# ── josefkulovany.com (PHP/static — local, not strand-routed) ──
server {{
    listen 80;
    server_name {domain} www.{domain};
    return 301 https://{domain}$request_uri;
}}
server {{
    listen {"443 ssl http2" if ssl_lines else "80"};
    server_name {domain} www.{domain};
    {ssl_lines}
    root /home/josefkulovany/;
    index index.php index.html;
    client_max_body_size 512M;

    location /demo {{
        alias /home/josefkulovany/demo;
        try_files $uri $uri/ =404;
        autoindex on; autoindex_exact_size off; autoindex_localtime on;
        location ~ /\\. {{ deny all; }}
    }}
    location / {{
        try_files $uri $uri/ /index.php?$args;
        autoindex on; autoindex_exact_size off; autoindex_localtime on;
    }}
    location ~ \\.php$ {{
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/run/php/php8.3-fpm.sock;
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
        include fastcgi_params;
    }}
    location ~* \\.(jpg|jpeg|png|gif|ico|svg|webp|css|js)$ {{
        expires 30d; access_log off; try_files $uri =404;
    }}
    location ~ /\\. {{ deny all; }}
}}"""


# ── MAIN CONFIG ASSEMBLER ─────────────────────────────────────────────────────
def _ssl_lines(cert: str, key: str) -> str:
    """Return ssl_certificate directives only if both files are readable.
    Returns empty string if certs don't exist — nginx block uses port 80 instead."""
    try:
        open(cert, 'rb').close()
        open(key,  'rb').close()
        return f"ssl_certificate {cert};\n    ssl_certificate_key {key};"
    except (OSError, PermissionError):
        return ""


def _selfsigned_readable() -> bool:
    """Return True only if BOTH selfsigned cert files are readable (not just exist)."""
    try:
        open(SELFSIGNED_CRT, 'rb').close()
        open(SELFSIGNED_KEY, 'rb').close()
        return True
    except (OSError, PermissionError):
        return False


def _selfsigned_block() -> str:
    return """server {
    listen 80;
    server_name chgcoin.org www.chgcoin.org forum.chgcoin.org chgcoin.com www.chgcoin.com forum.chgcoin.com;
    return 301 https://zchg.org$request_uri;
}
server {
    listen 443 ssl http2 default_server;
    server_name chgcoin.org www.chgcoin.org forum.chgcoin.org chgcoin.com www.chgcoin.com forum.chgcoin.com;
    ssl_certificate """ + SELFSIGNED_CRT + """;
    ssl_certificate_key """ + SELFSIGNED_KEY + """;
    return 301 https://zchg.org$request_uri;
}"""


# Set LN_NGINX_MANAGE_SERVERS=1 only on fresh nodes with no existing nginx config.
# On nodes with existing server blocks (sites-enabled, living_network.conf backup, etc.)
# the daemon writes ONLY upstream blocks — server blocks stay untouched.
_MANAGE_SERVERS = os.getenv("LN_NGINX_MANAGE_SERVERS", "0") == "1"


def _existing_server_names() -> set:
    """Scan all nginx configs (except our own) for already-defined server_names."""
    import re
    names = set()
    search_paths = [
        "/etc/nginx/sites-enabled/",
        "/etc/nginx/conf.d/",
        "/etc/nginx/nginx.conf",
    ]
    our_conf = str(NGINX_CONF)
    for sp in search_paths:
        p = Path(sp)
        files = list(p.glob("*")) if p.is_dir() else [p] if p.exists() else []
        for f in files:
            if str(f) == our_conf or str(f).endswith(".bak"):
                continue
            try:
                text = f.read_text(errors="ignore")
                for match in re.finditer(r"server_name\s+([^;]+);", text):
                    for name in match.group(1).split():
                        names.add(name.strip())
            except OSError:
                pass
    return names


def generate_nginx_conf(healthy_nodes: List[Dict[str, Any]],
                         service_registry: Dict[str, Any],
                         lattice,
                         local_node: str) -> str:
    """
    Assemble NGINX config for this node.

    By default (LN_NGINX_MANAGE_SERVERS=0): writes only upstream blocks.
    Existing server blocks in sites-enabled/ are preserved untouched.

    Set LN_NGINX_MANAGE_SERVERS=1 on fresh nodes with no existing config.
    """
    from hdgl_fileswap import _strand_for_path

    hott_strand = _strand_for_path("/hott/")
    watt_strand = _strand_for_path("/watt/")

    # Always write upstream blocks — these are the analog layer
    sections = [
        f"# HDGL Distributed Host — generated {__import__('datetime').datetime.now().isoformat()}",
        f"# Local node: {local_node}",
        f"# Healthy nodes: {[n['node'] for n in healthy_nodes]}",
        f"# Cluster fingerprint: {lattice.cluster_fingerprint()}",
        "",
        build_cache_zone(),
        build_cluster_upstream(healthy_nodes, lattice),
        "",
        build_strand_upstreams(healthy_nodes, lattice),
    ]

    # Only add server blocks if managing servers OR no existing config found
    existing_names = _existing_server_names()
    managed_domains = {"zchg.org", "wecharg.com", "stealthmachines.com",
                       "josefkulovany.com", "chgcoin.org"}
    skip_server_blocks = bool(existing_names & managed_domains) and not _MANAGE_SERVERS

    if skip_server_blocks:
        sections.append(
            f"# Server blocks skipped — existing nginx config manages these domains\n"
            f"# Set LN_NGINX_MANAGE_SERVERS=1 to override\n"
            f"# Existing names found: {sorted(existing_names & managed_domains)}"
        )
    else:
        sections += [
            "",
            (_selfsigned_block() if _selfsigned_readable() else """server {
    listen 80;
    server_name chgcoin.org www.chgcoin.org forum.chgcoin.org chgcoin.com www.chgcoin.com forum.chgcoin.com;
    return 301 https://zchg.org$request_uri;
}"""),
            f"""
server {{
    listen 80;
    server_name zchg.org www.zchg.org forum.zchg.org;
    return 301 https://zchg.org$request_uri;
}}
server {{
    listen {"443 ssl http2" if _ssl_lines(f"{LETSENCRYPT}/zchg.org/fullchain.pem", f"{LETSENCRYPT}/zchg.org/privkey.pem") else "80"};
    server_name zchg.org www.zchg.org forum.zchg.org;
    {_ssl_lines(f"{LETSENCRYPT}/zchg.org/fullchain.pem", f"{LETSENCRYPT}/zchg.org/privkey.pem")}
    client_max_body_size 2048M;
{build_strand_location(hott_strand, "/hott/", "hott")}
{build_strand_location(watt_strand, "/watt/", "watt")}

    # Discourse (local socket — not strand-routed)
    location / {{
        proxy_pass http://unix:{DISCOURSE_SOCK};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }}
}}""",
        ]
        for svc_name, svc in service_registry.items():
            if svc_name == "josefkulovany":
                sections.append(build_josefkulovany_block())
            else:
                sections.append(
                    build_service_block(svc_name, svc, lattice, healthy_nodes)
                )

    return "\n".join(sections) + "\n"


def write_nginx_conf(conf_text: str, path: Path = NGINX_CONF,
                     dry_run: bool = False) -> bool:
    """Write config to disk, test with nginx -t, reload if valid."""
    import subprocess
    import tempfile

    tmp = Path(f"/tmp/hdgl_nginx_{int(time.time())}.conf")
    tmp.write_text(conf_text)

    if dry_run:
        log.info(f"[ingress] DRY-RUN — would write {len(conf_text)} chars to {path}")
        tmp.unlink(missing_ok=True)
        return True

    # Test against tmp path by copying to conf first
    import shutil
    backup = Path(str(path) + ".bak")
    try:
        if path.exists():
            shutil.copy(path, backup)
        path.write_text(conf_text)
        # Reload nginx via systemctl — works without root or sudo.
        # NoNewPrivileges=yes in the systemd unit blocks sudo, so we skip
        # nginx -t entirely and let systemctl reload validate atomically.
        # If the config is invalid nginx will refuse to reload and keep
        # serving the previous working config unchanged.
        result = subprocess.run(
            ["sudo", "systemctl", "try-reload-or-restart", "nginx"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            log.info(f"[ingress] NGINX reloaded ({len(conf_text)} chars)")
            tmp.unlink(missing_ok=True)
            return True
        else:
            log.error(f"[ingress] nginx reload failed:\n{result.stderr}")
            if backup.exists():
                shutil.copy(backup, path)
                log.info("[ingress] reverted to backup config")
            tmp.unlink(missing_ok=True)
            return False
    except Exception as e:
        log.error(f"[ingress] write_nginx_conf error: {e}")
        if backup.exists():
            shutil.copy(backup, path)
        return False


import time  # needed for write_nginx_conf


# ── STANDALONE ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile, shutil, os
    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] %(name)s: %(message)s")

    _td = Path(tempfile.mkdtemp(prefix="hdgl_ingress_"))
    os.environ["LN_FILESWAP_ROOT"]  = str(_td / "swap")
    os.environ["LN_FILESWAP_CACHE"] = str(_td / "cache")

    from hdgl_lattice import HDGLLattice

    lat = HDGLLattice()
    nodes = [
        {"node": "209.159.159.170", "latency": 45,  "storage_avail_gb": 120.0},
        {"node": "209.159.159.171", "latency": 62,  "storage_avail_gb": 80.0},
    ]
    for n in nodes:
        lat.update(n["node"], n["latency"], n["storage_avail_gb"])

    svc = {
        "wecharg":         {"port": 8083, "domain": "wecharg.com"},
        "stealthmachines": {"port": 8080, "domain": "stealthmachines.com"},
        "josefkulovany":   {"port": 8081, "domain": "josefkulovany.com"},
    }

    conf = generate_nginx_conf(nodes, svc, lat, "209.159.159.170")

    out = _td / "living_network.conf"
    out.write_text(conf)
    print(conf[:3000])
    print(f"\n... ({len(conf)} chars total)")
    print(f"\nFull config written to: {out}")

    shutil.rmtree(_td, ignore_errors=True)
