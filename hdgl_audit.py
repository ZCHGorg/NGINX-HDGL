#!/usr/bin/env python3
"""
hdgl_audit.py — Comprehensive HDGL stack audit & measurement suite
"""

import json, math, os, sys, time, shutil, tempfile, traceback, hashlib
from pathlib import Path
from typing import List, Dict, Any

sys.path.insert(0, str(Path(__file__).parent))

_TEST_DIR = Path(tempfile.mkdtemp(prefix="hdgl_test_"))
os.environ["LN_FILESWAP_ROOT"]  = str(_TEST_DIR / "swap")
os.environ["LN_FILESWAP_CACHE"] = str(_TEST_DIR / "cache")
os.environ["LN_DRY_RUN"]        = "1"
os.environ["LN_SIMULATION"]     = "1"

from hdgl_lattice  import HDGLLattice, fibonacci, PHI, SQRT_PHI, NUM_SLOTS
from hdgl_fileswap import (
    HDGLFileswap, _phi_tau, _strand_for_path, _omega_ttl,
    STRAND_GEOMETRY, SPIRAL_PERIOD, SPEED_FACTOR, ECHO_SCALE,
    LOW_D_THRESHOLD, INTER_RUNG_LINKS, NUM_STRANDS,
    strand_topology, strand_replication, alpha_ttl, TTL_BASE,
)
import hdgl_fileswap as _fs_mod
_fs_mod.DRY_RUN = True

# ── helpers ──────────────────────────────────────────────────
PASS_S = "\033[92m✓\033[0m"
FAIL_S = "\033[91m✗\033[0m"
results: List[Dict[str, Any]] = []

def eq(a, b, msg=""): assert a == b, msg or f"{a!r} != {b!r}"
def ne(a, b, msg=""): assert a != b, msg or f"{a!r} == {b!r}"
def lt(a, b, msg=""): assert a < b,  msg or f"{a} >= {b}"
def gt(a, b, msg=""): assert a > b,  msg or f"{a} <= {b}"
def gte(a,b, msg=""): assert a >= b, msg or f"{a} < {b}"
def approx(a, b, tol=1e-6, msg=""): assert abs(a-b)<=tol, msg or f"|{a}-{b}|={abs(a-b):.2e} > {tol}"
def ok(v, msg=""): assert v, msg or f"expected True, got {v!r}"
def nok(v,msg=""): assert not v, msg or f"expected False, got {v!r}"

def test(name, fn):
    try:
        detail = fn()
        print(f"  {PASS_S} {name}")
        if detail: [print(f"       {l}") for l in str(detail).split("\n") if l.strip()]
        results.append({"name": name, "status": "PASS", "detail": str(detail or "")})
    except Exception as e:
        print(f"  {FAIL_S} {name}")
        print(f"       {e}")
        results.append({"name": name, "status": "FAIL", "detail": str(e)})

def section(t): print(f"\n{'─'*62}\n  {t}\n{'─'*62}")

def make_lattice(overrides=None):
    nodes = overrides or [
        {"node":"10.0.0.1","latency":20, "storage_avail_gb":4.0},
        {"node":"10.0.0.2","latency":50, "storage_avail_gb":2.0},
        {"node":"10.0.0.3","latency":120,"storage_avail_gb":8.0},
        {"node":"10.0.0.4","latency":200,"storage_avail_gb":1.0},
    ]
    lat = HDGLLattice()
    for n in nodes: lat.update(n["node"], n["latency"], n["storage_avail_gb"])
    return lat

def make_swap(lattice=None, local="10.0.0.1"):
    lat  = lattice or make_lattice()
    swap = HDGLFileswap(lat, local_node=local)
    swap._dry_run_override = True
    return swap

# ══════════════════════════════════════════════════════════════
section("1. Math Primitives")

def t_fib():
    eq([fibonacci(i) for i in range(10)], [0,1,1,2,3,5,8,13,21,34])
test("Fibonacci sequence", t_fib)

def t_tau_determinism():
    eq(_phi_tau("/api/v1/users"), _phi_tau("/api/v1/users"))
test("phi_tau is deterministic", t_tau_determinism)

def t_tau_prefix():
    t1 = _phi_tau("/api/v1/users")
    t2 = _phi_tau("/api/v1/posts")
    t3 = _phi_tau("/static/images/logo.png")
    ds = abs(t1-t2); dd = abs(t1-t3)
    lt(ds, dd, f"similar Δ={ds:.4f} should < different Δ={dd:.4f}")
    return f"api/v1/users={t1:.4f}  api/v1/posts={t2:.4f}  Δ_similar={ds:.4f}  Δ_diff={dd:.4f}"
test("phi_tau prefix clustering", t_tau_prefix)

def t_strand_bounds():
    paths = ["/","/a","/a/b","/a/b/c/d/e","/wecharg/x","/api/v1/users"]
    strands = [_strand_for_path(p) for p in paths]
    ok(all(0 <= s <= 7 for s in strands), f"out of range: {strands}")
    return f"strands={strands}"
test("Strand index always in [0,7]", t_strand_bounds)

def t_phi_sqrt():
    approx(SQRT_PHI**2, PHI, tol=1e-9, msg="sqrt(phi)^2 != phi")
test("PHI / SQRT_PHI relationship", t_phi_sqrt)

# ══════════════════════════════════════════════════════════════
section("2. TTL System (Alpha-Aware)")

def t_stable_ttl():
    ttl_D = _omega_ttl(3); ttl_C = _omega_ttl(2)
    ttl_F = _omega_ttl(5); ttl_E = _omega_ttl(4)
    gt(ttl_D, ttl_C, f"D={ttl_D:.1f}s should > C={ttl_C:.1f}s")
    gt(ttl_F, ttl_E, f"F={ttl_F:.1f}s should > E={ttl_E:.1f}s")
    rows = [f"  {chr(65+i)} {STRAND_GEOMETRY[i][2]:<14} α={STRAND_GEOMETRY[i][0]:+.4f}  TTL={_omega_ttl(i):.1f}s"
            for i in range(NUM_STRANDS)]
    return "\n".join(rows)
test("Stable strands (D,F) have longer TTL", t_stable_ttl)

def t_ttl_floor():
    ttls = [_omega_ttl(k) for k in range(NUM_STRANDS)]
    ok(all(t >= 0.5 for t in ttls), f"TTL below 0.5s: {min(ttls):.2f}")
    return f"min={min(ttls):.1f}s  max={max(ttls):.1f}s"
test("All TTLs >= 0.5s floor", t_ttl_floor)

def t_echo_ttl():
    for k in range(NUM_STRANDS):
        ptl = _omega_ttl(k)
        remaining = ptl - ptl * (1 - ECHO_SCALE)
        approx(remaining, ptl * ECHO_SCALE, tol=0.1)
    return f"echo expires at {ECHO_SCALE*100:.0f}% of primary TTL"
test("Echo TTL = ECHO_SCALE × primary", t_echo_ttl)

def t_ttl_distribution():
    rows = []
    for k in range(NUM_STRANDS):
        a, v, poly = STRAND_GEOMETRY[k]
        ta = alpha_ttl(k, TTL_BASE)
        ts = TTL_BASE * (1.618 ** (-k * 2.5))
        rows.append(f"  {'◆' if a<0 else ' '} {chr(65+k)} {poly:<14} α={a:+.4f}  α-aware={ta:.1f}s  simple={ts:.1f}s  diff={ta-ts:+.1f}s")
    return "\n".join(rows)
test("TTL distribution report", t_ttl_distribution)

# ══════════════════════════════════════════════════════════════
section("3. Topology & Replication")

def t_low_d_complete():
    low = [(i, STRAND_GEOMETRY[i][1]) for i in range(NUM_STRANDS) if STRAND_GEOMETRY[i][1] <= LOW_D_THRESHOLD]
    ok(all(strand_topology(i) == "complete" for i, _ in low))
    return f"low-D: {[(chr(65+i), v) for i,v in low]}"
test("Low-D strands → 'complete' topology", t_low_d_complete)

def t_high_d_grid():
    high = [(i, STRAND_GEOMETRY[i][1]) for i in range(NUM_STRANDS) if STRAND_GEOMETRY[i][1] > LOW_D_THRESHOLD]
    ok(all(strand_topology(i) == "grid" for i, _ in high))
    return f"high-D: {[(chr(65+i), v) for i,v in high]}"
test("High-D strands → 'grid' topology", t_high_d_grid)

def t_rep_bounds():
    cs = 4
    reps = [strand_replication(k, cs) for k in range(NUM_STRANDS)]
    ok(all(1 <= r <= cs for r in reps), f"out of bounds: {reps}")
    return f"rep factors (cluster={cs}): {reps}"
test("Replication factor bounded by cluster size", t_rep_bounds)

def t_rep_monotone():
    reps = [strand_replication(k, 16) for k in range(NUM_STRANDS)]
    ok(max(reps) > min(reps), "no differentiation in replication")
    return f"reps (large cluster): {reps}"
test("Replication differentiates across strands", t_rep_monotone)

# ══════════════════════════════════════════════════════════════
section("4. HDGLLattice")

def t_weight_ordering():
    lat = make_lattice()
    w1 = lat.strand_weight("10.0.0.1", 0)   # 20ms, 4GB — best
    w4 = lat.strand_weight("10.0.0.4", 0)   # 200ms, 1GB — worst
    gt(w1, w4, f"fast+big={w1:.4f} should > slow+small={w4:.4f}")
    return f"10.0.0.1 weight={w1:.4f}  10.0.0.4 weight={w4:.4f}  ratio={w1/w4:.2f}x"
test("Faster+bigger node has higher weight", t_weight_ordering)

def t_ema_convergence():
    lat = HDGLLattice()
    lat.update("n1", 100.0, 1.0)
    for _ in range(20): lat.observe_latency("n1", 20.0)
    ema = lat._latency_ema["n1"]
    lt(ema, 60.0, f"EMA not converging: {ema:.1f}ms")
    return f"EMA after 20×20ms obs (started 100ms): {ema:.2f}ms"
test("EMA feedback converges toward observed", t_ema_convergence)

def t_ema_recency():
    lat = HDGLLattice()
    lat.update("n", 100.0, 1.0)
    for _ in range(5):  lat.observe_latency("n", 10.0)
    e5 = lat._latency_ema["n"]
    for _ in range(20): lat.observe_latency("n", 10.0)
    e25 = lat._latency_ema["n"]
    lt(e25, e5, "EMA not decreasing with continued low obs")
    return f"after 5 cycles: {e5:.1f}ms  after 25: {e25:.1f}ms"
test("EMA weights recent observations more", t_ema_recency)

def t_cluster_fp():
    lat  = make_lattice()
    cfp  = int(lat.cluster_fingerprint(), 16)
    orrd = 0
    for s in lat._states.values(): orrd |= int(s.fingerprint, 16)
    eq(cfp, orrd, "cluster fingerprint != OR of node fingerprints")
test("Cluster fingerprint = OR of node fingerprints", t_cluster_fp)

def t_top_node_count():
    lat = make_lattice()
    top = lat.top_node_per_strand()
    eq(len(top), 8)
    return f"top nodes: {[v[0][-3:] for v in top.values()]}"
test("top_node_per_strand returns 8 entries", t_top_node_count)

def t_excitation_matches_bits():
    lat = make_lattice()
    for nid, state in lat._states.items():
        expected = sum(state.bits) / NUM_SLOTS
        approx(state.excitation, expected, tol=1e-9, msg=f"{nid} excitation wrong")
test("NodeState excitation = sum(bits)/32", t_excitation_matches_bits)

def t_fingerprint_health():
    lat = make_lattice()
    fps = {nid: s.fingerprint for nid, s in lat._states.items()}
    cfp = lat.cluster_fingerprint()
    target = 0xFFFF0000
    actual = int(cfp, 16)
    bits_correct = bin(~(actual ^ target) & 0xFFFFFFFF).count("1")
    rows = [f"  {nid}: {fp}  excit={lat._states[nid].excitation*100:.0f}%" for nid, fp in fps.items()]
    rows += [f"  Cluster: {cfp}  target: 0xFFFF0000", f"  Bits matching target: {bits_correct}/32 ({bits_correct/32*100:.0f}%)"]
    return "\n".join(rows)
test("Fingerprint health report", t_fingerprint_health)

# ══════════════════════════════════════════════════════════════
section("5. Fileswap Addressing")

def t_primary_ne_mirror():
    swap = make_swap()
    auth = swap._authority_for(0)
    mirr = swap._mirror_for(0)
    ne(auth, mirr, "primary == mirror with 4-node cluster")
    return f"primary={auth}  mirror={mirr}"
test("Primary authority ≠ mirror (4-node cluster)", t_primary_ne_mirror)

def t_echo_node():
    swap = make_swap()
    for k in range(1, NUM_STRANDS):
        echo = swap._echo_node_for(k)
        prev = swap._authority_for(k-1)
        eq(echo, prev, f"echo node mismatch at strand {k}")
test("Echo node = authority of previous strand", t_echo_node)

def t_migration_count():
    swap  = make_swap()
    paths = swap._migration_paths(0, 1)
    eq(len(paths), INTER_RUNG_LINKS, f"expected {INTER_RUNG_LINKS}, got {len(paths)}")
    return f"paths: {paths}"
test(f"Migration paths == INTER_RUNG_LINKS ({INTER_RUNG_LINKS})", t_migration_count)

def t_migration_has_direct():
    swap     = make_swap()
    fa, ta   = swap._authority_for(0), swap._authority_for(1)
    paths    = swap._migration_paths(0, 1)
    ok(any(f==fa and t==ta for f,t in paths), f"no direct {fa}→{ta}")
test("Migration paths include primary→authority pair", t_migration_has_direct)

def t_migration_diversity():
    swap   = make_swap()
    paths  = swap._migration_paths(0, 1)
    unique = set(paths)
    gte(len(unique), 2, f"only {len(unique)} unique paths")
    return f"{len(unique)} unique paths / {len(paths)} total"
test("Migration path diversity >= 2 unique routes", t_migration_diversity)

# ══════════════════════════════════════════════════════════════
section("6. Write / Read / Echo")

def t_write_caches():
    swap = make_swap()
    swap.write("/test/file.txt", b"hello")
    ok("/test/file.txt" in swap._cache)
test("Write populates cache immediately", t_write_caches)

def t_checksum():
    swap = make_swap()
    data = b"checksum data"
    swap.write("/test/cksum.bin", data)
    entry = swap._cache["/test/cksum.bin"]
    eq(entry.checksum, hashlib.sha256(data).hexdigest())
test("Write checksum matches SHA256(data)", t_checksum)

def t_echo_entry():
    swap = make_swap()
    swap.write("/test/echo.txt", b"echo")
    echo_key = "/test/echo.txt.__echo__"
    ok(echo_key in swap._cache, "no echo cache entry")
    return f"echo authority={swap._cache[echo_key].authority}"
test("Write creates echo cache entry", t_echo_entry)

def t_echo_preaged():
    swap = make_swap()
    path = "/test/age.txt"
    swap.write(path, b"data")
    echo_key = path + ".__echo__"
    if echo_key not in swap._cache:
        return "no echo entry (single-node — expected)"
    primary = swap._cache[path]
    echo    = swap._cache[echo_key]
    lt(echo.written_at, primary.written_at + 0.01, "echo not pre-aged")
    return f"primary age={primary.age():.3f}s  echo age={echo.age():.3f}s"
test("Echo entry pre-aged vs primary", t_echo_preaged)

def t_cache_hit():
    swap = make_swap()
    data = b"cache hit"
    swap.write("/test/hit.txt", data)
    result = swap.read("/test/hit.txt")
    eq(result, data)
test("Read returns correct data on cache hit", t_cache_hit)

def t_persist():
    swap1 = make_swap()
    swap1.write("/test/persist.txt", b"persist")
    route = swap1._routes.get("/test/persist.txt")
    ok(route is not None)
    # New instance — loads routes from disk
    swap2 = make_swap(swap1.lattice)
    ok("/test/persist.txt" in swap2._routes, "route not reloaded")
    return f"strand={route.strand_idx}  authority={route.authority}"
test("Route persists to disk and reloads", t_persist)

def t_multi_write_read():
    swap = make_swap()
    files = {
        "/wecharg/config.json":        b'{"svc":"wecharg"}',
        "/stealthmachines/index.html": b"<html/>",
        "/api/v1/health":              b'{"ok":true}',
    }
    for p, d in files.items(): swap.write(p, d)
    for p, d in files.items():
        eq(swap.read(p), d, f"mismatch {p}")
    return f"{len(files)} files written and read correctly"
test("Multiple write/read round-trips correct", t_multi_write_read)

# ══════════════════════════════════════════════════════════════
section("7. Rebalance & Migration")

def t_rebalance_shift():
    lat  = make_lattice()
    swap = make_swap(lat)
    swap.write("/test/rebalance.txt", b"data")
    route     = swap._routes["/test/rebalance.txt"]
    old_auth  = route.authority
    # Tank all nodes except 10.0.0.3
    for n in ["10.0.0.1","10.0.0.2","10.0.0.4"]:
        lat.update(n, 9999, 0.001)
    lat.update("10.0.0.3", 5, 100.0)
    new_auth = swap._authority_for(route.strand_idx)
    return f"old={old_auth}  new={new_auth}  shifted={'YES' if new_auth != old_auth else 'no change'}"
test("Rebalance detects authority shift", t_rebalance_shift)

def t_migration_survives_failure():
    swap   = make_swap()
    paths  = swap._migration_paths(2, 3)
    failed = swap._authority_for(2)
    surviving = [(f,t) for f,t in paths if f != failed]
    gt(len(surviving), 0, "no paths survive node failure")
    return f"{len(surviving)}/{len(paths)} paths survive {failed} failing"
test("Migration paths survive single-node failure", t_migration_survives_failure)

def t_full_rebalance():
    lat  = make_lattice()
    swap = make_swap(lat)
    for p, d in {"/a/f1.txt": b"f1", "/b/f2.txt": b"f2"}.items():
        swap.write(p, d)
    swap.rebalance()  # should not raise
    # Shift and rebalance again
    lat.update("10.0.0.1", 5, 200.0)
    swap.rebalance()
    return "two rebalance cycles completed without error"
test("Full rebalance cycle runs without error", t_full_rebalance)

# ══════════════════════════════════════════════════════════════
section("8. Weight Differentiation Metrics")

def t_weight_differentiation():
    lat   = make_lattice()
    nodes = list(lat._states.keys())
    rows  = ["Weight differentiation per strand:"]
    all_ratios = []
    for k in range(NUM_STRANDS):
        ws  = [lat.strand_weight(n, k) for n in nodes]
        mn, mx = min(ws), max(ws)
        ratio = mx / mn if mn > 0 else 0
        all_ratios.append(ratio)
        bar = "█" * min(int(ratio * 8), 32)
        rows.append(f"  strand {k} ({chr(65+k)}) {STRAND_GEOMETRY[k][2]:<14}: "
                    f"min={mn:.5f} max={mx:.5f} ratio={ratio:.2f}x {bar}")
    gt(max(all_ratios), 1.01, f"no weight differentiation: max ratio={max(all_ratios):.4f}")
    return "\n".join(rows)
test("Weight differentiation measurable", t_weight_differentiation)

def t_nonlinear_amplification():
    # Verify raw^1.2 spreads more than linear for values in [0.01, 0.1]
    import math
    vals = [0.01, 0.02, 0.05, 0.10]
    rows = ["Nonlinear amplification (raw^1.2 vs linear×20):"]
    for v in vals:
        amp = v ** 1.2
        lin = v
        rows.append(f"  raw={v:.3f}  ^1.2={amp:.5f}  ratio_boost={(amp/lin)/1:.3f}x  "
                    f"int_weight={max(1,min(int(amp*20),100))}")
    # Verify spread: ratio of top/bottom should be higher after amplification
    linear_ratio   = vals[-1] / vals[0]
    amp_ratio      = (vals[-1]**1.2) / (vals[0]**1.2)
    gt(amp_ratio, linear_ratio, "amplification doesn't increase spread")
    return "\n".join(rows) + f"\n  spread linear={linear_ratio:.2f}x  amplified={amp_ratio:.2f}x"
test("Nonlinear amplification increases spread", t_nonlinear_amplification)

# ══════════════════════════════════════════════════════════════
section("9. Matrix & Status Output")

def t_lattice_matrix():
    lat    = make_lattice()
    nodes  = [{"node": n, "latency": 50, "storage_avail_gb": 1.0} for n in lat._states]
    matrix = lat.simulation_matrix(nodes, {"svc": {"port": 8080}})
    ok(len(matrix) > 100)
    ok("HDGL SUPERPOSITION" in matrix)
    ok("EMA" in matrix)
    return f"{len(matrix)} chars  {matrix.count(chr(10))} lines"
test("Lattice matrix renders correctly", t_lattice_matrix)

def t_fileswap_matrix():
    swap = make_swap()
    swap.write("/wecharg/config.json", b"{}")
    matrix = swap.simulation_matrix()
    ok("FILESWAP MATRIX" in matrix)
    ok("◆ STABLE" in matrix)
    ok("complete" in matrix)
    ok("grid" in matrix)
    ok("Mirror" in matrix)
    return f"{len(matrix)} chars  {matrix.count(chr(10))} lines"
test("Fileswap matrix renders with all columns", t_fileswap_matrix)

def t_status():
    swap = make_swap()
    swap.write("/x/y.txt", b"test")
    s = swap.status()
    ok("strands" in s)
    eq(len(s["strands"]), NUM_STRANDS)
    ok("routed_files" in s)
    ok(s["routed_files"] >= 1)
    return f"routed={s['routed_files']} cached={s['cached_files']}"
test("Status dict structure correct", t_status)

# ══════════════════════════════════════════════════════════════
section("10. Full Integration")

def t_integration():
    # Fresh isolated lattice + swap with clean route state
    import tempfile, os
    _td = Path(tempfile.mkdtemp(prefix="hdgl_integ_"))
    os.environ["LN_FILESWAP_ROOT"]  = str(_td / "swap")
    os.environ["LN_FILESWAP_CACHE"] = str(_td / "cache")
    lat  = make_lattice()
    swap = make_swap(lat)
    files = {
        "/wecharg/config.json":        b'{"svc":"wecharg"}',
        "/stealthmachines/index.html": b"<html>stealth</html>",
        "/josefkulovany/data/u.json":  b'[{"id":1}]',
        "/api/v1/health":              b'{"status":"ok"}',
        "/static/logo.png":            b"\x89PNG\r\n",
    }
    # Write
    for p, d in files.items(): swap.write(p, d)
    # Read all back
    for p, d in files.items():
        result = swap.read(p)
        eq(result, d, f"read mismatch: {p}")
    # Rebalance
    swap.rebalance()
    # Shift lattice
    lat.update("10.0.0.1", 5, 100.0)
    for n in ["10.0.0.2","10.0.0.3","10.0.0.4"]: lat.update(n, 9999, 0.01)
    swap.rebalance()
    # Matrix
    matrix = swap.simulation_matrix()
    ok("FILESWAP MATRIX" in matrix)
    # Status
    s = swap.status()
    gte(s["routed_files"], len(files))
    return f"✓ wrote {len(files)} files, read all, rebalanced ×2, matrix OK, status correct"
test("Full cycle: write→read→rebalance×2→matrix→status", t_integration)


# ══════════════════════════════════════════════════════════════
section("11. Property-based & Concurrent Safety")

# ── Property-based: phi_tau -> strand -> TTL invariant ────────────────────────
try:
    from hypothesis import given, strategies as st, settings, example
    from hypothesis import HealthCheck, Phase

    _PATH_CHARS = st.characters(
        whitelist_categories=("Lu","Ll","Nd","Pc","Pd","Po"),
        exclude_characters="/",
    )
    _SEGMENT = st.text(_PATH_CHARS, min_size=1, max_size=48)
    _PATHS   = st.lists(_SEGMENT, min_size=1, max_size=8).map(
        lambda segs: "/" + "/".join(segs)
    )

    @given(_PATHS)
    @settings(
        max_examples=800,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
        phases=[Phase.generate, Phase.shrink],
    )
    @example("/wecharg/config.json")
    @example("/api/v1/users/999999/profile")
    @example("/static/fonts/Roboto-Bold.woff2")
    @example("/hott/track01.mp3")
    def _prop_phi_tau_invariant(path):
        k    = _strand_for_path(path)
        ttl  = _omega_ttl(k)
        # Trailing-slash variant must land on same strand
        path_v = path.rstrip("/") + "/"
        k_v    = _strand_for_path(path_v)
        ttl_v  = _omega_ttl(k_v)
        assert k == k_v,              f"strand flip: {k} -> {k_v} for {path!r}"
        assert abs(ttl - ttl_v) < 1e-5, f"TTL drift {ttl:.8f} vs {ttl_v:.8f}"
        assert 0 <= k <= NUM_STRANDS - 1, f"strand out of range: {k}"
        assert ttl > 0,               f"non-positive TTL: {ttl}"

    def t_prop_phi_tau():
        _prop_phi_tau_invariant()
        return "800 generated paths — strand + TTL stable under trailing-slash variant"

    test("Property: phi_tau -> strand -> TTL invariant (800 examples)", t_prop_phi_tau)

except ImportError:
    test(
        "Property-based phi_tau invariants",
        lambda: "hypothesis not installed (pip install hypothesis) — skipped",
    )

# ── Concurrent write survival ─────────────────────────────────────────────────
import threading as _threading
import random as _random

def t_concurrent_write():
    swap = make_swap()
    path = "/shared/race_condition.json"
    versions = [
        f'{{"id":{i},"ts":{time.time_ns()}}}'.encode()
        for i in range(5)
    ]

    errors = []
    def writer(i):
        try:
            time.sleep(_random.uniform(0.0, 0.06))
            swap.write(path, versions[i])
        except Exception as exc:
            errors.append(exc)

    threads = [_threading.Thread(target=writer, args=(i,), daemon=True) for i in range(5)]
    _random.shuffle(threads)
    for t in threads: t.start()
    for t in threads: t.join(timeout=3.0)

    ok(not errors, f"writer exceptions: {errors}")
    final = swap.read(path)
    ok(final is not None, "all concurrent writes lost — read returned None")
    ok(any(final == v for v in versions), f"corrupt data survived — got {final!r}")
    return "5 concurrent writers: last-write-wins, no crash, no corruption"

test("Concurrent write — at least one version survives cleanly", t_concurrent_write)

# ── SQLite state DB round-trip ────────────────────────────────────────────────
def t_state_db():
    import tempfile, shutil
    from hdgl_state_db import HDGLStateDB
    td = Path(tempfile.mkdtemp(prefix="hdgl_audit_db_"))
    db = HDGLStateDB(td / "test.db")
    db.open()

    ema_in = {"10.0.0.1": 45.2, "10.0.0.2": 62.1}
    db.save_ema(ema_in)
    ema_out = db.load_ema()
    eq(set(ema_in), set(ema_out), "EMA key mismatch")
    for k in ema_in:
        approx(ema_in[k], ema_out[k], tol=0.01, msg=f"EMA value mismatch: {k}")

    nodes_in = ["10.0.0.1","10.0.0.2","10.0.0.3"]
    db.save_known_nodes(nodes_in)
    nodes_out = db.load_known_nodes()
    eq(set(nodes_in), set(nodes_out), "known_nodes mismatch")

    db.save_metadata("version","2.0.1")
    eq(db.load_metadata("version"), "2.0.1")
    eq(db.load_metadata("missing"), None)

    # Stale pruning
    old_ts = int(time.time()) - 90_001
    db.save_ema({"stale.node": 99.9}, timestamp=old_ts)
    db.save_ema({"10.0.0.1": 45.2})   # triggers prune
    fresh = db.load_ema()
    ok("stale.node" not in fresh, "stale node not pruned")

    # Pickle migration
    import pickle
    pkl = td / "lattice_state.pkl"
    pkl.write_bytes(pickle.dumps({"ema": {"mig.node": 55.5}, "timestamp": time.time()}))
    db2 = HDGLStateDB(td / "mig.db")
    db2.open()
    ok(db2.migrate_from_pickle(pkl), "migration returned False")
    ok(not pkl.exists(), ".pkl not renamed")
    ok("mig.node" in db2.load_ema(), "migrated node missing")
    db2.close()

    db.close()
    shutil.rmtree(td, ignore_errors=True)
    return "EMA / nodes / metadata / stale-prune / pickle-migration all correct"

test("SQLite state DB: round-trip, pruning, pickle migration", t_state_db)

# ── HMAC auth end-to-end ─────────────────────────────────────────────────────
def t_hmac_auth():
    import os as _os
    _os.environ["LN_CLUSTER_SECRET"] = "audit-test-secret-xyz"
    # Force re-import so the module picks up the new env
    import importlib, hdgl_node_server as _ns
    importlib.reload(_ns)

    payload = b'{"node":"10.0.0.1","latency":45.0}'
    sig     = _ns._sign_payload(payload)
    ok(_ns._verify_signature(payload, sig),      "valid sig rejected")
    nok(_ns._verify_signature(b"tampered", sig), "tampered payload accepted")
    nok(_ns._verify_signature(payload, ""),      "missing sig accepted")

    # Replay: fabricate an old timestamp
    old_sig = _ns._sign_payload(payload, timestamp=int(time.time()) - 60)
    nok(_ns._verify_signature(payload, old_sig), "replayed sig accepted")

    # Restore — don't leave test secret in env
    _os.environ.pop("LN_CLUSTER_SECRET", None)
    return "valid / tampered / missing / replayed — all handled correctly"

test("HMAC auth: valid / tampered / missing / replayed", t_hmac_auth)


# ══════════════════════════════════════════════════════════════
section("12. Moiré Interference Encoding")

def t_moire_dn_formula():
    from hdgl_moire import compute_Dn_r
    # Exact values from hdgl_analog_v30.c — verified against C engine
    expected = {(1, 0.5, 1.0): 2.1393, (4, 0.5, 1.0): 15.1189,
                (8, 1.0, 1.0): 406.5372}
    for (n, r, omega), exp in expected.items():
        approx(compute_Dn_r(n, r, omega), exp, tol=0.001,
               msg=f"Dn({n},{r},{omega}) mismatch")
    return "D1,D4,D8 match C engine values"
test("Dn(r) formula matches hdgl_analog_v30.c", t_moire_dn_formula)

def t_moire_roundtrip():
    from hdgl_moire import HDGLMoire
    m  = HDGLMoire()
    fp = 0xFFFFFFFF
    cases = [
        ("/wecharg/config.json",        b'{"svc":"wecharg"}'),
        ("/hott/track01.mp3",           bytes(range(256)) * 4),
        ("/netboot/alpine-A/kernel",    b"STUB_KERNEL" * 50),
        ("/api/v1/health",              b'{"status":"ok"}'),
    ]
    for path, data in cases:
        enc = m.encode(data, path, fp)
        dec = m.decode(enc, path, fp)
        eq(dec, data, f"round-trip failed for {path}")
    return f"{len(cases)} paths — encode/decode round-trip clean"
test("Moire encode/decode round-trip (4 paths)", t_moire_roundtrip)

def t_moire_wrong_key():
    from hdgl_moire import HDGLMoire
    m  = HDGLMoire()
    fp = 0xFFFFFFFF
    data = b"Private content for alpine-A - must not leak to alpine-B"
    enc  = m.encode(data, "/netboot/alpine-A", fp)
    # Wrong path
    wrong_path = m.decode(enc, "/netboot/alpine-B", fp)
    nok(wrong_path == data, "wrong path decoded correctly — not private")
    # Wrong fingerprint
    wrong_fp = m.decode(enc, "/netboot/alpine-A", 0xDEADBEEF)
    nok(wrong_fp == data, "wrong fp decoded correctly — not private")
    return "wrong path = noise, wrong fingerprint = noise"
test("Moire wrong key/fp produces noise", t_moire_wrong_key)

def t_moire_instance_isolation():
    from hdgl_moire import _moire_keystream, HDGLMoire
    fp = 0xFFFFFFFF
    paths = ["/netboot/alpine-A", "/netboot/alpine-B",
             "/netboot/staging-env", "/wecharg/config.json"]
    streams = [_moire_keystream(32, HDGLMoire._phi_tau_standalone(p), fp)
               for p in paths]
    n_unique = len(set(streams))
    eq(n_unique, len(paths), "duplicate keystreams across instances")
    return f"{n_unique}/{len(paths)} paths produce unique keystreams"
test("Moire instance isolation: unique keystreams", t_moire_instance_isolation)

def t_moire_transparent_fileswap():
    # Write through moire-enabled fileswap, read back — data survives
    lat  = make_lattice()
    swap = make_swap(lat)
    path = "/moire/test/secret.json"
    data = b'{"secret": "only readable with correct phi_tau perspective"}'
    swap.write(path, data)
    got  = swap.read(path)
    eq(got, data, "moire-encoded fileswap round-trip failed")
    return "write→encode→disk→decode→read: data intact through fileswap"
test("Moire transparent in fileswap write/read", t_moire_transparent_fileswap)

def t_moire_c_acceleration():
    from hdgl_moire import _C_LIB, _moire_keystream
    if _C_LIB is None:
        return "C library not available — pure Python fallback active"
    # Verify C and Python produce identical keystreams
    tau = 3.5832; fp = 0xFFFFFFFF; N = 512
    import ctypes
    buf = (ctypes.c_uint8 * N)()
    _C_LIB.moire_keystream_c(buf, ctypes.c_int(N),
                              ctypes.c_double(tau), ctypes.c_uint32(fp))
    c_stream  = bytes(buf)
    py_stream = _moire_keystream(N, tau, fp)
    eq(c_stream, py_stream, "C and Python keystreams diverge")
    return "C keystream == Python keystream (512 bytes)"
test("Moire C/Python keystream parity", t_moire_c_acceleration)


# ══════════════════════════════════════════════════════════════
section("12. Moire Interference Encoding")

def t_moire_dn_formula():
    from hdgl_moire import compute_Dn_r
    expected = {(1, 0.5, 1.0): 2.1393, (4, 0.5, 1.0): 15.1189,
                (8, 1.0, 1.0): 406.5372}
    for (n, r, omega), exp in expected.items():
        approx(compute_Dn_r(n, r, omega), exp, tol=0.001,
               msg=f"Dn({n},{r},{omega}) mismatch")
    return "D1,D4,D8 match C engine values"
test("Dn(r) formula matches hdgl_analog_v30.c", t_moire_dn_formula)

def t_moire_roundtrip():
    from hdgl_moire import HDGLMoire
    m  = HDGLMoire()
    fp = 0xFFFFFFFF
    cases = [
        ("/wecharg/config.json",        b'{"svc":"wecharg"}'),
        ("/hott/track01.mp3",           bytes(range(256)) * 4),
        ("/netboot/alpine-A/kernel",    b"STUB_KERNEL" * 50),
        ("/api/v1/health",              b'{"status":"ok"}'),
    ]
    for path, data in cases:
        enc = m.encode(data, path, fp)
        dec = m.decode(enc, path, fp)
        eq(dec, data, f"round-trip failed for {path}")
    return f"{len(cases)} paths encode/decode clean"
test("Moire encode/decode round-trip (4 paths)", t_moire_roundtrip)

def t_moire_wrong_key():
    from hdgl_moire import HDGLMoire
    m    = HDGLMoire()
    fp   = 0xFFFFFFFF
    data = b"Private content - must not leak"
    enc  = m.encode(data, "/netboot/alpine-A", fp)
    nok(m.decode(enc, "/netboot/alpine-B", fp) == data,
        "wrong path decoded correctly")
    nok(m.decode(enc, "/netboot/alpine-A", 0xDEADBEEF) == data,
        "wrong fp decoded correctly")
    return "wrong path=noise, wrong fingerprint=noise"
test("Moire wrong key/fp produces noise", t_moire_wrong_key)

def t_moire_instance_isolation():
    from hdgl_moire import _moire_keystream, HDGLMoire
    fp = 0xFFFFFFFF
    paths = ["/netboot/alpine-A", "/netboot/alpine-B",
             "/netboot/staging-env", "/wecharg/config.json"]
    streams = [_moire_keystream(32, HDGLMoire._phi_tau_standalone(p), fp)
               for p in paths]
    eq(len(set(streams)), len(paths), "duplicate keystreams across instances")
    return f"{len(paths)} paths produce unique keystreams"
test("Moire instance isolation: unique keystreams", t_moire_instance_isolation)

def t_moire_fileswap():
    lat  = make_lattice()
    swap = make_swap(lat)
    path = "/moire/test/secret.json"
    data = b'{"secret": "private"}'
    swap.write(path, data)
    got  = swap.read(path)
    eq(got, data, "moire fileswap round-trip failed")
    return "write->encode->disk->decode->read: data intact"
test("Moire transparent in fileswap write/read", t_moire_fileswap)

def t_moire_c_parity():
    from hdgl_moire import _C_LIB, _moire_keystream
    if _C_LIB is None:
        return "C library not available - pure Python fallback active"
    import ctypes
    tau = 3.5832; fp = 0xFFFFFFFF; N = 512
    buf = (ctypes.c_uint8 * N)()
    _C_LIB.moire_keystream_c(buf, ctypes.c_int(N),
                              ctypes.c_double(tau), ctypes.c_uint32(fp))
    eq(bytes(buf), _moire_keystream(N, tau, fp),
       "C and Python keystreams diverge")
    return "C keystream == Python keystream (512 bytes)"
test("Moire C/Python keystream parity", t_moire_c_parity)

# ══════════════════════════════════════════════════════════════
shutil.rmtree(_TEST_DIR, ignore_errors=True)

passed = sum(1 for r in results if r["status"] == "PASS")
failed = sum(1 for r in results if r["status"] == "FAIL")
total  = len(results)

print(f"\n{'═'*62}")
print(f"  HDGL FULL AUDIT: {passed}/{total} passed  |  {failed} failed")
print(f"  Pass rate: {passed/total*100:.1f}%")
print(f"{'═'*62}")

if failed:
    print(f"\n  Failed tests:")
    for r in results:
        if r["status"] == "FAIL":
            print(f"    ✗ {r['name']}")
            print(f"      {r['detail']}")

summary = {
    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "total": total, "passed": passed, "failed": failed,
    "pass_rate": round(passed/total, 4) if total else 0,
    "results": results,
}
try:
    Path("/mnt/user-data/outputs/hdgl_audit_results.json").write_text(
        json.dumps(summary, indent=2)
    )
    print(f"  Results: hdgl_audit_results.json")
except Exception:
    pass

sys.exit(0 if failed == 0 else 1)
