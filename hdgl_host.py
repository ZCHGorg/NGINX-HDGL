#!/usr/bin/env python3
"""
hdgl_host.py
────────────
Unified entry point for the HDGL distributed host.

This is what you run on every node. Same binary. No master.
The lattice determines each node's role dynamically.

Replaces living_network_daemon.py with a three-layer architecture:

  Layer 1 — hdgl_lattice.py    : analog weights, EMA, fingerprints
  Layer 2 — hdgl_fileswap.py   : strand-addressed file routing, echo, migration
  Layer 3 — hdgl_node_server.py: per-node HTTP server (serve, proxy, gossip)
                         hdgl_http_server_native.py: public strand-native HTTP ingress

Each cycle:
  1. Health-check all known peers via /node_info
  2. Update EMA feedback for each live peer
  3. Gossip this node's info to all peers (so they update their lattice)
  4. Run fileswap rebalance (migrate files if strand authority shifted)
    5. Refresh in-memory routing state for public native HTTP serving
  6. Log cluster fingerprint, alive nodes, authority map

Boot sequence:
  1. Load lattice state from disk (if any)
    2. Start internal node server and public native HTTP server
  3. Begin health loop
  4. On first successful peer contact: emit "cluster joined" log
    5. In SIMULATION_MODE: print matrix, skip SSH/SCP

Usage:
  python3 hdgl_host.py                  # uses env vars
    LN_LOCAL_NODE=10.0.0.10 \
  LN_SIMULATION=0 LN_DRY_RUN=0 \\
    python3 hdgl_host.py
"""

import json
import logging
import os
import sys
import time
import signal
import threading
import subprocess
import ipaddress as _ipaddress
import requests
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hdgl.host")

# ── env config ────────────────────────────────────────────────────────────────
import socket as _socket


def _detect_outbound_ip() -> str:
    """
    Detect the IP of the interface used for outbound traffic.
    Avoids the Ubuntu/Debian /etc/hosts 127.0.1.1 trap where
    gethostbyname(gethostname()) returns a loopback address instead
    of the real external interface IP.
    Falls back to gethostbyname only if the UDP probe also fails.
    """
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass
    # Last resort: hostname lookup (may return 127.0.1.1 on Ubuntu — accepted only
    # if nothing better is available and the caller sets LN_LOCAL_NODE explicitly)
    return _socket.gethostbyname(_socket.gethostname())


def _is_valid_cluster_ip(ip: str) -> bool:
    """Return True for routable unicast peer addresses used by cluster gossip."""
    try:
        addr = _ipaddress.ip_address(ip)
        return not (
            addr.is_loopback
            or addr.is_unspecified
            or addr.is_multicast
            or addr.is_link_local
        )
    except ValueError:
        return False


def _resolve_local_node() -> str:
    """
    Resolve local node IP with safety checks.

    If LN_LOCAL_NODE is set but invalid for cluster routing (loopback, etc.),
    ignore it and fall back to outbound interface detection.
    """
    env_ip = os.getenv("LN_LOCAL_NODE", "").strip()
    if env_ip:
        if _is_valid_cluster_ip(env_ip):
            return env_ip
        log.warning(
            f"[config] ignoring invalid LN_LOCAL_NODE={env_ip!r}; "
            f"falling back to outbound IP detection"
        )

    detected = _detect_outbound_ip()
    if _is_valid_cluster_ip(detected):
        return detected

    log.warning(
        f"[config] outbound IP detection returned non-routable address {detected!r}; "
        "set LN_LOCAL_NODE explicitly to the public/private cluster IP"
    )
    return detected


LOCAL_NODE        = _resolve_local_node()
SSH_USER          = os.getenv("LN_SSH_USER",          "deployuser")
NODE_PORT         = int(os.getenv("LN_NODE_PORT",     "8090"))
HTTP_PORT         = int(os.getenv("LN_HTTP_PORT",     "8080"))
HEALTH_INTERVAL   = int(os.getenv("LN_HEALTH_INTERVAL", "30"))
GOSSIP_PORT       = int(os.getenv("LN_GOSSIP_PORT",   "8090"))
SIMULATION_MODE   = os.getenv("LN_SIMULATION", "1") == "1"
DRY_RUN           = os.getenv("LN_DRY_RUN",    "0") == "1"
PEER_EVICT_FAILS  = int(os.getenv("LN_PEER_EVICT_FAILS", "2"))
CYCLE_LOG_EVERY   = max(1, int(os.getenv("LN_CYCLE_LOG_EVERY", "4")))

INSTALL_DIR       = Path(os.getenv("LN_INSTALL_DIR",  "/opt/hdgl"))
STATE_DB          = INSTALL_DIR / "lattice_state.db"
STATE_PKL         = INSTALL_DIR / "lattice_state.pkl"   # legacy — auto-migrated
NODES_FILE        = INSTALL_DIR / "known_nodes.json"

# ── seed nodes (edit or override via known_nodes.json) ────────────────────────
SEED_NODES = []

SERVICE_REGISTRY = {}

# ── imports ───────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from hdgl_state_db import HDGLStateDB
from hdgl_site_config import (
    get_dns_domain_map,
    get_primary_site,
    get_seed_nodes,
    get_service_registry,
    load_site_config,
)

from hdgl_lattice    import HDGLLattice
from hdgl_fileswap   import HDGLFileswap
from hdgl_transport  import HDGLTransportServer
from hdgl_transport_client import HDGLTransportClient
from hdgl_dns        import HDGLResolver

SITE_CONFIG = load_site_config()
SEED_NODES = get_seed_nodes(SITE_CONFIG)
SERVICE_REGISTRY = get_service_registry(SITE_CONFIG)
PRIMARY_SITE = get_primary_site(SITE_CONFIG)
DNS_DOMAIN_MAP = get_dns_domain_map(SITE_CONFIG)


# ── HDGL HOST ─────────────────────────────────────────────────────────────────
class HDGLHost:
    """
    The distributed host. Runs on every node. No master.

    Each instance manages:
      - Its own lattice view (updated from peer /node_info)
      - Its local fileswap (serves authority files, caches proxied ones)
            - Its internal node HTTP server (gossip, /node_info, /serve)
            - Its public native HTTP server (per-request strand routing)
            - Its DNS resolver and persistent lattice state
    """

    def __init__(self):
        # state_db must be opened before _load_known_nodes or _load_or_create_lattice
        self.state_db    = HDGLStateDB(STATE_DB)
        self.state_db.open()
        self.known_nodes: List[str] = self._load_known_nodes()
        self.lattice     = self._load_or_create_lattice()
        self.swap        = HDGLFileswap(self.lattice, local_node=LOCAL_NODE)
        self.transport   = HDGLTransportServer(self.lattice, self.swap, self, local_node=LOCAL_NODE)
        self.transport_client = HDGLTransportClient(local_node=LOCAL_NODE)
        self.resolver    = HDGLResolver(
            self.lattice, DNS_DOMAIN_MAP, LOCAL_NODE,
            port=int(os.getenv("LN_DNS_PORT", "5353"))
        )
        self._running    = True
        self._cycle      = 0
        self._no_healthy = 0
        self._joined     = False
        self._peer_failures: Dict[str, int] = {}
        self._last_cycle_summary: Optional[str] = None

        if SIMULATION_MODE or DRY_RUN:
            self.swap._dry_run_override = True

    def start(self):
        """Boot sequence: state, servers, simulation audit or health loop."""
        log.info(f"{'─'*60}")
        log.info("HDGL Distributed Host starting")
        log.info(f"  Local node   : {LOCAL_NODE}")
        log.info(f"  Public HTTP  : {HTTP_PORT}")
        log.info(f"  Internal API : {NODE_PORT}")
        log.info(
            f"  Mode         : {'SIMULATION' if SIMULATION_MODE else 'LIVE'}"
            f"{' + DRY_RUN' if DRY_RUN else ''}"
        )
        log.info(f"  Known peers  : {self.known_nodes}")
        log.info("  Routing      : phi_tau(path) → strand → authority")
        log.info(f"{'─'*60}")

        self.lattice.update(LOCAL_NODE, 10.0, self._local_storage_gb())

        self.transport.start()
        log.info(f"[host] unified transport live (HDGL frames, strand-routed)")
        self.resolver.start()

        self._boot_encoder_check()

        if SIMULATION_MODE:
            self._run_simulation_audit()
            return

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT,  self._handle_signal)

        try:
            self._health_loop()
        except Exception as e:
            log.error(f"Health loop crashed: {e}", exc_info=True)
            raise
        finally:
            self._shutdown()

    def _shutdown(self):
        """Shut down all services."""
        self._running = False
        if self.transport:
            self.transport.stop()
        if self.transport_client:
            self.transport_client.close_all()
        if self.resolver:
            self.resolver.stop()
        if self.state_db:
            self.state_db.close()
        log.info("[host] shutdown complete")

    # ── HEALTH LOOP ───────────────────────────────────────────────────────────

    def _health_loop(self):
        while self._running:
            cycle_start = time.time()
            self._cycle += 1

            healthy = self._check_peers()
            self._gossip_self(healthy)
            self._provisioner_cycle(healthy)   # NORM→SCALE→ENERGY→FOLD256
            self._rebalance(healthy)
            self._renew_certs()
            self._persist_state()
            self._log_cycle_summary(healthy)

            elapsed = time.time() - cycle_start
            sleep   = max(0, HEALTH_INTERVAL - elapsed)
            if self._running:
                time.sleep(sleep)

    def _check_peers(self) -> List[Dict[str, Any]]:
        """
        Health-check all known nodes via their /node_info endpoint.
        Updates lattice EMA for each live peer.
        Discovers new nodes from peer's known_nodes list.
        """
        healthy   = []
        new_nodes = []

        for node in list(self.known_nodes):
            if node != LOCAL_NODE and not _is_valid_cluster_ip(node):
                log.warning(f"[health] dropping invalid peer address: {node}")
                if node in self.known_nodes:
                    self.known_nodes.remove(node)
                self.lattice._states.pop(node, None)
                self.lattice._latency_ema.pop(node, None)
                self._peer_failures.pop(node, None)
                continue

            ok, info = self._fetch_node_info(node)
            if ok and info.get("health") == "ok":
                self._peer_failures.pop(node, None)
                lat  = info.get("latency", 1000)
                stor = info.get("storage_available_gb", 1.0)

                # EMA feedback from observed latency
                self.lattice.observe_latency(node, lat)
                self.lattice.update(node, lat, stor)

                healthy.append({
                    "node":             node,
                    "latency":          lat,
                    "storage_avail_gb": stor,
                    "fingerprint":      info.get("fingerprint", "0x00000000"),
                    "authority_strands": info.get("authority_strands", []),
                })

                # Gossip: discover new peers
                for peer in info.get("known_nodes", []):
                    if peer != LOCAL_NODE and not _is_valid_cluster_ip(peer):
                        continue
                    if peer not in self.known_nodes and peer not in new_nodes:
                        new_nodes.append(peer)
            else:
                log.warning(f"[health] {node} unreachable or unhealthy")
                fails = self._peer_failures.get(node, 0) + 1
                self._peer_failures[node] = fails
                if node != LOCAL_NODE and fails >= PEER_EVICT_FAILS:
                    log.warning(
                        f"[health] evicting stale peer {node} after {fails} failed checks"
                    )
                    if node in self.known_nodes:
                        self.known_nodes.remove(node)
                    self.lattice._states.pop(node, None)
                    self.lattice._latency_ema.pop(node, None)
                    self._peer_failures.pop(node, None)

        if new_nodes:
            log.info(f"[gossip] discovered {len(new_nodes)} new peer(s): {new_nodes}")
            self.known_nodes.extend(new_nodes)
            self._save_known_nodes()

        if not healthy:
            self._no_healthy += 1
            if self._no_healthy >= 3:
                log.critical(
                    f"[health] NO healthy peers for {self._no_healthy} consecutive cycles. "
                    f"Cluster may be isolated. Check network connectivity."
                )
        else:
            self._no_healthy = 0
            if not self._joined:
                self._joined = True
                log.info(f"[host] cluster joined — {len(healthy)} peer(s) healthy")

        return healthy

    def _recv_gossip(self, gossip_data: Dict[str, Any], peer_ip: str):
        """
        Receive and process incoming gossip from a peer (via HDGL_MSG_GOSSIP frame).
        Updates lattice with peer's announced state.
        """
        try:
            node = gossip_data.get("node", peer_ip)
            lat = gossip_data.get("latency", 50)
            stor = gossip_data.get("storage_available_gb", 1.0)

            # Update lattice with peer's state
            self.lattice.observe_latency(node, lat)
            self.lattice.update(node, lat, stor)

            # Discover new peers
            for peer in gossip_data.get("known_nodes", []):
                if peer != LOCAL_NODE and _is_valid_cluster_ip(peer):
                    if peer not in self.known_nodes:
                        log.debug(f"[gossip] discovered new peer {peer} from {node}")
                        self.known_nodes.append(peer)
                        self._save_known_nodes()
        except Exception as e:
            log.debug(f"[gossip] recv error from {peer_ip}: {e}")

    def _gossip_self(self, healthy: List[Dict]):
        """
        Announce this node's state to all healthy peers via HDGL_MSG_GOSSIP frames.
        Unified transport replaces separate /gossip HTTP endpoints.
        """
        if DRY_RUN:
            return

        gossip_data = {
            "node":                LOCAL_NODE,
            "latency":             self.lattice._latency_ema.get(LOCAL_NODE, 50),
            "storage_available_gb": self._local_storage_gb(),
            "fingerprint":         self.lattice.fingerprint(LOCAL_NODE),
            "known_nodes":         self.known_nodes,
            "authority_strands":   [chr(65+k) for k, (n, _) in self.lattice.top_node_per_strand().items() if n == LOCAL_NODE],
        }

        for n in healthy:
            peer = n["node"]
            if peer == LOCAL_NODE:
                continue
            try:
                self.transport_client.send_gossip(peer, gossip_data)
            except Exception as e:
                log.debug(f"[gossip] {peer}: {e}")

    def _provisioner_cycle(self, healthy: List[Dict]) -> None:
        """
        Run one provisioner pass per healthy node each cycle.
        Derived from hdgl_executor2.py NORM→SCALE→PHASESHIFT→OMEGAMULT→ENERGY→FOLD256.

        The energy scalar provides a self-calibrating upstream weight that
        reflects actual slot excitation rather than fixed amplification constants.
        Logged as part of the cycle summary for observability.
        """
        from hdgl_lattice import run_provisioner, ProvisionerResult
        self._last_provisioner: dict = {}

        for n in healthy:
            nid = n["node"]
            try:
                result = self.lattice.provisioner_pass(nid)
                self._last_provisioner[nid] = result
                log.debug(
                    f"[provisioner] {nid}  energy={result.energy:.3e}  "
                    f"fold={result.folded_weight:.4f}  "
                    f"norm_max={result.norm_max:.4f}"
                )
            except Exception as e:
                log.debug(f"[provisioner] {nid} error: {e}")

    def _rebalance(self, healthy: List[Dict]):
        """Run fileswap rebalance — migrate files if strand authority shifted."""
        try:
            self.swap.rebalance()
        except Exception as e:
            log.error(f"[rebalance] error: {e}", exc_info=True)

    def _update_nginx(self, healthy: List[Dict]):
        """Compatibility shim for v0.3. Keep DNS domain map current."""
        self.resolver.update_domain_map(get_dns_domain_map(SITE_CONFIG))

    def _renew_certs(self):
        """Run certbot renewal (once per day max via systemd timer or here)."""
        if DRY_RUN:
            return
        # Disable if LN_CERTBOT_ENABLED=0 (e.g. running as non-root deployuser)
        if os.getenv("LN_CERTBOT_ENABLED", "1") == "0":
            return
        # Only attempt on cycle 1 and every 2880 cycles (~1 day at 30s intervals)
        if self._cycle == 1 or self._cycle % 2880 == 0:
            try:
                result = subprocess.run(
                    ["certbot", "renew", "--quiet"],
                    capture_output=True, text=True, timeout=120
                )
                if result.stdout.strip():
                    log.info(f"[certbot] {result.stdout.strip()}")
                if result.returncode != 0 and result.stderr:
                    log.warning(f"[certbot] {result.stderr.strip()}")
            except Exception as e:
                log.warning(f"[certbot] {e}")

    def _log_cycle_summary(self, healthy: List[Dict]):
        """Log cycle summary with cluster fingerprint and authority map."""
        top = self.lattice.top_node_per_strand()
        cfp = self.lattice.cluster_fingerprint()
        target_match = bin(~(int(cfp, 16) ^ 0xFFFF0000) & 0xFFFFFFFF).count("1")

        # Which strands does this node own?
        my_strands = [
            chr(65 + k) for k, (n, _) in top.items() if n == LOCAL_NODE
        ]

        # Provisioner energy summary
        prov = getattr(self, "_last_provisioner", {})
        if prov:
            energies = [r.energy for r in prov.values()]
            avg_energy = sum(energies)/len(energies) if energies else 0
            prov_str = f"  energy={avg_energy:.2e}"
        else:
            prov_str = ""

        summary = (
            f"peers={len(healthy)}/{len(self.known_nodes)}  "
            f"cluster={cfp}  "
            f"fp_match={target_match}/32  "
            f"my_strands={my_strands or 'none'}{prov_str}"
        )

        # Keep logs compact: emit every N cycles, or immediately on topology/fingerprint change.
        changed = summary != self._last_cycle_summary
        if changed or self._cycle % CYCLE_LOG_EVERY == 0:
            log.info(f"[cycle {self._cycle}] {summary}")
            self._last_cycle_summary = summary

    # ── SIMULATION / AUDIT ────────────────────────────────────────────────────

    def _boot_encoder_check(self):
        from hdgl_fileswap import _phi_tau, _strand_for_path, _omega_ttl
        log.info("── Boot encoder check ──────────────────────────────")
        probe_paths = list(PRIMARY_SITE.get("storage_paths", []))
        probe_paths.extend(f"/{svc}/" for svc in SERVICE_REGISTRY)
        if not probe_paths:
            probe_paths = ["/storage/"]
        for path in probe_paths:
            from hdgl_fileswap import _phi_tau as pt
            tau    = pt(path)
            strand = _strand_for_path(path)
            ttl    = _omega_ttl(strand)
            auth   = self.lattice.top_node_per_strand().get(strand, (LOCAL_NODE, 0))[0]
            log.info(f"  {path:<25} τ={tau:.3f}  strand={strand}  "
                     f"TTL={ttl:.0f}s  auth={auth}")

    def _run_simulation_audit(self):
        """Full matrix audit — runs when SIMULATION_MODE=1."""
        dummy_nodes = [
            {"node": n, "latency": 50 + i*30, "storage_avail_gb": 1.0 + i}
            for i, n in enumerate(self.known_nodes[:4] or [LOCAL_NODE])
        ]
        for n in dummy_nodes:
            self.lattice.update(n["node"], n["latency"], n["storage_avail_gb"])

        log.info("── Lattice simulation matrix ───────────────────────")
        matrix = self.lattice.simulation_matrix(dummy_nodes, SERVICE_REGISTRY)
        print(matrix)

        log.info("── Fileswap simulation matrix ──────────────────────")
        for svc in SERVICE_REGISTRY:
            self.swap.write(f"/{svc}/config.json",
                            f'{{"service":"{svc}"}}'.encode())
        self.swap._dry_run_override = True
        print(self.swap.simulation_matrix())

        log.info("── Native HTTP routing preview ─────────────────────")
        sample_paths = list(PRIMARY_SITE.get("storage_paths", [])) or ["/storage/"]
        sample_paths.extend(f"/{svc}/config.json" for svc in SERVICE_REGISTRY)
        for path in sample_paths[:8]:
            from hdgl_fileswap import _phi_tau, _strand_for_path, _omega_ttl
            tau = _phi_tau(path)
            strand = _strand_for_path(path)
            authority, weight = self.lattice.top_node_per_strand()[strand]
            log.info(
                f"  {path:<28} τ={tau:.3f}  strand={strand}  "
                f"auth={authority}  weight={weight:.5f}  TTL={_omega_ttl(strand):.0f}s"
            )

        log.info("── Strand authority map ────────────────────────────")
        top = self.lattice.top_node_per_strand()
        for k, (node, weight) in top.items():
            from hdgl_fileswap import STRAND_GEOMETRY, _omega_ttl
            _, _, poly = STRAND_GEOMETRY[k]
            log.info(f"  Strand {k} ({chr(65+k)}) {poly:<14}: "
                     f"authority={node}  weight={weight:.5f}  "
                     f"TTL={_omega_ttl(k):.0f}s")

        log.info("── DNS strand map ──────────────────────────────────")
        for entry in self.resolver.strand_map():
            log.info(
                f"  {entry['domain']:<28}  strand={entry['strand']}({entry['label']})  "
                f"τ={entry['tau']}  TTL={entry['ttl_s']}s  auth={entry['authority']}"
            )
        log.info("── Ready to go live ────────────────────────────────")
        log.info("  Set LN_SIMULATION=0 LN_DRY_RUN=0 and restart")

    # ── HELPERS ───────────────────────────────────────────────────────────────

    def _fetch_node_info(self, node: str,
                          retries: int = 3,
                          backoff: float = 1.5) -> tuple:
        """Fetch node info via HDGL_MSG_INFO frame."""
        delay = 1.0
        for attempt in range(retries):
            try:
                info = self.transport_client.send_info_query(node)
                if info is not None:
                    # Merge in stored peer info to include known_nodes, fingerprint, etc.
                    # Transport returns basic ack; combine with latest gossip state
                    info.update({
                        "health": "ok",
                        "latency": info.get("latency", 50),
                        "storage_available_gb": info.get("storage_available_gb", 1.0),
                    })
                    return True, info
            except Exception as e:
                log.debug(f"[health] {node} attempt {attempt+1}: {e}")
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= backoff
        return False, None

    def _local_storage_gb(self) -> float:
        try:
            from hdgl_fileswap import SWAP_ROOT
            st = os.statvfs(str(SWAP_ROOT))
            return round(st.f_bavail * st.f_frsize / 1e9, 2)
        except Exception:
            return 10.0   # default if path doesn't exist yet

    def _load_known_nodes(self) -> List[str]:
        def _sanitize(nodes: List[str]) -> List[str]:
            out: List[str] = []
            for node in nodes:
                if _is_valid_cluster_ip(node):
                    if node not in out:
                        out.append(node)
            return out

        # Try SQLite first
        db_nodes = self.state_db.load_known_nodes()
        if db_nodes:
            log.info(f"[state] loaded {len(db_nodes)} known nodes from DB")
            return _sanitize([LOCAL_NODE] + SEED_NODES + db_nodes)
        # Legacy JSON fallback
        if NODES_FILE.exists():
            try:
                nodes = json.loads(NODES_FILE.read_text())
                log.info(f"[state] loaded {len(nodes)} known nodes from JSON (legacy)")
                return _sanitize([LOCAL_NODE] + SEED_NODES + nodes)
            except Exception as e:
                log.warning(f"[state] could not load known_nodes.json: {e}")
        return _sanitize([LOCAL_NODE] + SEED_NODES)

    def _save_known_nodes(self):
        try:
            self.known_nodes = [n for n in self.known_nodes if _is_valid_cluster_ip(n)]
            self.state_db.save_known_nodes(self.known_nodes)
        except Exception as e:
            log.warning(f"[state] could not save known_nodes: {e}")

    def _load_or_create_lattice(self) -> HDGLLattice:
        lat = HDGLLattice()
        # One-time pickle migration (renames .pkl to .pkl.migrated after)
        if STATE_PKL.exists():
            if self.state_db.migrate_from_pickle(STATE_PKL):
                log.info("[state] pickle -> SQLite migration complete")
        # Load EMA from SQLite
        ema = self.state_db.load_ema()
        if ema:
            lat._latency_ema = ema
            log.info(f"[state] loaded lattice EMA for {len(ema)} nodes")
        return lat

    def _persist_state(self):
        """
        Persist EMA and known_nodes to SQLite.
        Stale pruning (24h TTL) is handled inside HDGLStateDB automatically.
        """
        try:
            self.state_db.save_ema(self.lattice._latency_ema)
        except Exception as e:
            log.warning(f"[state] EMA persist failed: {e}")
        self._save_known_nodes()

    def _handle_signal(self, signum, frame):
        log.info(f"[host] signal {signum} received — shutting down")
        self._running = False

# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Make install dir if running from source
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    host = HDGLHost()
    host.start()
