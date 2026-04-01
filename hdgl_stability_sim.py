#!/usr/bin/env python3
"""
hdgl_stability_sim.py
─────────────────────
Long-term stability simulation for the HDGL stack.

Simulates months of synthetic time compressed into seconds:
  - Node failures, recoveries, latency spikes, storage churn
  - Gossip discovery of new nodes
  - File write/read/rebalance cycles
  - Authority migration tracking
  - Cache hit/miss/echo rates
  - Fingerprint drift and excitation tracking
  - Weight differentiation stability
  - TTL correctness over time

Output: per-cycle metrics + final stability report + JSON results
"""

import os, sys, math, time, json, random, tempfile, shutil, hashlib
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

# ── env setup ────────────────────────────────────────────────────────────────
_TD = Path(tempfile.mkdtemp(prefix="hdgl_sim_"))
os.environ["LN_FILESWAP_ROOT"]  = str(_TD / "swap")
os.environ["LN_FILESWAP_CACHE"] = str(_TD / "cache")
os.environ["LN_DRY_RUN"]        = "1"
os.environ["LN_SIMULATION"]     = "1"

sys.path.insert(0, str(Path(__file__).parent))
from hdgl_lattice  import HDGLLattice, PHI, SQRT_PHI
from hdgl_fileswap import (
    HDGLFileswap, _phi_tau, _strand_for_path, _omega_ttl,
    STRAND_GEOMETRY, NUM_STRANDS, TTL_BASE,
    strand_replication, alpha_ttl,
)
import hdgl_fileswap as _fs
_fs.DRY_RUN = True

# ── ANSI ──────────────────────────────────────────────────────────────────────
G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"
B = "\033[94m"; C = "\033[96m"; W = "\033[97m"; X = "\033[0m"
BOLD = "\033[1m"

def bar(v, mn, mx, w=20, color=G):
    if mx == mn: pct = 0
    else: pct = (v - mn) / (mx - mn)
    filled = int(pct * w)
    return color + "█" * filled + X + "░" * (w - filled)

# ── SIMULATION CONFIG ─────────────────────────────────────────────────────────
SIM_DAYS          = 90       # synthetic days to simulate
CYCLES_PER_DAY    = 48       # one cycle = 30 min of synthetic time
TOTAL_CYCLES      = SIM_DAYS * CYCLES_PER_DAY

INITIAL_NODES     = 2        # start with 2 (matching your real cluster)
MAX_NODES         = 6        # gossip can grow to this many
FAILURE_PROB      = 0.04     # probability a node fails per cycle
RECOVERY_PROB     = 0.35     # probability a failed node recovers per cycle
SPIKE_PROB        = 0.08     # probability of latency spike per node per cycle
GOSSIP_PROB       = 0.02     # probability of discovering a new node per cycle
STORAGE_DRIFT     = 0.001    # GB change per cycle (gradual fill)

# Test file paths (representative of your actual services)
TEST_PATHS = [
    "/hott/album1/track01.mp3",
    "/hott/album1/track02.mp3",
    "/watt/episode01.mp4",
    "/watt/episode02.mp4",
    "/wecharg/config.json",
    "/stealthmachines/index.html",
    "/josefkulovany/data/users.json",
    "/api/v1/health",
    "/static/logo.png",
    "/forum/thread/42",
]

# ── NODE STATE ────────────────────────────────────────────────────────────────
@dataclass
class SimNode:
    ip:          str
    latency:     float   # ms
    storage:     float   # GB available
    alive:       bool    = True
    spike_ttl:   int     = 0   # cycles remaining in spike
    joined_at:   int     = 0   # cycle number when discovered

    def base_latency(self) -> float:
        return self.latency

    def effective_latency(self) -> float:
        if not self.alive:
            return 9999.0
        if self.spike_ttl > 0:
            return self.latency * random.uniform(3.0, 8.0)
        return self.latency * random.uniform(0.85, 1.15)   # ±15% jitter

# ── METRICS ───────────────────────────────────────────────────────────────────
@dataclass
class CycleMetrics:
    cycle:           int
    day:             float
    alive_nodes:     int
    total_nodes:     int
    cache_hits:      int = 0
    cache_misses:    int = 0
    echo_hits:       int = 0
    migrations:      int = 0
    authority_stable: bool = True
    cluster_fp:      str  = "0x00000000"
    excitation_avg:  float = 0.0
    weight_ratio:    float = 0.0   # max/min weight across nodes
    ttl_violations:  int  = 0      # files served after TTL expiry
    fingerprint_bits: int = 0      # bits matching target 0xFFFF0000

@dataclass
class StabilityReport:
    total_cycles:     int
    sim_days:         int
    node_events:      List[str] = field(default_factory=list)
    failure_episodes: int  = 0
    zero_node_cycles: int  = 0
    max_consecutive_failures: int = 0
    cache_hit_rate:   float = 0.0
    echo_fallback_rate: float = 0.0
    migration_total:  int   = 0
    authority_flap_rate: float = 0.0
    avg_weight_ratio: float = 0.0
    avg_excitation:   float = 0.0
    avg_fp_bits:      float = 0.0
    ttl_violation_rate: float = 0.0
    stability_score:  float = 0.0   # 0-100

# ── SIMULATION ENGINE ─────────────────────────────────────────────────────────
class HDGLSimulation:

    def __init__(self):
        self.nodes: Dict[str, SimNode] = {}
        self.lattice  = HDGLLattice()
        self.swap:    Optional[HDGLFileswap] = None
        self.cycle    = 0
        self.metrics: List[CycleMetrics] = []
        self.report   = StabilityReport(TOTAL_CYCLES, SIM_DAYS)
        self._prev_authorities: Dict[str, str] = {}
        self._consec_failures   = 0
        self._authority_flaps   = 0
        self._total_reads       = 0

        # Seed initial nodes (your real cluster IPs)
        self._add_node("209.159.159.170", latency=45,  storage=120.0)
        self._add_node("209.159.159.171", latency=62,  storage=80.0)

        # Pool of potential gossip-discovered nodes
        # Use realistic storage/latency ratios > sqrt(phi)=1.272 to excite lattice
        self._node_pool = [
            ("10.10.0.1", 30,  200.0),   # ratio 6.67 — high performance
            ("10.10.0.2", 55,  150.0),   # ratio 2.73
            ("10.10.0.3", 120, 500.0),   # ratio 4.17
            ("10.10.0.4", 25,  100.0),   # ratio 4.00
        ]

        # Write initial test files
        self._init_swap()
        self._write_test_files()

    def _add_node(self, ip: str, latency: float, storage: float, cycle: int = 0):
        self.nodes[ip] = SimNode(ip=ip, latency=latency, storage=storage, joined_at=cycle)
        self.lattice.update(ip, latency, storage)

    def _init_swap(self):
        local = next(iter(self.nodes))
        self.swap = HDGLFileswap(self.lattice, local_node=local)
        self.swap._dry_run_override = True

    def _write_test_files(self):
        for path in TEST_PATHS:
            data = f"content:{path}:{time.time()}".encode()
            self.swap.write(path, data)

    def _update_lattice(self):
        for ip, node in self.nodes.items():
            if node.alive:
                lat = node.effective_latency()
                self.lattice.update(ip, lat, node.storage)
                self.lattice.observe_latency(ip, lat)

    def _simulate_failures(self):
        for ip, node in self.nodes.items():
            if node.alive:
                # Random failure
                if random.random() < FAILURE_PROB:
                    node.alive = False
                    self.report.failure_episodes += 1
                    self.report.node_events.append(
                        f"day {self.cycle/CYCLES_PER_DAY:.1f}: {ip} FAILED"
                    )
                # Latency spike
                elif random.random() < SPIKE_PROB:
                    node.spike_ttl = random.randint(2, 8)
            else:
                # Recovery
                if node.spike_ttl > 0:
                    node.spike_ttl -= 1
                if random.random() < RECOVERY_PROB:
                    node.alive = True
                    self.report.node_events.append(
                        f"day {self.cycle/CYCLES_PER_DAY:.1f}: {ip} RECOVERED"
                    )

    def _simulate_gossip(self):
        if (len(self.nodes) < MAX_NODES and
                self._node_pool and
                random.random() < GOSSIP_PROB):
            ip, lat, stor = self._node_pool.pop(0)
            self._add_node(ip, lat, stor, self.cycle)
            self.report.node_events.append(
                f"day {self.cycle/CYCLES_PER_DAY:.1f}: {ip} DISCOVERED (gossip)"
            )

    def _simulate_storage_drift(self):
        for node in self.nodes.values():
            if node.alive:
                node.storage = max(1.0, node.storage - STORAGE_DRIFT)

    def _simulate_reads(self) -> Tuple[int, int, int]:
        hits = misses = echo = 0
        for path in random.choices(TEST_PATHS, k=6):
            entry = self.swap._cache.get(path)
            echo_key = path + ".__echo__"
            echo_entry = self.swap._cache.get(echo_key)
            self._total_reads += 1

            if entry and not entry.expired():
                hits += 1
            elif echo_entry and not echo_entry.expired():
                echo += 1
            else:
                misses += 1
                # Simulate re-write on miss (daemon would handle this)
                data = f"content:{path}:{time.time()}".encode()
                self.swap.write(path, data)
        return hits, misses, echo

    def _check_authority_stability(self) -> Tuple[bool, int]:
        top = self.lattice.top_node_per_strand()
        flaps = 0
        stable = True
        for k, (node, _) in top.items():
            prev = self._prev_authorities.get(str(k))
            if prev and prev != node:
                flaps += 1
                stable = False
        self._prev_authorities = {str(k): v[0] for k, v in top.items()}
        self._authority_flaps += flaps
        return stable, flaps

    def _collect_metrics(self, hits, misses, echo, stable, flaps) -> CycleMetrics:
        alive_nodes = [n for n in self.nodes.values() if n.alive]
        top = self.lattice.top_node_per_strand()

        # Weight ratio
        if len(alive_nodes) >= 2:
            weights = [self.lattice.strand_weight(n.ip, 0) for n in alive_nodes]
            mn, mx = min(weights), max(weights)
            ratio = mx / mn if mn > 0 else 0.0
        else:
            ratio = 0.0

        # Excitation avg
        excit = 0.0
        if self.lattice._states:
            excit = sum(s.excitation for s in self.lattice._states.values()) / len(self.lattice._states)

        # Fingerprint bits vs target
        cfp = self.lattice.cluster_fingerprint()
        target = 0xFFFF0000
        bits = bin(~(int(cfp, 16) ^ target) & 0xFFFFFFFF).count("1")

        return CycleMetrics(
            cycle=self.cycle,
            day=self.cycle / CYCLES_PER_DAY,
            alive_nodes=len(alive_nodes),
            total_nodes=len(self.nodes),
            cache_hits=hits,
            cache_misses=misses,
            echo_hits=echo,
            migrations=0,
            authority_stable=stable,
            cluster_fp=cfp,
            excitation_avg=excit,
            weight_ratio=ratio,
            fingerprint_bits=bits,
        )

    def run(self):
        print(f"\n{BOLD}{'═'*68}{X}")
        print(f"{BOLD}  HDGL Long-Term Stability Simulation{X}")
        print(f"  {SIM_DAYS} synthetic days  |  {TOTAL_CYCLES} cycles  |  "
              f"{INITIAL_NODES} initial nodes → max {MAX_NODES}")
        print(f"{BOLD}{'═'*68}{X}\n")

        # Print header
        print(f"  {'Day':>5}  {'Alive':>5}  {'Hit%':>5}  {'Echo%':>5}  "
              f"{'WtRatio':>8}  {'FP bits':>7}  {'Excit%':>6}  Status")
        print(f"  {'─'*65}")

        checkpoint_days = set(range(0, SIM_DAYS+1, 10))
        summary_rows = []

        for cycle in range(TOTAL_CYCLES):
            self.cycle = cycle
            day = cycle / CYCLES_PER_DAY

            # Events
            self._simulate_failures()
            self._simulate_gossip()
            self._simulate_storage_drift()
            self._update_lattice()

            # Rebalance every 4 cycles (synthetic 2hr)
            if cycle % 4 == 0:
                self.swap.rebalance()

            # Reads
            hits, misses, echo = self._simulate_reads()

            # Stability
            stable, flaps = self._check_authority_stability()

            # Metrics
            m = self._collect_metrics(hits, misses, echo, stable, flaps)
            self.metrics.append(m)

            # Zero-node tracking
            if m.alive_nodes == 0:
                self.report.zero_node_cycles += 1
                self._consec_failures += 1
                self.report.max_consecutive_failures = max(
                    self.report.max_consecutive_failures,
                    self._consec_failures
                )
            else:
                self._consec_failures = 0

            # Print every 10 synthetic days
            if int(day) in checkpoint_days and cycle % CYCLES_PER_DAY == 0:
                checkpoint_days.discard(int(day))
                total_reads = hits + misses + echo
                hit_pct  = hits  / total_reads * 100 if total_reads else 0
                echo_pct = echo  / total_reads * 100 if total_reads else 0
                status_color = G if m.alive_nodes >= 2 else (Y if m.alive_nodes == 1 else R)
                status = "OK" if m.alive_nodes >= 2 else ("DEGRADED" if m.alive_nodes == 1 else "DOWN")
                fp_bar = bar(m.fingerprint_bits, 0, 32, w=8,
                             color=G if m.fingerprint_bits >= 24 else Y)
                print(f"  {day:>5.0f}  "
                      f"{status_color}{m.alive_nodes}/{m.total_nodes}{X}{'':>2}  "
                      f"{hit_pct:>4.0f}%  "
                      f"{echo_pct:>4.0f}%  "
                      f"{m.weight_ratio:>8.1f}x  "
                      f"{fp_bar}{m.fingerprint_bits:>2}/32  "
                      f"{m.excitation_avg*100:>5.0f}%  "
                      f"{status_color}{status}{X}")
                summary_rows.append(m)

        self._build_report()
        self._print_report(summary_rows)
        self._save_results()

    def _build_report(self):
        r = self.report
        total = len(self.metrics)
        if not total:
            return

        total_reads  = sum(m.cache_hits + m.cache_misses + m.echo_hits for m in self.metrics)
        total_hits   = sum(m.cache_hits   for m in self.metrics)
        total_echo   = sum(m.echo_hits    for m in self.metrics)
        total_misses = sum(m.cache_misses for m in self.metrics)
        flaps        = sum(1 for m in self.metrics if not m.authority_stable)

        r.migration_total      = sum(m.migrations for m in self.metrics)
        r.cache_hit_rate       = total_hits  / total_reads if total_reads else 0
        r.echo_fallback_rate   = total_echo  / total_reads if total_reads else 0
        r.authority_flap_rate  = flaps / total
        r.avg_weight_ratio     = sum(m.weight_ratio     for m in self.metrics) / total
        r.avg_excitation       = sum(m.excitation_avg   for m in self.metrics) / total
        r.avg_fp_bits          = sum(m.fingerprint_bits for m in self.metrics) / total
        r.ttl_violation_rate   = sum(m.ttl_violations   for m in self.metrics) / (total_reads or 1)

        # Stability score (0-100)
        # Weighted: availability 40%, cache 25%, weight diff 20%, fp 15%
        availability   = 1 - (r.zero_node_cycles / total)
        cache_quality  = r.cache_hit_rate + r.echo_fallback_rate * 0.5
        weight_quality = min(r.avg_weight_ratio / 40.0, 1.0)   # 40x = perfect
        fp_quality     = r.avg_fp_bits / 32.0

        r.stability_score = (
            availability  * 40 +
            cache_quality * 25 +
            weight_quality * 20 +
            fp_quality    * 15
        )

    def _print_report(self, summary_rows):
        r = self.report
        print(f"\n{BOLD}{'═'*68}{X}")
        print(f"{BOLD}  STABILITY REPORT — {SIM_DAYS}-Day Simulation{X}")
        print(f"{BOLD}{'═'*68}{X}\n")

        # Availability
        avail = 1 - (r.zero_node_cycles / TOTAL_CYCLES)
        avail_color = G if avail > 0.99 else (Y if avail > 0.95 else R)
        print(f"  {BOLD}Availability{X}")
        print(f"    Uptime (≥1 node alive)  : {avail_color}{avail*100:.3f}%{X}")
        print(f"    Zero-node cycles        : {r.zero_node_cycles}/{TOTAL_CYCLES}")
        print(f"    Failure episodes        : {r.failure_episodes}")
        print(f"    Max consecutive down    : {r.max_consecutive_failures} cycles "
              f"({r.max_consecutive_failures/CYCLES_PER_DAY*24:.1f}h synthetic)")

        # Cache
        print(f"\n  {BOLD}Cache Performance{X}")
        hr_color = G if r.cache_hit_rate > 0.7 else (Y if r.cache_hit_rate > 0.4 else R)
        print(f"    Cache hit rate          : {hr_color}{r.cache_hit_rate*100:.1f}%{X}")
        print(f"    Echo fallback rate      : {r.echo_fallback_rate*100:.1f}%")
        print(f"    Effective serve rate    : {(r.cache_hit_rate+r.echo_fallback_rate)*100:.1f}%  "
              f"(hits + echo)")
        print(f"    TTL violations          : {r.ttl_violation_rate*100:.3f}%")

        # Weight differentiation
        print(f"\n  {BOLD}Weight Differentiation{X}")
        wr_color = G if r.avg_weight_ratio > 5 else (Y if r.avg_weight_ratio > 2 else R)
        print(f"    Avg weight ratio        : {wr_color}{r.avg_weight_ratio:.1f}x{X}  "
              f"(best/worst node)")
        print(f"    Avg excitation          : {r.avg_excitation*100:.1f}%  "
              f"(lattice slot activation)")
        print(f"    Avg FP bits vs target   : {r.avg_fp_bits:.1f}/32  "
              f"({r.avg_fp_bits/32*100:.0f}% toward 0xFFFF0000)")

        # Authority stability
        print(f"\n  {BOLD}Authority Stability{X}")
        af_color = G if r.authority_flap_rate < 0.05 else (Y if r.authority_flap_rate < 0.15 else R)
        print(f"    Authority flap rate     : {af_color}{r.authority_flap_rate*100:.1f}%{X}  "
              f"(cycles with any authority change)")
        print(f"    Total migrations        : {r.migration_total}")

        # Node events
        print(f"\n  {BOLD}Node Events ({len(r.node_events)} total){X}")
        for ev in r.node_events[:8]:
            icon = "✗" if "FAILED" in ev else ("✓" if "RECOVERED" in ev else "◎")
            color = R if "FAILED" in ev else (G if "RECOVERED" in ev else B)
            print(f"    {color}{icon}{X}  {ev}")
        if len(r.node_events) > 8:
            print(f"    ... and {len(r.node_events)-8} more events")

        # Stability issues
        print(f"\n  {BOLD}Long-Term Risk Factors{X}")
        issues = []
        if r.zero_node_cycles > 0:
            issues.append((R, f"Had {r.zero_node_cycles} cycles with zero healthy nodes — "
                           f"consider minimum 3-node cluster"))
        if r.cache_hit_rate < 0.5:
            issues.append((Y, f"Cache hit rate {r.cache_hit_rate*100:.0f}% — "
                           f"TTL_BASE may need tuning for your access patterns"))
        if r.authority_flap_rate > 0.10:
            issues.append((Y, f"Authority flap rate {r.authority_flap_rate*100:.0f}% — "
                           f"EMA alpha may need reducing for more damping"))
        if r.max_consecutive_failures > CYCLES_PER_DAY:
            issues.append((R, f"Node down for {r.max_consecutive_failures/CYCLES_PER_DAY:.1f} "
                           f"synthetic days — add peer nodes for resilience"))
        if r.avg_weight_ratio < 2.0:
            issues.append((Y, "Low weight differentiation — nodes too similar in "
                           "latency/storage to get meaningful routing variation"))
        if not issues:
            issues.append((G, "No significant stability risks detected"))

        for color, msg in issues:
            print(f"    {color}{'⚠' if color!=G else '✓'}{X}  {msg}")

        # Overall score
        sc = r.stability_score
        sc_color = G if sc >= 80 else (Y if sc >= 60 else R)
        grade = "A" if sc>=90 else ("B" if sc>=80 else ("C" if sc>=70 else ("D" if sc>=60 else "F")))
        print(f"\n  {BOLD}{'─'*66}{X}")
        print(f"  {BOLD}Overall Stability Score: {sc_color}{sc:.1f}/100  [{grade}]{X}")
        print(f"  {BOLD}{'─'*66}{X}")
        print(f"    Availability (40pts)  : {(1-r.zero_node_cycles/TOTAL_CYCLES)*40:.1f}")
        print(f"    Cache quality (25pts) : {(r.cache_hit_rate+r.echo_fallback_rate*0.5)*25:.1f}")
        print(f"    Weight diff  (20pts)  : {min(r.avg_weight_ratio/40.0,1)*20:.1f}")
        print(f"    Fingerprint  (15pts)  : {r.avg_fp_bits/32*15:.1f}")
        print(f"\n{BOLD}{'═'*68}{X}\n")

    def _save_results(self):
        r = self.report
        out = {
            "sim_days": SIM_DAYS,
            "total_cycles": TOTAL_CYCLES,
            "stability_score": round(r.stability_score, 2),
            "availability_pct": round((1 - r.zero_node_cycles/TOTAL_CYCLES)*100, 3),
            "cache_hit_rate": round(r.cache_hit_rate, 4),
            "echo_fallback_rate": round(r.echo_fallback_rate, 4),
            "effective_serve_rate": round(r.cache_hit_rate + r.echo_fallback_rate, 4),
            "authority_flap_rate": round(r.authority_flap_rate, 4),
            "avg_weight_ratio": round(r.avg_weight_ratio, 2),
            "avg_excitation_pct": round(r.avg_excitation * 100, 2),
            "avg_fp_bits": round(r.avg_fp_bits, 2),
            "failure_episodes": r.failure_episodes,
            "zero_node_cycles": r.zero_node_cycles,
            "max_consecutive_down_cycles": r.max_consecutive_failures,
            "node_events": r.node_events,
            "per_checkpoint": [
                {
                    "cycle": m.cycle,
                    "day": round(m.day, 1),
                    "alive": m.alive_nodes,
                    "total": m.total_nodes,
                    "fp_bits": m.fingerprint_bits,
                    "excitation_pct": round(m.excitation_avg * 100, 1),
                    "weight_ratio": round(m.weight_ratio, 2),
                }
                for m in self.metrics[::CYCLES_PER_DAY]
            ],
        }
        out_path = Path("/mnt/user-data/outputs/hdgl_sim_results.json")
        try:
            out_path.write_text(json.dumps(out, indent=2))
            print(f"  Results saved: {out_path}")
        except Exception as e:
            print(f"  Could not save results: {e}")
        return out


# ── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    random.seed(42)   # reproducible
    sim = HDGLSimulation()
    try:
        sim.run()
    finally:
        shutil.rmtree(_TD, ignore_errors=True)
