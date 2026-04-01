#!/usr/bin/env python3
"""
hdgl_lattice.py
---------------
HDGL Superposition-Advantaged Binary Lattice — live integration module.

Produces per-node and cluster-level signals:
  - D_n(node)          : metric-driven slot value (float, continuous)
  - bit_n              : binary at threshold √φ
  - fingerprint(node)  : 32-bit hex cluster health signal
  - strand_weight()    : analog upstream weight per octave
  - simulation_matrix(): full ASCII audit table

Audit fixes applied:
  - Iterative Fibonacci (no recursion depth risk)
  - Self-contained _phi_cache_key_simple (no internal import)
  - Global top-strand highlight across all nodes
  - SHARED_RESONANCE_SLOTS consistently 0-indexed
  - Improved type hints
  - Adaptive table separator
"""

import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

# ----------------------------
# CONSTANTS
# ----------------------------
PHI          = (1 + math.sqrt(5)) / 2    # ≈ 1.6180339887
SQRT_PHI     = math.sqrt(PHI)            # ≈ 1.2720196495 — discretization threshold
NUM_STRANDS  = 8                          # A–H
SLOTS_PER    = 4                          # D slots per strand
NUM_SLOTS    = NUM_STRANDS * SLOTS_PER   # 32 total

# Strand labels and recursion biases (per HDGL spec)
STRANDS: List[Dict[str, Any]] = [
    {"label": "A", "r_dim": 0.3, "wave": "+/0"},
    {"label": "B", "r_dim": 0.4, "wave": "0/-"},
    {"label": "C", "r_dim": 0.5, "wave": "+/-"},
    {"label": "D", "r_dim": 0.6, "wave": "full"},
    {"label": "E", "r_dim": 0.7, "wave": "+"},
    {"label": "F", "r_dim": 0.8, "wave": "0"},
    {"label": "G", "r_dim": 0.9, "wave": "-"},
    {"label": "H", "r_dim": 1.0, "wave": "full"},
]

# 0-indexed shared resonance slots (every 3rd slot starting at index 2)
# Maps to D_3, D_7, D_11, D_15, D_19, D_23, D_27, D_31 in 1-indexed spec
SHARED_RESONANCE_SLOTS = frozenset(range(2, NUM_SLOTS, 4))  # {2, 6, 10, 14, 18, 22, 26, 30}


# ----------------------------
# FIBONACCI — iterative, no recursion depth risk
# ----------------------------
def fibonacci(n: int) -> int:
    """Return nth Fibonacci number iteratively. F(0)=0, F(1)=1."""
    if n <= 0:
        return 0
    a, b = 0, 1
    for _ in range(n - 1):
        a, b = b, a + b
    return b


# ----------------------------
# φ-CACHE KEY (self-contained, no imports needed)
# ----------------------------
def _phi_cache_key_simple(path: str) -> str:
    """Deterministic φ-spiral cache key. Format: phi_<octave>_<tau_hex>."""
    segments = [s for s in path.strip("/").split("/") if s]
    tau = 0.0
    for depth, seg in enumerate(segments):
        intra = (sum(ord(c) for c in seg) % 1000) / 1000.0
        tau  += (PHI ** depth) * (depth + intra)
    k       = min(int(tau), 7)
    tau_hex = format(int((tau % 1.0) * 0xFFFF), "04x")
    return f"phi_{k}_{tau_hex}"


# ----------------------------
# HDGL SLOT COMPUTATION
# ----------------------------
def hdgl_slot(n: int, latency_ms: float, storage_gb: float,
              r_dim: float = 0.5) -> float:
    """
    D_n(r) — metric-driven slot value.

        D_n = φ^n × r × r_dim / F_n

    r     = storage_gb / max(latency_ms, 1)  — resource efficiency ratio
    r_dim = strand recursion bias [0.3–1.0]
    F_n   = nth Fibonacci (stabilises high-n growth)

    φ^n / F_n → 1/√5 as n→∞ (Binet's formula), so slots plateau
    rather than diverge.
    """
    r   = storage_gb / max(latency_ms, 1.0)
    F_n = fibonacci(n) or 1   # guard against F(0)=0
    return (PHI ** n) * r * r_dim / F_n


def discretize(value: float) -> int:
    """Apply √φ threshold: 1 if value ≥ √φ, else 0."""
    return 1 if value >= SQRT_PHI else 0


# ----------------------------
# PROVISIONER  (from fold_hdgl_full4.py / hdgl_executor2.py)
# ----------------------------
# Per-cycle lattice normalization pass derived from the Base4096 HDGL
# provisioner instruction set. Runs NORM → SCALE → PHASESHIFT → OMEGAMULT
# → ENERGY → FOLD256 each health cycle, making the lattice self-calibrating
# rather than relying on fixed constants.

from dataclasses import dataclass as _dc, field as _field

@_dc
class ProvisionerResult:
    """Output of one provisioner pass."""
    energy:         float        # total slot energy (Σ D_n × Ω_n)
    folded_weight:  float        # FOLD256: mean amplitude after superposition
    norm_max:       float        # max amplitude before normalisation
    scale_factor:   float        # amplification applied
    phase_shift:    float        # r_dim phase shift applied
    omega_mult:     float        # Ω multiplier applied


def run_provisioner(slots: list,
                    scale: float      = 1e6,
                    phase: float      = 0.25,
                    omega_mult: float = 2.0,
                    quality: float    = 1.0) -> ProvisionerResult:
    """
    Execute the six provisioner operations from hdgl_executor2.py,
    mapped onto our (D_n, Ω_n, r_dim) slot tuples.

    NORM       → normalize D_n to [0, 1] relative to max
    SCALE      → multiply D_n by scale factor (amplification)
    PHASESHIFT → add phase to r_dim, wrap to [0, 1]
    OMEGAMULT  → multiply all Ω_n by (omega_mult × quality)
    ENERGY     → Σ D_n × Ω_n  (cluster health scalar)
    FOLD256    → mean of all D_n after transforms (superposition collapse)

    quality = storage_gb / latency_ms  (node resource efficiency ratio)
    Better nodes (higher quality) → higher effective Ω → higher energy.
    This ensures ENERGY differentiates nodes even after NORM.

    The result feeds back into weight computation via the energy scalar.
    """
    if not slots:
        return ProvisionerResult(0, 0, 0, scale, phase, omega_mult)

    working = [list(s) for s in slots]   # mutable copy

    # NORM
    max_d = max(abs(s[0]) for s in working) or 1.0
    for s in working:
        s[0] /= max_d

    # SCALE
    for s in working:
        s[0] *= scale

    # PHASESHIFT
    for s in working:
        s[2] = (s[2] + phase) % 1.0

    # OMEGAMULT × quality — differentiates nodes by resource efficiency
    effective_mult = omega_mult * max(quality, 1e-6)
    for s in working:
        s[1] *= effective_mult

    # ENERGY
    energy = sum(s[0] * s[1] for s in working)

    # FOLD256 — superposition collapse to single weight
    fold = sum(s[0] for s in working) / len(working)

    return ProvisionerResult(
        energy=energy,
        folded_weight=fold,
        norm_max=max_d,
        scale_factor=scale,
        phase_shift=phase,
        omega_mult=effective_mult,
    )


# ----------------------------
# NODE STATE
# ----------------------------
@dataclass
class NodeState:
    """Full 32-slot HDGL state for one node."""
    node_id:     str
    latency_ms:  float
    storage_gb:  float
    slots:       List[float] = field(default_factory=list)
    bits:        List[int]   = field(default_factory=list)
    fingerprint: str         = ""
    excitation:  float       = 0.0   # fraction of bits == 1

    def __post_init__(self) -> None:
        if not self.slots:
            self._compute()

    def _compute(self) -> None:
        self.slots = []
        self.bits  = []

        for slot_idx in range(NUM_SLOTS):
            strand_idx = slot_idx // SLOTS_PER
            strand     = STRANDS[strand_idx]
            r_dim      = strand["r_dim"]
            n          = slot_idx + 1   # 1-indexed slot number

            val = hdgl_slot(n, self.latency_ms, self.storage_gb, r_dim)

            # Shared resonance: harmonic blend with previous strand's slot
            if slot_idx in SHARED_RESONANCE_SLOTS and strand_idx > 0:
                prev_r_dim = STRANDS[strand_idx - 1]["r_dim"]
                prev_val   = hdgl_slot(n - 1, self.latency_ms,
                                        self.storage_gb, prev_r_dim)
                val = 0.5 * (val + prev_val)

            self.slots.append(val)
            self.bits.append(discretize(val))

        # 32-bit fingerprint: LSB = slot 0, MSB = slot 31
        int_val          = sum(b << i for i, b in enumerate(self.bits))
        self.fingerprint = f"0x{int_val:08X}"
        self.excitation  = sum(self.bits) / NUM_SLOTS


# ----------------------------
# HDGL LATTICE
# ----------------------------
class HDGLLattice:
    """
    Cluster-level HDGL state manager.

    Tracks NodeState per node, exposes analog strand weights for routing,
    and generates fingerprints + simulation matrices.
    """

    def __init__(self) -> None:
        self._states: Dict[str, NodeState] = {}
        # Closed-loop feedback: EMA of observed latency per node.
        # Updated each cycle; blended into slot computation to let
        # actual traffic behaviour influence analog weights over time.
        self._latency_ema: Dict[str, float] = {}
        self._EMA_ALPHA = 0.25   # 0=ignore new, 1=ignore history

    def update(self, node_id: str, latency_ms: float,
               storage_gb: float) -> NodeState:
        """
        Recompute HDGL state, blending observed latency with EMA history.

        Closed-loop feedback path:
          analog weight → NGINX → traffic → observed latency
          → EMA blend → hdgl_slot() → updated analog weight

        The EMA smooths transient spikes while allowing the lattice to
        track genuine shifts in node performance over time.
        """
        # Update EMA: blend observed latency into running average
        if node_id in self._latency_ema:
            ema = (self._EMA_ALPHA * latency_ms
                   + (1 - self._EMA_ALPHA) * self._latency_ema[node_id])
        else:
            ema = latency_ms   # cold start: trust first observation
        self._latency_ema[node_id] = ema

        # Feed EMA latency into slot computation, not raw observed value
        state = NodeState(node_id=node_id,
                          latency_ms=ema,
                          storage_gb=storage_gb)
        self._states[node_id] = state
        return state

    def fingerprint(self, node_id: str) -> str:
        s = self._states.get(node_id)
        return s.fingerprint if s else "0x00000000"

    def strand_weight(self, node_id: str, strand_idx: int) -> float:
        """
        Mean analog slot value for a node at a given strand/octave.
        Continuous — not discretized. Used as upstream routing weight.
        """
        s = self._states.get(node_id)
        if not s:
            return 0.0
        base  = strand_idx * SLOTS_PER
        slots = s.slots[base: base + SLOTS_PER]
        return sum(slots) / len(slots) if slots else 0.0

    def cluster_fingerprint(self) -> str:
        """
        OR-aggregate of all node fingerprints.
        0x00000000 → all nodes degraded / grounded.
        0xFFFFFFFF → all slots excited across all nodes.
        Target: 0xFFFF0000 (low-n grounded, high-n excited).
        """
        agg = 0
        for s in self._states.values():
            agg |= int(s.fingerprint, 16)
        return f"0x{agg:08X}"

    def provisioner_pass(self, node_id: str) -> "ProvisionerResult":
        """
        Run one provisioner cycle for a node.
        Extracts slot values from current NodeState and applies
        NORM→SCALE→PHASESHIFT→OMEGAMULT→ENERGY→FOLD256.

        The returned energy and folded_weight are used by the host
        to compute a self-calibrating upstream weight that reflects
        actual cluster state rather than fixed amplification constants.

        Called each health cycle in hdgl_host.py after lattice.update().
        """
        state = self._states.get(node_id)
        if state is None:
            return ProvisionerResult(0, 0, 0, 1e6, 0.25, 2.0)

        # Build (D_n, Ω_n, r_dim) tuples from current slot values
        slots = []
        for si in range(NUM_SLOTS):
            strand_idx = si // SLOTS_PER
            omega_k    = 1.0 / (PHI ** ((strand_idx + 1) * 7))
            r_dim      = STRANDS[strand_idx]["r_dim"]
            slots.append([state.slots[si], omega_k, r_dim])

        # Pass node quality (storage/latency) so ENERGY differentiates nodes
        ema     = self._latency_ema.get(node_id, state.latency_ms)
        quality = state.storage_gb / max(ema, 1.0)
        return run_provisioner(slots, quality=quality)

    def observe_latency(self, node_id: str, observed_ms: float) -> float:
        """
        Explicit feedback hook. Call after each request cycle with
        measured round-trip latency to update the EMA without a full
        lattice recompute.

        Returns the updated EMA value.
        """
        if node_id in self._latency_ema:
            ema = (self._EMA_ALPHA * observed_ms
                   + (1 - self._EMA_ALPHA) * self._latency_ema[node_id])
        else:
            ema = observed_ms
        self._latency_ema[node_id] = ema
        return ema

    def top_node_per_strand(self) -> Dict[int, Tuple[str, float]]:
        """
        For each strand index, identify the globally highest-weight node.
        Used for cross-node top-strand highlighting in the matrix.
        """
        result: Dict[int, Tuple[str, float]] = {}
        for si in range(NUM_STRANDS):
            best_node, best_w = "", -1.0
            for nid in self._states:
                w = self.strand_weight(nid, si)
                if w > best_w:
                    best_w, best_node = w, nid
            result[si] = (best_node, best_w)
        return result

    def simulation_matrix(self, nodes: List[Dict[str, Any]],
                           services: Dict[str, Any]) -> str:
        """
        Full node × strand × service ASCII audit matrix.
        Highlights globally top strand weight per strand across all nodes.
        Table width adapts to terminal (min 100 cols).
        """
        for n in nodes:
            self.update(n["node"], n.get("latency", 1000),
                        n.get("storage_avail_gb", 1.0))

        try:
            term_w = max(os.get_terminal_size().columns, 100)
        except OSError:
            term_w = 110
        SEP     = "-" * term_w
        EQ      = "=" * term_w
        svc_items = list(services.items())
        top_map   = self.top_node_per_strand()   # global top per strand

        lines = [
            "",
            EQ,
            " HDGL SUPERPOSITION MATRIX  —  φ-Spiral Lattice Audit",
            EQ,
            f"  {'Node':<18}  {'Lat(ms)':<8}  {'EMA(ms)':<8}  {'Stor(GB)':<9}  "
            f"{'Strand':<7}  {'Analog W':<9}  {'Bits':<10}  "
            f"{'Excit%':<7}  {'Fingerprint':<12}  Service → Cache Key",
            SEP,
        ]

        for node_dict in nodes:
            nid   = node_dict["node"]
            state = self._states[nid]
            lat   = node_dict.get("latency", 1000)
            stor  = node_dict.get("storage_avail_gb", 1.0)
            excit = f"{state.excitation * 100:.0f}%"
            fp    = state.fingerprint

            for si, strand in enumerate(STRANDS):
                w       = self.strand_weight(nid, si)
                base    = si * SLOTS_PER
                bit_str = "".join(str(b) for b in state.bits[base: base + SLOTS_PER])

                # Global top-strand marker: ▶ if this node holds the top weight
                # for this strand across ALL nodes in the cluster
                is_global_top = top_map[si][0] == nid
                marker = "▶" if is_global_top else " "

                svc_name, _ = svc_items[si % len(svc_items)]
                cache_key   = _phi_cache_key_simple(f"/{svc_name}/")

                ema_ms    = self._latency_ema.get(nid, lat)
                node_col  = f"{nid:<18}" if si == 0 else " " * 18
                lat_col   = f"{lat:<8}"  if si == 0 else " " * 8
                ema_col   = f"{ema_ms:<8.1f}" if si == 0 else " " * 8
                stor_col  = f"{stor:<9.1f}" if si == 0 else " " * 9
                excit_col = f"{excit:<7}" if si == 0 else " " * 7
                fp_col    = f"{fp:<12}"  if si == 0 else " " * 12

                lines.append(
                    f"{marker} {node_col}  {lat_col}  {ema_col}  {stor_col}  "
                    f"{strand['label']:<7}  {w:<9.4f}  {bit_str:<10}  "
                    f"{excit_col}  {fp_col}  {svc_name} → {cache_key}"
                )

            lines.append(SEP)

        lines += [
            f"\n  Cluster fingerprint : {self.cluster_fingerprint()}",
            f"  Threshold           : √φ ≈ {SQRT_PHI:.7f}",
            f"  Target state        : 0xFFFF0000  "
            f"(low-n grounded, high-n excited)",
            EQ + "\n",
        ]

        return "\n".join(lines)


# ----------------------------
# STANDALONE SMOKE TEST
# ----------------------------
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] %(message)s",
                        datefmt="%H:%M:%S")
    log = logging.getLogger(__name__)

    # Verify Fibonacci correctness
    expected = [0, 1, 1, 2, 3, 5, 8, 13, 21, 34]
    assert [fibonacci(i) for i in range(10)] == expected, "Fibonacci mismatch"
    log.info("Fibonacci: OK")

    lattice = HDGLLattice()

    test_nodes: List[Dict[str, Any]] = [
        {"node": "209.159.159.170", "latency": 50,  "storage_avail_gb": 1.0},
        {"node": "209.159.159.171", "latency": 80,  "storage_avail_gb": 2.0},
        {"node": "209.159.159.172", "latency": 200, "storage_avail_gb": 4.0},
    ]

    test_services: Dict[str, Any] = {
        "wecharg":         {"port": 8083, "domain": "wecharg.com"},
        "stealthmachines": {"port": 8080, "domain": "stealthmachines.com"},
        "josefkulovany":   {"port": 8081, "domain": "josefkulovany.com"},
    }

    print(lattice.simulation_matrix(test_nodes, test_services))

    log.info("--- Per-node fingerprints ---")
    for n in test_nodes:
        nid = n["node"]
        s   = lattice._states[nid]
        log.info(f"  {nid}  {s.fingerprint}  excitation={s.excitation*100:.0f}%  "
                 f"bits={''.join(map(str, s.bits))}")
    log.info(f"  Cluster: {lattice.cluster_fingerprint()}")
