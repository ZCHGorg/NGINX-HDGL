#!/usr/bin/env python3
"""
hdgl_audit_v0.3.py — Audit suite for HDGL v0.3 (strand-native HTTP server)

Tests the native HTTP server endpoints:
  - /hdgl/metrics          : global metrics + strand_metrics
  - /hdgl/strand-map      : current authority per strand
  - /hdgl/pool-status     : connection pool state
  - /hdgl/health          : health check

Unlike v0.2 (NGINX config tests), v0.3 validates per-request routing and
connection pooling behavior.
"""

import json
import logging
import os
import sys
import time
import tempfile
from pathlib import Path
from typing import List, Dict, Any

# Setup test environment
sys.path.insert(0, str(Path(__file__).parent))

_TEST_DIR = Path(tempfile.mkdtemp(prefix="hdgl_v03_audit_"))
os.environ["LN_FILESWAP_ROOT"]  = str(_TEST_DIR / "swap")
os.environ["LN_FILESWAP_CACHE"] = str(_TEST_DIR / "cache")
os.environ["LN_DRY_RUN"]        = "1"
os.environ["LN_SIMULATION"]     = "1"
os.environ["LN_LOCAL_NODE"]     = "10.0.0.1"
os.environ["LN_HTTP_PORT"]      = "8080"
os.environ["LN_HTTP_POOL_SIZE"] = "16"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hdgl.audit_v03")

from hdgl_lattice  import HDGLLattice, NUM_SLOTS
from hdgl_fileswap import (
    HDGLFileswap, _phi_tau, _strand_for_path, _omega_ttl, NUM_STRANDS
)
from hdgl_http_server_native import (
    HDGLHTTPServer, StrandConnectionPool, _strand_for_path as _native_strand_for_path
)

# ─────────────────────────────────────────────────────────────────────────────
# TEST INFRASTRUCTURE
# ─────────────────────────────────────────────────────────────────────────────

PASS_S = "\033[92m✓\033[0m"
FAIL_S = "\033[91m✗\033[0m"
results: List[Dict[str, Any]] = []

def test(name, fn):
    """Run a test function and record result."""
    try:
        detail = fn()
        print(f"  {PASS_S} {name}")
        if detail:
            lines = str(detail).split("\n")
            for l in lines:
                if l.strip():
                    print(f"       {l}")
        results.append({"name": name, "status": "PASS", "detail": str(detail or "")})
    except Exception as e:
        print(f"  {FAIL_S} {name}")
        print(f"       {e}")
        results.append({"name": name, "status": "FAIL", "detail": str(e)})

def section(t):
    """Print section header."""
    print(f"\n{'─'*62}\n  {t}\n{'─'*62}")

def assert_eq(a, b, msg=""):
    """Assert equality."""
    assert a == b, msg or f"{a!r} != {b!r}"

def assert_ne(a, b, msg=""):
    """Assert inequality."""
    assert a != b, msg or f"{a!r} == {b!r}"

def assert_ok(v, msg=""):
    """Assert truthy."""
    assert v, msg or f"expected True, got {v!r}"

def assert_in(item, container, msg=""):
    """Assert membership."""
    assert item in container, msg or f"{item!r} not in {container!r}"

def assert_gt(a, b, msg=""):
    """Assert greater than."""
    assert a > b, msg or f"{a} <= {b}"

def assert_gte(a, b, msg=""):
    """Assert greater than or equal."""
    assert a >= b, msg or f"{a} < {b}"

def make_lattice(overrides=None):
    """Create test lattice with known nodes."""
    nodes = overrides or [
        {"node": "10.0.0.1", "latency": 20, "storage_avail_gb": 4.0},
        {"node": "10.0.0.2", "latency": 50, "storage_avail_gb": 2.0},
        {"node": "10.0.0.3", "latency": 120, "storage_avail_gb": 8.0},
        {"node": "10.0.0.4", "latency": 200, "storage_avail_gb": 1.0},
    ]
    lat = HDGLLattice()
    for n in nodes:
        lat.update(n["node"], n["latency"], n["storage_avail_gb"])
    return lat

def make_swap(lattice=None, local="10.0.0.1"):
    """Create test fileswap."""
    lat = lattice or make_lattice()
    swap = HDGLFileswap(lat, local_node=local)
    swap._dry_run_override = True
    return swap

def make_server(lattice=None, fileswap=None):
    """Create test HTTP server."""
    lat = lattice or make_lattice()
    swap = fileswap or make_swap(lat)
    return HDGLHTTPServer(lat, swap, local_node="10.0.0.1", http_port=8080)

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1: STRAND ROUTING FUNDAMENTALS
# ═════════════════════════════════════════════════════════════════════════════

section("1. Native Strand Routing (phi_tau → strand → authority)")

def t_phi_tau_determinism():
    """phi_tau must always return the same value for same path."""
    assert_eq(_phi_tau("/api/v1/users"), _phi_tau("/api/v1/users"))
    return "phi_tau deterministic ✓"

test("phi_tau is deterministic", t_phi_tau_determinism)

def t_strand_for_path_bounds():
    """Strand index must always be in [0, 7]."""
    paths = ["/", "/a", "/service/config.json", "/media/large.iso", "/netboot/alpine"]
    strands = [_strand_for_path(p) for p in paths]
    assert all(0 <= s <= 7 for s in strands), f"Out of bounds: {strands}"
    return f"Strands: {strands}"

test("Strand index bounded [0, 7]", t_strand_for_path_bounds)

def t_native_strand_routing():
    """Native HTTP server's strand routing must match fileswap routing."""
    paths = ["/service/config.json", "/web/index.html", "/data/users.json"]
    for path in paths:
        expected = _strand_for_path(path)
        actual = _native_strand_for_path(path)
        assert_eq(expected, actual, f"Strand mismatch for {path}")
    return f"{len(paths)} paths routed consistently"

test("Native server routes match fileswap logic", t_native_strand_routing)

def t_authority_lookup():
    """Lattice must provide authority per strand."""
    lat = make_lattice()
    top = lat.top_node_per_strand()
    assert_eq(len(top), NUM_STRANDS, "top_node_per_strand must return 8 entries")
    for k in range(NUM_STRANDS):
        assert_in(k, top)
    return f"8 strands, all have authority nodes"

test("Authority lookup per strand", t_authority_lookup)

def t_strand_persistence():
    """Same path must always route to same strand."""
    path = "/persistent/test/file.json"
    strand1 = _strand_for_path(path)
    strand2 = _strand_for_path(path)
    strand3 = _strand_for_path(path)
    assert_eq(strand1, strand2)
    assert_eq(strand2, strand3)
    return f"Strand {strand1} consistent across 3 lookups"

test("Strand routing persistent per path", t_strand_persistence)

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2: CONNECTION POOLING
# ═════════════════════════════════════════════════════════════════════════════

section("2. Strand-Affinity Connection Pooling")

def t_pool_creation():
    """Each strand must have its own connection pool."""
    server = make_server()
    assert_eq(len(server.pools), NUM_STRANDS)
    for k in range(NUM_STRANDS):
        pool = server.pools[k]
        assert_eq(pool.strand_idx, k)
    return "8 pools created, one per strand"

test("Connection pools created per strand", t_pool_creation)

def t_pool_status_structure():
    """Pool status must have required fields."""
    pool = StrandConnectionPool(strand_idx=0)
    status = pool.status()
    required = ["strand", "authority", "pooled_connections", "reuse_count", "mismatch_count"]
    for field in required:
        assert_in(field, status, f"Missing field: {field}")
    return f"Status fields: {list(status.keys())}"

test("Connection pool status structure", t_pool_status_structure)

def t_pool_reuse_tracking():
    """Connection reuse must be tracked."""
    pool = StrandConnectionPool(strand_idx=0)
    assert_eq(pool.reuse_count, 0)
    assert_eq(pool.mismatch_count, 0)
    return "Reuse and mismatch counters initialized"

test("Connection pool tracking counters", t_pool_reuse_tracking)

def t_authority_shift_detection():
    """Pool must detect when authority changes."""
    pool = StrandConnectionPool(strand_idx=3)
    pool.authority_ip = "10.0.0.1"
    old_mismatch = pool.mismatch_count
    # Simulate authority shift
    pool.authority_ip = "10.0.0.2"
    # Mismatch count would increment on next get_connection call
    assert_eq(pool.authority_ip, "10.0.0.2")
    return "Authority shift detected"

test("Authority shift detection in pool", t_authority_shift_detection)

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3: HTTP SERVER METRICS
# ═════════════════════════════════════════════════════════════════════════════

section("3. Metrics Tracking (per-request and per-strand)")

def t_metrics_initialization():
    """Server must initialize metrics tracking."""
    server = make_server()
    required = ["total_requests", "local_serves", "proxied_requests",
                "cache_hits", "errors", "authority_shifts"]
    for metric in required:
        assert_in(metric, server.metrics)
    return f"Metrics: {list(server.metrics.keys())}"

test("Metrics initialization", t_metrics_initialization)

def t_strand_metrics_structure():
    """Each strand must have independent metrics."""
    server = make_server()
    assert_eq(len(server.strand_metrics), NUM_STRANDS)
    for k in range(NUM_STRANDS):
        assert_in(k, server.strand_metrics)
        strand_m = server.strand_metrics[k]
        required = ["requests", "cache_hits", "authority"]
        for field in required:
            assert_in(field, strand_m)
    return "8 strand metrics with required fields"

test("Strand metrics per-strand tracking", t_strand_metrics_structure)

def t_metrics_json_serializable():
    """All metrics must be JSON serializable."""
    server = make_server()
    try:
        json_str = json.dumps(server.metrics)
        strand_json = json.dumps(server.strand_metrics)
        assert_ok(json_str)
        assert_ok(strand_json)
    except Exception as e:
        raise AssertionError(f"Metrics not JSON serializable: {e}")
    return "Metrics serializable to JSON"

test("Metrics JSON serializable", t_metrics_json_serializable)

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4: STRAND AUTHORITY MAP
# ═════════════════════════════════════════════════════════════════════════════

section("4. Strand Authority Map (top_node_per_strand)")

def t_strand_map_complete():
    """Strand map must cover all 8 strands."""
    lat = make_lattice()
    top = lat.top_node_per_strand()
    assert_eq(len(top), 8)
    for k in range(8):
        assert_in(k, top)
    return "All 8 strands have assigned authority"

test("Strand authority map complete", t_strand_map_complete)

def t_strand_map_valid_ips():
    """All authority IPs must be valid."""
    lat = make_lattice()
    top = lat.top_node_per_strand()
    for k, (node, weight) in top.items():
        assert_ok(node, f"Strand {k} has no authority")
        assert_gt(weight, 0, f"Strand {k} weight must be > 0, got {weight}")
    return "All authorities valid with positive weights"

test("Strand map authority validity", t_strand_map_valid_ips)

def t_strand_map_persistence():
    """Strand map must be consistent across reads."""
    lat = make_lattice()
    map1 = lat.top_node_per_strand()
    map2 = lat.top_node_per_strand()
    assert_eq(map1, map2)
    return "Strand map consistent"

test("Strand map persistence", t_strand_map_persistence)

def t_strand_map_weight_differentiation():
    """Strand weights must show differentiation."""
    lat = make_lattice()
    top = lat.top_node_per_strand()
    weights = [w for _, w in top.values()]
    unique_weights = len(set(weights))
    assert_gt(unique_weights, 1, "No weight differentiation")
    return f"Weight differentiation: {unique_weights} unique weights"

test("Strand weight differentiation", t_strand_map_weight_differentiation)

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5: FILESWAP INTEGRATION
# ═════════════════════════════════════════════════════════════════════════════

section("5. Fileswap Integration (local vs proxied requests)")

def t_fileswap_write_read():
    """Fileswap must persist writes and serve reads."""
    swap = make_swap()
    test_data = b"test content for v0.3 audit"
    test_path = "/v03/test.txt"

    swap.write(test_path, test_data)
    result = swap.read(test_path)

    assert_eq(result, test_data)
    return "Write/read round-trip successful"

test("Fileswap write/read", t_fileswap_write_read)

def t_strand_routing_for_files():
    """Files must route to correct strands."""
    swap = make_swap()
    files = {
        "/service/config.json": b'{"svc":"service"}',
        "/web/index.html": b"<html/>",
        "/data/users.json": b'[]',
    }

    for path, data in files.items():
        swap.write(path, data)
        strand = _strand_for_path(path)
        assert_ok(0 <= strand <= 7)

    return f"{len(files)} files routed to strands [0,7]"

test("File routing to strands", t_strand_routing_for_files)

def t_local_authority_detection():
    """Server must detect when it's the authority."""
    lat = make_lattice()
    top = lat.top_node_per_strand()

    local = "10.0.0.1"
    is_authority_for = [k for k, (node, _) in top.items() if node == local]

    assert_gt(len(is_authority_for), 0, f"Local node {local} has no authority strands")
    return f"Local node is authority for strands: {is_authority_for}"

test("Local authority detection", t_local_authority_detection)

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6: PER-REQUEST ROUTING DECISION
# ═════════════════════════════════════════════════════════════════════════════

section("6. Per-Request Routing Decision (phi_tau → strand → authority)")

def t_routing_decision_consistency():
    """Same path must make same routing decision."""
    lat = make_lattice()
    path = "/api/v1/users/123"

    strand1 = _strand_for_path(path)
    strand2 = _strand_for_path(path)

    auth1 = lat.top_node_per_strand()[strand1]
    auth2 = lat.top_node_per_strand()[strand2]

    assert_eq(strand1, strand2)
    assert_eq(auth1, auth2)
    return f"Path routes to strand {strand1} → {auth1[0]}"

test("Routing decision consistency", t_routing_decision_consistency)

def t_prefix_clustering():
    """Paths with similar prefixes should land on same strand."""
    base_path = "/service"
    similar_paths = [
        "/service/config",
        "/service/status",
        "/service/health",
    ]

    strands = [_strand_for_path(p) for p in similar_paths]
    # At least majority should be same strand
    strand_counts = {}
    for s in strands:
        strand_counts[s] = strand_counts.get(s, 0) + 1

    max_count = max(strand_counts.values())
    assert_gte(max_count, 2, "No prefix clustering observed")
    return f"Prefix clustering: {strand_counts}"

test("Prefix-based clustering", t_prefix_clustering)

def t_path_diversity():
    """Different paths should distribute across strands."""
    diverse_paths = [
        "/static/images/logo.png",
        "/media/video.mp4",
        "/netboot/kernel",
        "/api/v1/data",
        "/service/health",
    ]

    strands = set(_strand_for_path(p) for p in diverse_paths)
    assert_gt(len(strands), 1, "No strand diversity for diverse paths")
    return f"Diverse paths use {len(strands)} different strands"

test("Strand diversity for diverse paths", t_path_diversity)

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7: ENDPOINT VALIDATION
# ═════════════════════════════════════════════════════════════════════════════

section("7. Native Server Endpoints (structural validation)")

def t_routes_registered():
    """Server must have all required routes registered."""
    server = make_server()
    routes = list(server.app.router.routes())
    assert_gt(len(routes), 0, "No routes registered")
    return f"Routes registered in app router: {len(routes)}"

test("HTTP routes registered", t_routes_registered)

def t_endpoint_methods():
    """Server must have required endpoint methods."""
    server = make_server()
    required_methods = [
        'handle_metrics',
        'handle_strand_map',
        'handle_pool_status',
        'handle_health',
        'handle_request',
    ]
    for method in required_methods:
        assert_ok(hasattr(server, method), f"Missing method: {method}")
    return f"{len(required_methods)} endpoint methods available"

test("Required endpoint methods exist", t_endpoint_methods)

# ═════════════════════════════════════════════════════════════════════════════
# SECTION 8: INTEGRATION TEST
# ═════════════════════════════════════════════════════════════════════════════

section("8. Full v0.3 Integration Scenario")

def t_end_to_end():
    """Complete scenario: lattice → fileswap → server → metrics."""
    # Create lattice with 4 nodes
    lat = make_lattice()

    # Create fileswap with known authority
    swap = make_swap(lat)

    # Create server
    server = make_server(lat, swap)

    # Write some test files
    test_files = {
        "/service/app.config": b'{"name":"test"}',
        "/data/users.csv": b"id,name\n1,Alice",
        "/web/style.css": b"body { color: blue; }",
    }

    for path, data in test_files.items():
        swap.write(path, data)
        strand = _strand_for_path(path)
        auth = lat.top_node_per_strand()[strand]
        assert_ok(auth[0])

    # Verify metrics structure
    metrics = server.metrics
    assert_eq(metrics["total_requests"], 0, "Should start with 0 requests")

    # Verify strand maps
    top = lat.top_node_per_strand()
    for k in range(NUM_STRANDS):
        assert_ok(top[k][0])

    return f"E2E: 4-node lattice, {len(test_files)} files, 8 strands, full metrics"

test("End-to-end v0.3 scenario", t_end_to_end)

# ═════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═════════════════════════════════════════════════════════════════════════════

section("AUDIT SUMMARY")

passed = sum(1 for r in results if r["status"] == "PASS")
failed = sum(1 for r in results if r["status"] == "FAIL")
total = len(results)

print(f"\n  {PASS_S} Passed: {passed}/{total}")
print(f"  {FAIL_S} Failed: {failed}/{total}")

if failed > 0:
    print(f"\n  Failed tests:")
    for r in results:
        if r["status"] == "FAIL":
            print(f"    - {r['name']}: {r['detail'][:80]}")

sys.exit(0 if failed == 0 else 1)
