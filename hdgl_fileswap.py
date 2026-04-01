#!/usr/bin/env python3
"""
hdgl_fileswap.py
----------------
Analog-Over-Digital Fileswap — third module in the HDGL stack.

Files are not addressed by path or IP. They are addressed by HDGL lattice
position: phi_tau(path) → octave k → strand k → top_node_per_strand(k).

The analog layer (HDGLLattice) decides WHERE and WHEN.
The digital layer (scp / HTTP) moves the actual bytes.

Three operations:
  swap_write(path, data)   → write to authoritative node, propagate checksum
  swap_read(path)          → serve from authority or fetch+cache with Ω-TTL
  swap_rebalance()         → migrate files when strand authority shifts

TTL from spec: Ω_k = 1 / (φ^k)^7
  Strand A (k=0): Ω ≈ 8.12e-9 → long TTL (stable, root-level files)
  Strand H (k=7): Ω ≈ 2.81e-10 → short TTL (volatile, deep paths)
  TTL in seconds = Ω_k × TTL_SCALE (configurable)

Authority routing:
  Local node is authoritative if it is top_node for the file's strand.
  All other nodes hold cached copies with Ω-derived expiry.

Usage:
    from hdgl_lattice import HDGLLattice
    from hdgl_fileswap import HDGLFileswap

    lattice = HDGLLattice()
    swap    = HDGLFileswap(lattice, local_node="209.159.159.170")

    swap.write("/wecharg/config.json", b"{...}")
    data = swap.read("/wecharg/config.json")
    swap.rebalance()   # call each daemon heal cycle
"""

import hashlib
import json
import logging
import math
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Moiré encoding — lazy import to avoid circular dependency
try:
    from hdgl_moire import HDGLMoire as _HDGLMoire
    _MOIRE_AVAILABLE = True
except ImportError:
    _HDGLMoire = None
    _MOIRE_AVAILABLE = False

import requests

log = logging.getLogger(__name__)

# ----------------------------
# CONSTANTS
# ----------------------------
PHI          = (1 + math.sqrt(5)) / 2
NUM_STRANDS  = 8

# Ω_k = 1 / (φ^(k+1))^7  — per-strand tension (k is 0-indexed)
OMEGA = [1.0 / (PHI ** ((k + 1) * 7)) for k in range(NUM_STRANDS)]

# -------------------------------------------------------
# SPIRAL GEOMETRY — spiral3.py + dna_closed_interact.py
# github.com/stealthmachines/spiral8plus
# -------------------------------------------------------
SPIRAL_PERIOD  = 13.057          # natural rebalance window (seconds)
SPEED_FACTOR   = 2.0             # strands to pre-warm ahead

# Double-strand counter-rotation (dna_closed_interact.py)
# Primary strand: +golden_angle[k]  → authoritative node
# Mirror strand:  -golden_angle[k]  → echo/fallback node
# The two strands diverge by 2×angle, ensuring quasi-periodic non-overlap
_GOLDEN_DEG_DNA = 360.0 / (PHI ** 2)
STRAND_ANGLES_POS = [ i * _GOLDEN_DEG_DNA for i in range(8)]   # primary
STRAND_ANGLES_NEG = [-i * _GOLDEN_DEG_DNA for i in range(8)]   # mirror

# Echo scale factor = 0.8 (matches MEMORY_DECAY in original daemon)
# Each strand keeps an 80%-scale fallback copy from the previous strand's authority
ECHO_SCALE = 0.8

# Closed lattice topology split (dna_closed_interact.py)
# Low-D  (verts <= 4): complete graph — every node connected to every other
# High-D (verts >  4): grid lattice — sqrt(verts) × sqrt(verts) connectivity
LOW_D_THRESHOLD = 4

def strand_topology(strand_idx: int) -> str:
    """'complete' for low-D strands, 'grid' for high-D strands."""
    verts = STRAND_GEOMETRY[strand_idx][1]
    return "complete" if verts <= LOW_D_THRESHOLD else "grid"

def strand_grid_size(strand_idx: int) -> int:
    """Grid dimension for high-D strands: ceil(sqrt(verts))."""
    import math as _math
    verts = STRAND_GEOMETRY[strand_idx][1]
    return int(_math.ceil(_math.sqrt(verts)))

# Inter-rung link sample count (dna_closed_interact.py uses 5)
# Controls how many cross-strand migration paths exist between consecutive strands
INTER_RUNG_LINKS = 5

# Per-strand polytope geometry
# (alpha, vertex_count, polytope_name)
# alpha > 0 → expanding spiral → VOLATILE → shorter TTL
# alpha < 0 → contracting spiral → STABLE  → longer TTL
STRAND_GEOMETRY = [
    ( 0.015269,  1, "Point"),        # A
    ( 0.008262,  2, "Line"),         # B
    ( 0.110649,  3, "Triangle"),     # C
    (-0.083485,  4, "Tetrahedron"),  # D  ← contracting: STABLE
    ( 0.025847,  5, "Pentachoron"),  # E
    (-0.045123, 12, "Hexacross"),    # F  ← contracting: STABLE
    ( 0.067891, 14, "Heptacube"),    # G
    ( 0.012345, 16, "Octacube"),     # H
]

# Golden angle per strand (cumulative, degrees)
_GOLDEN_DEG  = 360.0 / (PHI ** 2)   # 137.508°
STRAND_ANGLES = [i * _GOLDEN_DEG for i in range(NUM_STRANDS)]

# Replication factor per strand: min(vertex_count, cluster_size)
# Higher vertex count → more nodes should hold copies
def strand_replication(strand_idx: int, cluster_size: int) -> int:
    verts = STRAND_GEOMETRY[strand_idx][1]
    return max(1, min(verts, cluster_size))

# Alpha-aware TTL: contracting strands get longer TTL, expanding get shorter
# Base formula: TTL_k = TTL_BASE × exp(-alpha_k × SPIRAL_PERIOD)
# Contracting (negative alpha) → exp(positive) → multiplier > 1 → longer TTL
# Expanding  (positive alpha) → exp(negative) → multiplier < 1 → shorter TTL
def alpha_ttl(strand_idx: int, base_ttl: float) -> float:
    alpha = STRAND_GEOMETRY[strand_idx][0]
    multiplier = math.exp(-alpha * SPIRAL_PERIOD)
    return max(base_ttl * multiplier, 0.5)   # floor at 0.5s

# TTL per strand — φ-geometric decay: TTL_k = BASE × φ^(-k×2.5)
# Strand A (k=0): 3600s (1hr)  — stable root-level files
# Strand H (k=7): ~0.8s        — highly volatile deep paths
# Override base with LN_FILESWAP_TTL_BASE (seconds, default 3600)
TTL_BASE  = float(os.getenv("LN_FILESWAP_TTL_BASE", "3600"))
TTL_DECAY = 2.5   # φ^(-k*2.5) per strand step
TTL_SCALE = TTL_BASE   # kept for compatibility

# Where swap files live on each node
SWAP_ROOT    = Path(os.getenv("LN_FILESWAP_ROOT", "/opt/hdgl_swap"))
CACHE_ROOT   = Path(os.getenv("LN_FILESWAP_CACHE", "/opt/hdgl_cache"))

# HTTP port nodes expose for swap reads (served by daemon's Flask or any static server)
SWAP_HTTP_PORT = int(os.getenv("LN_FILESWAP_HTTP_PORT", "8090"))

SSH_USER     = os.getenv("LN_SSH_USER", "deployuser")
DRY_RUN      = os.getenv("LN_DRY_RUN", "0") == "1"


# ----------------------------
# HELPERS
# ----------------------------
def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _omega_ttl(strand_idx: int) -> float:
    """
    Alpha-aware TTL from spiral geometry.

    TTL_k = TTL_BASE × exp(-alpha_k × SPIRAL_PERIOD)

    Contracting strands (alpha < 0, D=Tetrahedron, F=Hexacross):
      exp(-negative × period) = exp(positive) → multiplier > 1 → LONGER TTL
      These are geometrically stable — their files persist.

    Expanding strands (alpha > 0, most strands):
      exp(-positive × period) = exp(negative) → multiplier < 1 → SHORTER TTL
      These are geometrically volatile — their files expire quickly.

    Notable: strand C (Triangle, alpha=+0.11) has the highest positive alpha
    → shortest TTL among expanding strands (most volatile).
    Strand D (Tetrahedron, alpha=-0.083) is the most stable.
    """
    return alpha_ttl(strand_idx, TTL_BASE)


def _phi_tau(path: str) -> float:
    """Map URL path → continuous τ (inline, no circular import)."""
    segments = [s for s in path.strip("/").split("/") if s]
    tau = 0.0
    for depth, seg in enumerate(segments):
        intra = (sum(ord(c) for c in seg) % 1000) / 1000.0
        tau  += (PHI ** depth) * (depth + intra)
    return tau


def _strand_for_path(path: str) -> int:
    """Encode path → HDGL strand index (0–7)."""
    tau = _phi_tau(path)
    return min(int(tau), NUM_STRANDS - 1)


def _run_local(cmd: str) -> Tuple[bool, str]:
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.returncode == 0, result.stdout + result.stderr


def _scp_to(local_path: Path, remote_path: str, node: str) -> bool:
    if DRY_RUN:
        log.info(f"[DRY-RUN] scp {local_path} → {node}:{remote_path}")
        return True
    for attempt in range(2):
        res = subprocess.run(
            f"scp {local_path} {SSH_USER}@{node}:{remote_path}",
            shell=True, capture_output=True
        )
        if res.returncode == 0:
            return True
        log.warning(f"scp attempt {attempt+1} failed → {node}:{remote_path}")
        time.sleep(1)
    return False


def _fetch_from(node: str, swap_path: str) -> Optional[bytes]:
    """HTTP fetch of a swap file from a remote node."""
    url = f"http://{node}:{SWAP_HTTP_PORT}/swap{swap_path}"
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            return r.content
    except requests.RequestException as e:
        log.warning(f"Fetch from {node} failed: {e}")
    return None


# ----------------------------
# BINARY PROTOCOL  (derived from turingfold1.py + fold_hdgl_full4.py)
# ----------------------------
# Replaces JSON gossip (104 bytes) and JSON route table (~97 bytes/route)
# with packed binary structs. No compression needed — format change alone
# achieves 83% gossip reduction and 82% route table reduction.
#
# Gossip wire format:
#   4s  node IP octets
#   f   latency_ms
#   f   storage_gb
#   I   fingerprint (uint32)
#   = 16 bytes  (vs 104 bytes JSON)
#
# Route wire format:
#   d   phi_tau (float64)
#   B   strand_idx (uint8)
#   4s  authority IP octets
#   4s  checksum first 4 bytes
#   f   updated_at (float32 epoch, sufficient for age tracking)
#   = 17 bytes per route  (vs ~97 bytes JSON)

import struct as _struct

_GOSSIP_FMT  = "<4sffI"   # IP(4) + latency(f) + storage(f) + fingerprint(I)
_GOSSIP_SIZE = _struct.calcsize(_GOSSIP_FMT)   # 16

_ROUTE_FMT   = "<dB4s4sf"  # tau(d) + strand(B) + auth_ip(4s) + chk4(4s) + ts(f)
_ROUTE_SIZE  = _struct.calcsize(_ROUTE_FMT)    # 21

_ROUTES_MAGIC = b"HDGL"    # 4-byte magic for binary route files
_ROUTES_VER   = 1           # format version


def ip_to_bytes(ip: str) -> bytes:
    """Pack IPv4 string to 4 bytes. Falls back to zeros for non-IPv4."""
    try:
        return bytes(int(x) for x in ip.split("."))
    except Exception:
        return b"\x00" * 4


def bytes_to_ip(b: bytes) -> str:
    """Unpack 4 bytes to IPv4 string."""
    return ".".join(str(x) for x in b)


def encode_gossip(node_ip: str, latency_ms: float,
                  storage_gb: float, fingerprint: str) -> bytes:
    """
    Pack node health announcement to 16-byte binary struct.
    Input fingerprint is hex string like '0xFFFF0000'.
    """
    fp_int = int(fingerprint, 16) if isinstance(fingerprint, str) else int(fingerprint)
    return _struct.pack(_GOSSIP_FMT,
                        ip_to_bytes(node_ip),
                        float(latency_ms),
                        float(storage_gb),
                        fp_int & 0xFFFFFFFF)


def decode_gossip(data: bytes) -> dict:
    """Unpack 16-byte gossip struct to dict compatible with node_info format."""
    if len(data) < _GOSSIP_SIZE:
        raise ValueError(f"Gossip too short: {len(data)} < {_GOSSIP_SIZE}")
    ip_b, lat, stor, fp = _struct.unpack_from(_GOSSIP_FMT, data)
    return {
        "node":                ip_b.rstrip(b"\x00"),   # keep as bytes for now
        "node_str":            bytes_to_ip(ip_b),
        "latency":             round(lat, 2),
        "storage_available_gb": round(stor, 2),
        "fingerprint":         f"0x{fp:08X}",
        "health":              "ok",
    }


def encode_routes(routes: dict) -> bytes:
    """
    Pack route table to binary.  routes is {path: SwapRoute}.
    Format: MAGIC(4) + VERSION(1) + COUNT(4) + N × ROUTE_RECORD
    Each ROUTE_RECORD: tau(d8) + strand(B1) + auth_ip(4s) + chk4(4s) + ts(f4)
                     + path_len(H2) + path_bytes(N)
    """
    records = bytearray()
    count   = 0
    for path, route in routes.items():
        try:
            tau      = _phi_tau(path)
            auth_ip  = ip_to_bytes(route.authority)
            chk4     = bytes.fromhex(route.checksum[:8].ljust(8, "0"))[:4]
            ts       = float(route.updated_at)
            path_enc = path.encode("utf-8")
            records += _struct.pack("<dB4s4sf", tau, route.strand_idx,
                                    auth_ip, chk4, ts)
            records += _struct.pack("<H", len(path_enc))
            records += path_enc
            count   += 1
        except Exception:
            continue   # skip malformed routes silently

    header = (_ROUTES_MAGIC +
              _struct.pack("<BI", _ROUTES_VER, count))
    return bytes(header) + bytes(records)


def decode_routes(data: bytes) -> dict:
    """
    Unpack binary route table to {path: dict} compatible with _load_routes().
    """
    if len(data) < 9 or data[:4] != _ROUTES_MAGIC:
        raise ValueError("Not a valid HDGL binary route file")

    version, count = _struct.unpack_from("<BI", data, 4)
    ptr    = 9
    routes = {}

    for _ in range(count):
        if ptr + _ROUTE_SIZE + 2 > len(data):
            break
        tau, strand, auth_ip, chk4, ts = _struct.unpack_from("<dB4s4sf", data, ptr)
        ptr += _ROUTE_SIZE
        path_len, = _struct.unpack_from("<H", data, ptr); ptr += 2
        path = data[ptr:ptr+path_len].decode("utf-8", errors="replace"); ptr += path_len

        routes[path] = {
            "path":        path,
            "strand_idx":  strand,
            "authority":   bytes_to_ip(auth_ip),
            "checksum":    chk4.hex() + "00000000",   # expand to 8 hex chars
            "updated_at":  float(ts),
        }

    return routes


# ----------------------------
# CACHE ENTRY
# ----------------------------
@dataclass
class CacheEntry:
    data:       bytes
    checksum:   str
    strand_idx: int
    written_at: float = field(default_factory=time.time)
    authority:  str   = ""   # node IP that holds the authoritative copy

    def ttl(self) -> float:
        return _omega_ttl(self.strand_idx)

    def expired(self) -> bool:
        return (time.time() - self.written_at) > self.ttl()

    def age(self) -> float:
        return time.time() - self.written_at


# ----------------------------
# ROUTING TABLE
# ----------------------------
@dataclass
class SwapRoute:
    """
    Persistent record of where a file's authority currently lives.
    Written to disk so daemon restarts don't lose routing state.
    """
    path:        str
    strand_idx:  int
    authority:   str    # node IP
    checksum:    str
    updated_at:  float  = field(default_factory=time.time)


# ----------------------------
# HDGL FILESWAP
# ----------------------------
class HDGLFileswap:
    """
    Analog-over-digital fileswap.

    lattice    : HDGLLattice instance (shared with daemon)
    local_node : IP of the node this instance is running on
    """

    def __init__(self, lattice: Any, local_node: str) -> None:
        self.lattice     = lattice
        self.local_node  = local_node
        self._cache:  Dict[str, CacheEntry]  = {}
        self._routes: Dict[str, SwapRoute]   = {}

        SWAP_ROOT.mkdir(parents=True, exist_ok=True)
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        self._routes_file = SWAP_ROOT / "routes.json"
        self._load_routes()
        # Moiré encoder — transparent analog encoding of fileswap content
        self._moire = _HDGLMoire() if _MOIRE_AVAILABLE else None

    # --------------------------------------------------
    # PUBLIC API
    # --------------------------------------------------

    def write(self, path: str, data: bytes,
              broadcast: bool = True) -> SwapRoute:
        """
        Write a file to its analog-addressed authoritative node.

        1. Compute strand → find authoritative node via lattice
        2. Write locally if authoritative, else scp to authority
        3. Broadcast checksum to all known nodes for cache invalidation
        4. Update routing table
        """
        strand_idx = _strand_for_path(path)
        authority  = self._authority_for(strand_idx)
        # Moiré encode: transform data through Dₙ(r) interference pattern
        # The cluster fingerprint is the public lattice state; path tau is private.
        cluster_fp = self.lattice.cluster_fingerprint()
        if self._moire:
            data_on_disk = self._moire.encode(data, path, cluster_fp)
        else:
            data_on_disk = data
        checksum   = _sha256(data)          # checksum of plaintext
        swap_path  = SWAP_ROOT / path.lstrip("/")
        swap_path.parent.mkdir(parents=True, exist_ok=True)

        log.info(
            f"[fileswap] write {path}  strand={strand_idx}  "
            f"authority={authority}  size={len(data)}B  "
            f"ttl={_omega_ttl(strand_idx):.1f}s"
        )

        _dry = DRY_RUN or getattr(self, "_dry_run_override", False)
        if authority == self.local_node or _dry:
            if not _dry:
                swap_path.write_bytes(data_on_disk)
            log.info(f"[fileswap] written locally: {swap_path}" + (" [DRY-RUN]" if _dry else ""))
        else:
            # Write to temp, scp to authority
            tmp = Path(f"/tmp/hdgl_swap_{checksum[:8]}")
            tmp.write_bytes(data_on_disk)
            remote = str(SWAP_ROOT / path.lstrip("/"))
            ok = _scp_to(tmp, remote, authority)
            tmp.unlink(missing_ok=True)
            if not ok:
                log.error(f"[fileswap] write failed → {authority}:{remote}")

        # Update local cache immediately
        self._cache[path] = CacheEntry(
            data=data, checksum=checksum,
            strand_idx=strand_idx, authority=authority
        )

        # Update routing table
        route = SwapRoute(path=path, strand_idx=strand_idx,
                          authority=authority, checksum=checksum)
        self._routes[path] = route
        self._save_routes()

        # Replicate to additional peers based on vertex count (spiral geometry)
        top_map    = self.lattice.top_node_per_strand()
        all_nodes  = list({node for node, _ in top_map.values()} - {authority})
        rep_factor = strand_replication(strand_idx, len(all_nodes))
        replicas   = all_nodes[:rep_factor]

        if replicas and not _dry:
            log.info(
                f"[fileswap] replicating to {len(replicas)} peer(s) "
                f"(strand {strand_idx} {STRAND_GEOMETRY[strand_idx][2]} "
                f"verts={STRAND_GEOMETRY[strand_idx][1]} "
                f"topo={strand_topology(strand_idx)})"
            )
            tmp_r = Path(f"/tmp/hdgl_rep_{checksum[:8]}")
            tmp_r.write_bytes(data)
            for peer in replicas:
                remote = str(SWAP_ROOT / path.lstrip("/"))
                _scp_to(tmp_r, remote, peer)
            tmp_r.unlink(missing_ok=True)
        elif replicas and _dry:
            log.info(f"[DRY-RUN] would replicate to {replicas}")

        # Echo copy → mirror node at ECHO_SCALE (counter-rotating strand)
        # Stores a reduced-priority fallback per dna_closed_interact.py echo layer.
        mirror = self._mirror_for(strand_idx)
        if mirror != authority and mirror != self.local_node:
            echo_path_key = f"{path}.__echo__"
            self._cache[echo_path_key] = CacheEntry(
                data=data,               # full data, marked as echo
                checksum=checksum,
                strand_idx=strand_idx,
                authority=mirror,
            )
            # Echo TTL = ECHO_SCALE × primary TTL (lower priority, expires sooner)
            echo_ttl = _omega_ttl(strand_idx) * ECHO_SCALE
            self._cache[echo_path_key].written_at = time.time() - (
                _omega_ttl(strand_idx) * (1 - ECHO_SCALE)
            )
            log.info(
                f"[fileswap] echo copy → mirror {mirror}  "
                f"ttl={echo_ttl:.1f}s  key={echo_path_key}"
            )
            if not _dry:
                tmp_e = Path(f"/tmp/hdgl_echo_{checksum[:8]}")
                tmp_e.write_bytes(data)
                _scp_to(tmp_e, str(SWAP_ROOT / path.lstrip("/")) + ".echo", mirror)
                tmp_e.unlink(missing_ok=True)
            else:
                log.info(f"[DRY-RUN] echo scp → {mirror}")

        # Broadcast checksum so peers can invalidate stale caches
        if broadcast:
            self._broadcast_checksum(path, checksum, authority, strand_idx)

        return route

    def read(self, path: str) -> Optional[bytes]:
        """
        Read a file from the analog lattice.

        1. Check local cache — return if fresh (within Ω-TTL)
        2. Check if local node is authoritative — serve from disk
        3. Fetch from authoritative node — cache with Ω-TTL
        """
        strand_idx = _strand_for_path(path)
        authority  = self._authority_for(strand_idx)
        ttl        = _omega_ttl(strand_idx)

        # Cache hit
        entry = self._cache.get(path)
        if entry and not entry.expired():
            log.debug(f"[fileswap] cache hit {path}  age={entry.age():.1f}s/{ttl:.1f}s")
            return entry.data

        # Local authority — serve from disk
        local_path = SWAP_ROOT / path.lstrip("/")
        if authority == self.local_node and local_path.exists():
            raw  = local_path.read_bytes()
            cluster_fp = self.lattice.cluster_fingerprint()
            data = self._moire.decode(raw, path, cluster_fp) if self._moire else raw
            self._cache[path] = CacheEntry(
                data=data, checksum=_sha256(data),
                strand_idx=strand_idx, authority=authority
            )
            log.info(f"[fileswap] local authority read {path}")
            return data

        # Remote fetch
        log.info(f"[fileswap] fetching {path} from {authority}")
        raw_remote = _fetch_from(authority, path)
        if raw_remote:
            cluster_fp = self.lattice.cluster_fingerprint()
            data = self._moire.decode(raw_remote, path, cluster_fp) if self._moire else raw_remote
            # Verify checksum against plaintext
            route = self._routes.get(path)
            if route and _sha256(data) != route.checksum:
                log.warning(f"[fileswap] checksum mismatch for {path} from {authority}")
                return None
            self._cache[path] = CacheEntry(
                data=data, checksum=_sha256(data),
                strand_idx=strand_idx, authority=authority
            )
            # Cache encoded bytes locally for resilience
            cache_path = CACHE_ROOT / path.lstrip("/")
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(raw_remote)
            return data

        # Echo fallback: try mirror node at ECHO_SCALE priority
        echo_key = f"{path}.__echo__"
        echo_entry = self._cache.get(echo_key)
        if echo_entry and not echo_entry.expired():
            log.info(f"[fileswap] echo fallback hit for {path} from mirror {echo_entry.authority}")
            return echo_entry.data

        mirror = self._mirror_for(strand_idx)
        if mirror != authority:
            log.info(f"[fileswap] trying mirror node {mirror} for {path}")
            data = _fetch_from(mirror, path)
            if data:
                self._cache[path] = CacheEntry(
                    data=data, checksum=_sha256(data),
                    strand_idx=strand_idx, authority=mirror
                )
                log.info(f"[fileswap] served from mirror {mirror}")
                return data

        log.error(f"[fileswap] read failed for {path} — authority {authority} and mirror {mirror} both unreachable")
        return None

    def prewarm(self, path: str) -> None:
        """
        Pre-warm cache for strands ahead of the current path's strand.
        Derived from speed_factor in spiral3.py: fetch SPEED_FACTOR strands
        ahead so the cache is ready before the lattice transitions.

        Call this after a successful read to stay ahead of strand migration.
        """
        current_strand = _strand_for_path(path)
        # Look ahead by SPEED_FACTOR strands (wraps within 0-7)
        ahead = int(SPEED_FACTOR)
        for offset in range(1, ahead + 1):
            next_strand = (current_strand + offset) % NUM_STRANDS
            # Find files in the next strand that aren't cached or are expiring soon
            for fpath, route in self._routes.items():
                if route.strand_idx != next_strand:
                    continue
                entry = self._cache.get(fpath)
                ttl   = _omega_ttl(next_strand)
                # Pre-warm if not cached or within 20% of expiry
                if not entry or entry.age() > ttl * 0.8:
                    log.info(
                        f"[fileswap] pre-warm strand {next_strand} "
                        f"({STRAND_GEOMETRY[next_strand][2]}): {fpath}"
                    )
                    self.read(fpath)   # populates cache as side effect

    def rebalance(self) -> None:
        """
        Called each daemon heal cycle.

        For each routed file, check if the authoritative node has changed
        (because lattice weights shifted). If so, migrate the file to the
        new authority via scp and update the routing table.
        """
        if not self._routes:
            return

        migrations = 0
        for path, route in list(self._routes.items()):
            new_authority = self._authority_for(route.strand_idx)
            if new_authority == route.authority:
                continue

            log.info(
                f"[fileswap] rebalance {path}  "
                f"{route.authority} → {new_authority}  "
                f"strand={route.strand_idx}"
            )

            # Fetch file from old authority if not cached locally
            data = None
            entry = self._cache.get(path)
            if entry and not entry.expired():
                data = entry.data
            else:
                local_path = SWAP_ROOT / path.lstrip("/")
                if local_path.exists():
                    data = local_path.read_bytes()
                else:
                    data = _fetch_from(route.authority, path)

            if data is None:
                log.error(f"[fileswap] rebalance: could not retrieve {path}")
                continue

            # Use inter-rung migration paths (dna_closed_interact.py shape-to-shape links)
            # Try each path in order until one succeeds
            paths = self._migration_paths(
                self.lattice._states[route.authority].slots[0]  # dummy — use strand_idx
                if route.authority in self.lattice._states else 0,
                route.strand_idx
            ) if False else self._migration_paths(route.strand_idx, route.strand_idx)
            # Simpler: direct migration paths for this strand
            mig_paths = self._migration_paths(route.strand_idx, route.strand_idx)

            tmp = Path(f"/tmp/hdgl_migrate_{_sha256(data)[:8]}")
            tmp.write_bytes(data)
            remote = str(SWAP_ROOT / path.lstrip("/"))
            migrated = False

            for from_node, to_node in mig_paths:
                if to_node == new_authority:
                    ok = _scp_to(tmp, remote, to_node)
                    if ok:
                        log.info(
                            f"[fileswap] migrated {path} "
                            f"{from_node} → {to_node} "
                            f"(inter-rung path)"
                        )
                        migrated = True
                        break

            if not migrated:
                # Fallback: direct scp
                ok = _scp_to(tmp, remote, new_authority)
                if ok:
                    migrated = True
                    log.info(f"[fileswap] migrated {path} → {new_authority} (direct)")

            tmp.unlink(missing_ok=True)

            if migrated:
                route.authority  = new_authority
                route.updated_at = time.time()
                migrations += 1
            else:
                log.error(f"[fileswap] migration failed for {path} → {new_authority}")

        if migrations:
            self._save_routes()
            log.info(f"[fileswap] rebalance complete: {migrations} file(s) migrated")

    def status(self) -> Dict[str, Any]:
        """
        Return current fileswap state as a dict.
        Suitable for JSON export to monitoring dashboards.
        """
        top_map = self.lattice.top_node_per_strand()
        return {
            "local_node":    self.local_node,
            "routed_files":  len(self._routes),
            "cached_files":  len(self._cache),
            "strands": [
                {
                    "strand":    k,
                    "label":     chr(ord("A") + k),
                    "authority": top_map.get(k, ("", 0))[0],
                    "omega":     OMEGA[k],
                    "ttl_s":     _omega_ttl(k),
                    "files":     [
                        {
                            "path":      p,
                            "checksum":  r.checksum[:8],
                            "authority": r.authority,
                            "age_s":     time.time() - r.updated_at,
                        }
                        for p, r in self._routes.items()
                        if r.strand_idx == k
                    ],
                }
                for k in range(NUM_STRANDS)
            ],
        }

    def simulation_matrix(self) -> str:
        """ASCII audit of strand → polytope → alpha → TTL → replication → files."""
        top_map = self.lattice.top_node_per_strand()
        cluster_size = max(len(top_map), 1)
        SEP = "-" * 104
        lines = [
            "",
            "=" * 122,
            " HDGL FILESWAP MATRIX  —  Analog-Over-Digital Routing Audit",
            "  Sources: spiral3.py + dna_closed_interact.py (github.com/stealthmachines/spiral8plus)",
            "=" * 122,
            f"  {'Strand':<7}  {'Polytope':<14}  {'Alpha':>8}  {'Verts':<6}  "
            f"{'Authority':<18}  {'Mirror':<18}  {'TTL(s)':<10}  "
            f"{'Rep':<4}  {'Topo':<8}  {'Files':<5}  Paths  [Stability]",
            "-" * 122,
        ]
        SEP = "-" * 122

        for k in range(NUM_STRANDS):
            label               = chr(ord("A") + k)
            authority           = top_map.get(k, ("—", 0))[0] or "—"
            alpha, verts, poly  = STRAND_GEOMETRY[k]
            ttl                 = _omega_ttl(k)
            files               = [p for p, r in self._routes.items() if r.strand_idx == k]
            marker              = "▶" if authority == self.local_node else " "
            paths_str           = ", ".join(files[:2]) + ("…" if len(files) > 2 else "")
            rep_n               = strand_replication(k, cluster_size)
            stability           = "◆ STABLE" if alpha < 0 else "  expand"
            mirror              = self._mirror_for(k)
            topo                = strand_topology(k)

            lines.append(
                f"{marker} {label:<7}  {poly:<14}  {alpha:>+8.4f}  {verts:<6}  "
                f"{authority:<18}  {mirror:<18}  {ttl:<10.1f}  "
                f"{rep_n:<4}  {topo:<8}  {len(files):<5}  "
                f"{paths_str}  [{stability}]"
            )

        lines += [
            SEP,
            f"  Local node    : {self.local_node}",
            f"  TTL base      : {TTL_BASE:.0f}s  |  spiral period: {SPIRAL_PERIOD}s"
            f"  |  speed factor: {SPEED_FACTOR}  |  echo scale: {ECHO_SCALE}",
            f"  Stable strands: D (Tetrahedron α={STRAND_GEOMETRY[3][0]:+.4f}), "
            f"F (Hexacross α={STRAND_GEOMETRY[5][0]:+.4f})",
            f"  Topology      : low-D (verts≤{LOW_D_THRESHOLD}) = complete graph  |  high-D = grid lattice",
            f"  Inter-rung    : {INTER_RUNG_LINKS} migration paths between consecutive strands",
            f"  Total files   : {len(self._routes)}",
            "=" * 122 + "\n",
        ]
        return "\n".join(lines)

    # --------------------------------------------------
    # INTERNAL
    # --------------------------------------------------

    def _authority_for(self, strand_idx: int) -> str:
        """
        Primary authority: highest analog weight node for this strand.
        Corresponds to the positive (primary) spiral strand.
        """
        top_map = self.lattice.top_node_per_strand()
        node, _ = top_map.get(strand_idx, (self.local_node, 0.0))
        return node or self.local_node

    def _mirror_for(self, strand_idx: int) -> str:
        """
        Mirror authority: second-highest weight node for this strand.
        Corresponds to the negative (counter-rotating) spiral strand.
        Holds the echo copy at ECHO_SCALE size.
        Returns local_node if no second node exists.
        """
        all_nodes = list(self.lattice._states.keys())
        if len(all_nodes) < 2:
            return self.local_node
        scored = sorted(
            all_nodes,
            key=lambda n: self.lattice.strand_weight(n, strand_idx),
            reverse=True
        )
        return scored[1] if len(scored) > 1 else self.local_node

    def _echo_node_for(self, strand_idx: int) -> str:
        """
        Echo node: authority of the PREVIOUS strand (k-1).
        Holds the 0.8-scale fallback copy per dna_closed_interact.py echo layer.
        Falls back to local_node for strand 0.
        """
        if strand_idx == 0:
            return self.local_node
        return self._authority_for(strand_idx - 1)

    def _migration_paths(self, from_strand: int, to_strand: int) -> list:
        """
        Inter-rung links: INTER_RUNG_LINKS paths between consecutive strand authorities.
        Derived from shape-to-shape connections in dna_closed_interact.py.
        Returns list of (from_node, to_node) tuples for migration routing.
        """
        from_auth = self._authority_for(from_strand)
        to_auth   = self._authority_for(to_strand)
        from_mir  = self._mirror_for(from_strand)
        to_mir    = self._mirror_for(to_strand)

        # Primary path + mirror paths (up to INTER_RUNG_LINKS)
        paths = [(from_auth, to_auth)]
        if from_mir != from_auth:
            paths.append((from_mir, to_auth))
        if to_mir != to_auth:
            paths.append((from_auth, to_mir))
        if from_mir != from_auth and to_mir != to_auth:
            paths.append((from_mir, to_mir))
        # Fill remaining slots with primary path (resilience via repetition)
        while len(paths) < INTER_RUNG_LINKS:
            paths.append((from_auth, to_auth))
        return paths[:INTER_RUNG_LINKS]

    def _broadcast_checksum(self, path: str, checksum: str,
                             authority: str, strand_idx: int) -> None:
        """
        Notify all known nodes of a new checksum so they can invalidate
        stale cache entries. Uses a lightweight JSON gossip POST.
        """
        _dry = DRY_RUN or getattr(self, "_dry_run_override", False)
        if _dry:
            log.info(f"[DRY-RUN] broadcast checksum {checksum[:8]} for {path}")
            return

        top_map = self.lattice.top_node_per_strand()
        peers   = set(node for node, _ in top_map.values()) - {self.local_node}

        payload = {
            "path":       path,
            "checksum":   checksum,
            "authority":  authority,
            "strand_idx": strand_idx,
        }

        for peer in peers:
            url = f"http://{peer}:{SWAP_HTTP_PORT}/swap_invalidate"
            try:
                import json as _json
                from hdgl_node_server import _sign_payload, _HMAC_HEADER
                body = _json.dumps(payload).encode()
                sig  = _sign_payload(body)
                hdrs = {"Content-Type": "application/json"}
                if sig:
                    hdrs[_HMAC_HEADER] = sig
                requests.post(url, data=body, headers=hdrs, timeout=2)
                log.debug(f"[fileswap] checksum broadcast → {peer}")
            except requests.RequestException as e:
                log.warning(f"[fileswap] broadcast failed → {peer}: {e}")

    def observe_latency(self, node_id: str, observed_ms: float) -> float:
        """Delegate EMA feedback to the underlying lattice."""
        return self.lattice.observe_latency(node_id, observed_ms)

    def invalidate(self, path: str, checksum: str) -> None:
        """
        Receive a cache invalidation from a peer.
        If our cached entry doesn't match the new checksum, evict it.
        """
        entry = self._cache.get(path)
        if entry and entry.checksum != checksum:
            del self._cache[path]
            log.info(f"[fileswap] cache invalidated: {path}")

    def _load_routes(self) -> None:
        """
        Load routes preferring binary format, falling back to JSON.
        Binary: 17 bytes/route  JSON: ~97 bytes/route  (82% smaller)
        """
        bin_path = self._routes_file.with_suffix(".bin")

        # Try binary first
        if bin_path.exists():
            try:
                raw_bin = decode_routes(bin_path.read_bytes())
                for path, r in raw_bin.items():
                    self._routes[path] = SwapRoute(**r)
                log.info(
                    f"[fileswap] loaded {len(self._routes)} routes from binary "                    f"({bin_path.stat().st_size} bytes)"                )
                return
            except Exception as e:
                log.warning(f"[fileswap] binary route load failed, trying JSON: {e}")

        # Fall back to JSON
        if self._routes_file.exists():
            try:
                raw = json.loads(self._routes_file.read_text())
                for path, r in raw.items():
                    self._routes[path] = SwapRoute(**r)
                log.info(
                    f"[fileswap] loaded {len(self._routes)} routes from JSON "                    f"(consider running once to migrate to binary format)"                )
            except Exception as e:
                log.warning(f"[fileswap] could not load routes: {e}")

    def _save_routes(self) -> None:
        """
        Persist routes in binary format (17 bytes/route vs ~97 bytes JSON).
        Falls back to JSON on any error.
        """
        try:
            bin_path = self._routes_file.with_suffix(".bin")
            bin_data = encode_routes(self._routes)
            bin_path.write_bytes(bin_data)
            # Also keep JSON as human-readable backup
            raw = {
                p: {"path": r.path, "strand_idx": r.strand_idx,
                    "authority": r.authority, "checksum": r.checksum,
                    "updated_at": r.updated_at}
                for p, r in self._routes.items()
            }
            self._routes_file.write_text(json.dumps(raw, indent=2))
        except Exception as e:
            log.warning(f"[fileswap] could not save routes: {e}")


# ----------------------------
# STANDALONE SMOKE TEST
# ----------------------------
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")

    # Import lattice and build a minimal cluster for testing
    sys.path.insert(0, str(Path(__file__).parent))
    from hdgl_lattice import HDGLLattice

    lattice = HDGLLattice()
    nodes   = [
        {"node": "209.159.159.170", "latency": 50,  "storage_avail_gb": 1.0},
        {"node": "209.159.159.171", "latency": 80,  "storage_avail_gb": 2.0},
    ]
    for n in nodes:
        lattice.update(n["node"], n["latency"], n["storage_avail_gb"])

    swap = HDGLFileswap(lattice, local_node="209.159.159.170")

    # Verify strand mapping for test paths
    log.info("--- Strand mapping ---")
    test_paths = [
        "/wecharg/config.json",
        "/stealthmachines/index.html",
        "/josefkulovany/data/export.csv",
        "/api/v1/users",
        "/static/images/logo.png",
    ]
    for p in test_paths:
        k   = _strand_for_path(p)
        ttl = _omega_ttl(k)
        log.info(f"  {p:<35}  strand={k} ({chr(ord('A')+k)})  "
                 f"Ω={OMEGA[k]:.3e}  TTL={ttl:.1f}s")

    # Register test files in routing table (dry-run write)
    log.info("\n--- Writing test files (DRY_RUN) ---")
    import os
    os.environ["LN_DRY_RUN"] = "1"

    # Patch module-level DRY_RUN for smoke test
    import hdgl_fileswap as _self
    _self.DRY_RUN = True
    swap._dry_run_override = True   # flag checked in write()

    for p in test_paths:
        swap.write(p, f"content of {p}".encode())

    # Print simulation matrix
    print(swap.simulation_matrix())

    # Print JSON status
    status = swap.status()
    log.info(f"Status: {status['routed_files']} routed, "
             f"{status['cached_files']} cached")
    for s in status["strands"]:
        if s["files"]:
            log.info(f"  Strand {s['label']}: authority={s['authority']}  "
                     f"TTL={s['ttl_s']:.1f}s  files={[f['path'] for f in s['files']]}")
