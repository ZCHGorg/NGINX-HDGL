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
             hdgl_ingress.py   : NGINX config generator (strand upstreams)

Each cycle:
  1. Health-check all known peers via /node_info
  2. Update EMA feedback for each live peer
  3. Gossip this node's info to all peers (so they update their lattice)
  4. Run fileswap rebalance (migrate files if strand authority shifted)
  5. Regenerate NGINX config with current strand weights
  6. Log cluster fingerprint, alive nodes, authority map

Boot sequence:
  1. Load lattice state from disk (if any)
  2. Start node HTTP server (immediately live)
  3. Begin health loop
  4. On first successful peer contact: emit "cluster joined" log
  5. In SIMULATION_MODE: print matrix, skip SSH/NGINX/SCP

Usage:
  python3 hdgl_host.py                  # uses env vars
  LN_LOCAL_NODE=209.159.159.170 \\
  LN_SIMULATION=0 LN_DRY_RUN=0 \\
    python3 hdgl_host.py
"""

import json
import logging
import os
import sys
import time
import signal
import pickle
import threading
import subprocess
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

LOCAL_NODE        = os.getenv("LN_LOCAL_NODE",
                               _socket.gethostbyname(_socket.gethostname()))
SSH_USER          = os.getenv("LN_SSH_USER",          "deployuser")
NODE_PORT         = int(os.getenv("LN_NODE_PORT",     "8090"))
HEALTH_INTERVAL   = int(os.getenv("LN_HEALTH_INTERVAL", "30"))
GOSSIP_PORT       = int(os.getenv("LN_GOSSIP_PORT",   "8090"))
SIMULATION_MODE   = os.getenv("LN_SIMULATION", "1") == "1"
DRY_RUN           = os.getenv("LN_DRY_RUN",    "0") == "1"

INSTALL_DIR       = Path(os.getenv("LN_INSTALL_DIR",  "/opt/hdgl"))
STATE_FILE        = INSTALL_DIR / "lattice_state.pkl"
STATE_DB          = INSTALL_DIR / "lattice_state.db"
STATE_PKL         = INSTALL_DIR / "lattice_state.pkl"   # legacy — auto-migrated
NODES_FILE        = INSTALL_DIR / "known_nodes.json"

# ── seed nodes (edit or override via known_nodes.json) ────────────────────────
SEED_NODES = [
    "209.159.159.170",
    "209.159.159.171",
]

SERVICE_REGISTRY = {
    "wecharg":         {"port": 8083, "domain": "wecharg.com"},
    "stealthmachines": {"port": 8080, "domain": "stealthmachines.com"},
    "josefkulovany":   {"port": 8081, "domain": "josefkulovany.com"},
}

# ── imports ───────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from hdgl_state_db import HDGLStateDB

from hdgl_lattice    import HDGLLattice
from hdgl_fileswap   import HDGLFileswap
from hdgl_node_server import HDGLNodeServer
from hdgl_ingress    import generate_nginx_conf, write_nginx_conf
from hdgl_dns        import HDGLResolver


# ── HDGL HOST ─────────────────────────────────────────────────────────────────
class HDGLHost:
    """
    The distributed host. Runs on every node. No master.

    Each instance manages:
      - Its own lattice view (updated from peer /node_info)
      - Its local fileswap (serves authority files, caches proxied ones)
      - Its node HTTP server (handles incoming requests)
      - Its NGINX config (regenerated each cycle with current weights)
    """

    def __init__(self):
        # state_db must be opened before _load_known_nodes or _load_or_create_lattice
        self.state_db    = HDGLStateDB(STATE_DB)
        self.state_db.open()
        self.known_nodes: List[str] = self._load_known_nodes()
        self.lattice     = self._load_or_create_lattice()
        self.swap        = HDGLFileswap(self.lattice, local_node=LOCAL_NODE)
        self.node_server = HDGLNodeServer(self.lattice, self.swap, port=NODE_PORT)
        # Build domain-keyed map for DNS resolver
        # SERVICE_REGISTRY is keyed by svc_name; DNS needs domain as key
        _dns_map = {
            v.get("domain", k): v
            for k, v in SERVICE_REGISTRY.items()
            if v.get("domain")
        }
        # Also add zchg.org directly
        _dns_map["zchg.org"] = {"port": 443}
        self.resolver    = HDGLResolver(
            self.lattice, _dns_map, LOCAL_NODE,
            port=int(os.getenv("LN_DNS_PORT", "5353"))
        )
        self._running    = True
        self._cycle      = 0
        self._no_healthy = 0
        self._joined     = False

        if SIMULATION_MODE or DRY_RUN:
            self.swap._dry_run_override = True

    # ── BOOT ──────────────────────────────────────────────────────────────────

    def start(self):
        """Boot sequence: state, server, loop."""
        log.info(f"{'─'*60}")
        log.info(f"HDGL Distributed Host starting")
        log.info(f"  Local node : {LOCAL_NODE}:{NODE_PORT}")
        log.info(f"  Mode       : {'SIMULATION' if SIMULATION_MODE else 'LIVE'}"
                 f"{' + DRY_RUN' if DRY_RUN else ''}")
        log.info(f"  Known peers: {self.known_nodes}")
        log.info(f"{'─'*60}")

        # Seed local node into lattice
        self.lattice.update(LOCAL_NODE, 10.0, self._local_storage_gb())

        # Start HTTP server and DNS resolver immediately
        self.node_server.start()
        log.info(f"Node server live on :{NODE_PORT}")
        self.resolver.start()

        # Boot encoder check
        self._boot_encoder_check()

        # Simulation audit
        if SIMULATION_MODE:
            self._run_simulation_audit()
            return

        # Signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT,  self._handle_signal)

        # Main loop
        try:
            self._health_loop()
        except Exception as e:
            log.error(f"Health loop crashed: {e}", exc_info=True)
        finally:
            self._shutdown()

    # ── HEALTH LOOP ───────────────────────────────────────────────────────────

    def _health_loop(self):
        while self._running:
            cycle_start = time.time()
            self._cycle += 1

            healthy = self._check_peers()
            self._gossip_self(healthy)
            self._provisioner_cycle(healthy)   # NORM→SCALE→ENERGY→FOLD256
            self._rebalance(healthy)
            self._update_nginx(healthy)
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
            ok, info = self._fetch_node_info(node)
            if ok and info.get("health") == "ok":
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
                    if peer not in self.known_nodes and peer not in new_nodes:
                        new_nodes.append(peer)
            else:
                log.warning(f"[health] {node} unreachable or unhealthy")

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

    def _gossip_self(self, healthy: List[Dict]):
        """
        Announce this node's state to all healthy peers using binary protocol.
        Binary: 16 bytes vs 104 bytes JSON = 83% reduction per gossip POST.
        Falls back to JSON if binary import unavailable.
        """
        if DRY_RUN:
            return

        try:
            from hdgl_fileswap import encode_gossip
            payload = encode_gossip(
                node_ip     = LOCAL_NODE,
                latency_ms  = self.lattice._latency_ema.get(LOCAL_NODE, 50),
                storage_gb  = self._local_storage_gb(),
                fingerprint = self.lattice.fingerprint(LOCAL_NODE),
            )
            content_type = "application/octet-stream"
        except Exception:
            # JSON fallback
            payload = json.dumps({
                "node":                LOCAL_NODE,
                "latency":             self.lattice._latency_ema.get(LOCAL_NODE, 50),
                "storage_available_gb": self._local_storage_gb(),
                "fingerprint":         self.lattice.fingerprint(LOCAL_NODE),
            }).encode()
            content_type = "application/json"

        for n in healthy:
            peer = n["node"]
            if peer == LOCAL_NODE:
                continue
            try:
                from hdgl_node_server import _sign_payload, _HMAC_HEADER
                sig = _sign_payload(payload)
                headers = {"Content-Type": content_type}
                if sig:
                    headers[_HMAC_HEADER] = sig
                requests.post(
                    f"http://{peer}:{NODE_PORT}/gossip",
                    data=payload,
                    headers=headers,
                    timeout=2,
                )
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
        """Regenerate NGINX config with current strand weights and reload."""
        if not healthy:
            log.warning("[nginx] skipping update — no healthy nodes")
            return
        try:
            conf = generate_nginx_conf(
                healthy_nodes=healthy,
                service_registry=SERVICE_REGISTRY,
                lattice=self.lattice,
                local_node=LOCAL_NODE,
            )
            write_nginx_conf(conf, dry_run=DRY_RUN)
            # Hot-update DNS resolver with current domain map each cycle
            _dns_map_upd = {
                v.get("domain", k): v
                for k, v in SERVICE_REGISTRY.items() if v.get("domain")
            }
            _dns_map_upd["zchg.org"] = {"port": 443}
            self.resolver.update_domain_map(_dns_map_upd)
        except Exception as e:
            log.error(f"[nginx] update failed: {e}", exc_info=True)

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

        log.info(
            f"[cycle {self._cycle}] "
            f"peers={len(healthy)}/{len(self.known_nodes)}  "
            f"cluster={cfp}  "
            f"fp_match={target_match}/32  "
            f"my_strands={my_strands or 'none'}"            f"{prov_str}"
        )

    # ── SIMULATION / AUDIT ────────────────────────────────────────────────────

    def _boot_encoder_check(self):
        from hdgl_fileswap import _phi_tau, _strand_for_path, _omega_ttl
        log.info("── Boot encoder check ──────────────────────────────")
        for path in ["/hott/", "/watt/", "/wecharg/", "/stealthmachines/"]:
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

        log.info("── NGINX config preview ────────────────────────────")
        conf = generate_nginx_conf(dummy_nodes, SERVICE_REGISTRY,
                                   self.lattice, LOCAL_NODE)
        # Print just the upstream section for brevity
        upstream_section = "\n".join(
            l for l in conf.split("\n")
            if any(x in l for x in ["upstream", "server ", "weight=", "# Strand", "# HDGL"])
        )[:2000]
        print(upstream_section)
        log.info(f"Full config: {len(conf)} chars")

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
        delay = 1.0
        for attempt in range(retries):
            try:
                r = requests.get(
                    f"http://{node}:{NODE_PORT}/node_info",
                    timeout=2
                )
                if r.status_code == 200:
                    try:
                        return True, r.json()
                    except ValueError as e:
                        log.warning(f"[health] {node} malformed JSON: {e}")
            except requests.RequestException as e:
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
        # Try SQLite first
        db_nodes = self.state_db.load_known_nodes()
        if db_nodes:
            log.info(f"[state] loaded {len(db_nodes)} known nodes from DB")
            for s in SEED_NODES + [LOCAL_NODE]:
                if s not in db_nodes:
                    db_nodes.insert(0, s)
            return db_nodes
        # Legacy JSON fallback
        if NODES_FILE.exists():
            try:
                nodes = json.loads(NODES_FILE.read_text())
                log.info(f"[state] loaded {len(nodes)} known nodes from JSON (legacy)")
                return nodes
            except Exception as e:
                log.warning(f"[state] could not load known_nodes.json: {e}")
        return list(set(SEED_NODES + [LOCAL_NODE]))

    def _save_known_nodes(self):
        try:
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

    def _shutdown(self):
        log.info("[host] shutting down node server and DNS resolver")
        self.node_server.stop()
        self.resolver.stop()
        self._persist_state()
        self.state_db.close()
        log.info("[host] shutdown complete")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Make install dir if running from source
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    host = HDGLHost()
    host.start()
