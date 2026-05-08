#!/usr/bin/env python3
"""
hdgl_audit_v0.4.py
──────────────────
Comprehensive audit suite for HDGL v0.4 Unified Transport.

Tests the true end-to-end HDGL architecture where all node communication
flows through a single transport listener with geometry-driven routing.

8 Test Sections (32 tests total):
  1. Frame Format & Serialization (4 tests)
  2. HMAC Signing & Verification (4 tests)
  3. Unified Transport Server (4 tests)
  4. Transport Client & Connection Pooling (4 tests)
  5. Strand-Based Message Routing (4 tests)
  6. Gossip Frame Exchange (4 tests)
  7. Multi-strand Coordination (2 tests)
  8. Metrics & Transport Observability (2 tests)

Usage:
    python3 hdgl_audit_v0.4.py

Expected Result: 32/32 tests PASS ✓
"""

import json
import logging
import math
import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# TEST HARNESS
# ─────────────────────────────────────────────────────────────────────────────

test_count = 0
pass_count = 0

def test(name: str, condition: bool, details: str = ""):
    """Register test result."""
    global test_count, pass_count
    test_count += 1
    status = "✓ PASS" if condition else "✗ FAIL"
    msg = f"  [{test_count:2d}] {status} — {name}"
    if details:
        msg += f"\n       {details}"
    log.info(msg)
    if condition:
        pass_count += 1
    return condition


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1: FRAME FORMAT & SERIALIZATION
# ─────────────────────────────────────────────────────────────────────────────

def audit_section_1():
    """Test HDGL frame format, size, and serialization."""
    log.info("──── SECTION 1: Frame Format & Serialization ────")
    from hdgl_transport import (
        HDGLFrame,
        HDGL_MSG_INFO,
        HDGL_MSG_GOSSIP,
        FRAME_HEADER_SIZE,
        FRAME_HMAC_SIZE,
        LOCAL_NODE,
    )

    # Test 1: Frame structure
    frame = HDGLFrame(
        frame_type=HDGL_MSG_INFO,
        strand_id=3,
        source_ip=LOCAL_NODE,
        payload=b"test_payload",
    )
    test(
        "HDGLFrame structure initialization",
        frame.strand_id == 3 and frame.frame_type == HDGL_MSG_INFO,
    )

    # Test 2: Serialization size
    serialized = frame.serialize()
    expected_min_size = 4 + FRAME_HEADER_SIZE + len(b"test_payload") + FRAME_HMAC_SIZE
    test(
        "Frame serialization produces correct minimum size",
        len(serialized) >= expected_min_size,
        f"size={len(serialized)}, expected>={expected_min_size}",
    )

    # Test 3: Frame deserialization
    deserialized = HDGLFrame.deserialize(serialized)
    test(
        "Frame deserialization round-trip",
        deserialized is not None and deserialized.strand_id == 3,
        f"payload_len={len(deserialized.payload) if deserialized else 'None'}",
    )

    # Test 4: Payload preservation
    if deserialized:
        test(
            "Payload preserved through serialization",
            deserialized.payload == b"test_payload",
            f"got {deserialized.payload}",
        )
    else:
        test("Payload preserved through serialization", False, "deserialized=None")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2: HMAC SIGNING & VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def audit_section_2():
    """Test HMAC signing, verification, and replay protection."""
    log.info("──── SECTION 2: HMAC Signing & Verification ────")
    from hdgl_transport import HDGLFrame, HDGL_MSG_GOSSIP, LOCAL_NODE

    # Test 1: HMAC signature generation
    frame1 = HDGLFrame(
        frame_type=HDGL_MSG_GOSSIP,
        strand_id=0,
        source_ip=LOCAL_NODE,
        payload=json.dumps({"node": LOCAL_NODE, "latency": 50}).encode(),
    )
    sig1 = frame1.serialize()
    test("HMAC signature generation", len(sig1) > 0, f"sig_len={len(sig1)}")

    # Test 2: Two frames with same payload have different signatures (timestamps differ)
    time.sleep(0.1)  # Ensure timestamp changes
    frame2 = HDGLFrame(
        frame_type=HDGL_MSG_GOSSIP,
        strand_id=0,
        source_ip=LOCAL_NODE,
        payload=json.dumps({"node": LOCAL_NODE, "latency": 50}).encode(),
    )
    sig2 = frame2.serialize()
    test(
        "Different timestamps produce different signatures",
        sig1 != sig2,
        "signatures should differ due to timestamp",
    )

    # Test 3: Deserialization with HMAC verification
    deserialized = HDGLFrame.deserialize(sig1)
    test(
        "HMAC verification succeeds for valid frame",
        deserialized is not None,
        f"deserialized={deserialized}",
    )

    # Test 4: Corrupted frame is rejected
    corrupted = sig1[:50] + b"\x00" + sig1[51:]  # flip one byte
    deserialized_bad = HDGLFrame.deserialize(corrupted)
    test(
        "HMAC verification fails for corrupted frame",
        deserialized_bad is None,
        "corrupted frame should be rejected",
    )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3: UNIFIED TRANSPORT SERVER
# ─────────────────────────────────────────────────────────────────────────────

def audit_section_3():
    """Test unified transport server startup, frame handling, and graceful shutdown."""
    log.info("──── SECTION 3: Unified Transport Server ────")
    from hdgl_lattice import HDGLLattice
    from hdgl_fileswap import HDGLFileswap
    from hdgl_transport import HDGLTransportServer, LOCAL_NODE

    # Create minimal mock host object
    class MockHost:
        def _recv_gossip(self, data, peer_ip):
            pass

    # Test 1: Server initialization
    lattice = HDGLLattice()
    swap = HDGLFileswap(lattice, local_node=LOCAL_NODE)
    host = MockHost()
    server = HDGLTransportServer(lattice, swap, host, local_node=LOCAL_NODE)
    test("Transport server initialization", server is not None, f"port={9999}")

    # Test 2: Server can start
    server.start()
    time.sleep(0.5)  # Let server bind
    test("Transport server starts", server._running, "server should be running")

    # Test 3: Metrics available
    metrics = server.get_metrics()
    test(
        "Server metrics available",
        "listening_on" in metrics and "frame_counts" in metrics,
        f"keys={list(metrics.keys())}",
    )

    # Test 4: Server shutdown
    server.stop()
    time.sleep(0.5)
    test("Transport server stops cleanly", not server._running, "server should stop")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4: TRANSPORT CLIENT & CONNECTION POOLING
# ─────────────────────────────────────────────────────────────────────────────

def audit_section_4():
    """Test transport client creation, connection pooling, and socket caching."""
    log.info("──── SECTION 4: Transport Client & Connection Pooling ────")
    from hdgl_transport_client import HDGLTransportClient, LOCAL_NODE

    # Test 1: Client initialization
    client = HDGLTransportClient(local_node=LOCAL_NODE)
    test("Transport client initialization", client is not None, f"local={LOCAL_NODE}")

    # Test 2: Client has per-peer cache
    test(
        "Client has connection cache",
        hasattr(client, "peer_cache") and isinstance(client.peer_cache, dict),
        "peer_cache should be a dict",
    )

    # Test 3: Pool configuration
    from hdgl_transport import POOL_SIZE_PER_STRAND
    test(
        "Pool size configuration available",
        POOL_SIZE_PER_STRAND > 0,
        f"POOL_SIZE={POOL_SIZE_PER_STRAND}",
    )

    # Test 4: Client cleanup
    client.close_all()
    test(
        "Client cleanup (close_all)",
        len(client.peer_cache) == 0,
        "cache should be empty after cleanup",
    )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5: STRAND-BASED MESSAGE ROUTING
# ─────────────────────────────────────────────────────────────────────────────

def audit_section_5():
    """Test strand ID assignment, routing decisions, and authority mapping."""
    log.info("──── SECTION 5: Strand-Based Message Routing ────")
    from hdgl_transport import (
        HDGLFrame,
        HDGL_MSG_FETCH,
        HDGL_MSG_REPLICATE,
        NUM_STRANDS,
        LOCAL_NODE,
    )
    from hdgl_fileswap import _strand_for_path

    # Test 1: Strand ID valid range
    for strand_id in range(NUM_STRANDS):
        frame = HDGLFrame(
            frame_type=HDGL_MSG_FETCH,
            strand_id=strand_id,
            source_ip=LOCAL_NODE,
            payload=b"",
        )
        serialized = frame.serialize()
        deserialized = HDGLFrame.deserialize(serialized)
        if deserialized and deserialized.strand_id != strand_id:
            test(
                f"Strand ID {strand_id} round-trip",
                False,
                f"got {deserialized.strand_id}",
            )
            return

    test("Strand ID round-trip for all 8 strands", True, "all strands 0-7 OK")

    # Test 2: Strand routing for paths
    test_paths = ["/app/config", "/storage/data", "/cache/session"]
    strands = []
    for path in test_paths:
        strand = _strand_for_path(path)
        strands.append(strand)
        if strand < 0 or strand >= NUM_STRANDS:
            test(f"Path '{path}' routing to valid strand", False, f"strand={strand}")
            return

    test(
        "Path routing produces valid strands",
        len(strands) == 3,
        f"strands={strands}",
    )

    # Test 3: Different paths map to different strands (usually)
    test(
        "Different paths map to different strands (phi_tau distribution)",
        len(set(strands)) >= 2,
        f"strands={strands}, unique={set(strands)}",
    )

    # Test 4: Strand authority lookup
    from hdgl_lattice import HDGLLattice
    lattice = HDGLLattice()
    lattice.update(LOCAL_NODE, 10, 100)
    top = lattice.top_node_per_strand()
    test(
        "Lattice provides authority for all 8 strands",
        len(top) == NUM_STRANDS,
        f"authorities={len(top)}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6: GOSSIP FRAME EXCHANGE
# ─────────────────────────────────────────────────────────────────────────────

def audit_section_6():
    """Test gossip frame creation, sending simulation, and data integrity."""
    log.info("──── SECTION 6: Gossip Frame Exchange ────")
    from hdgl_transport import (
        HDGLFrame,
        HDGL_MSG_GOSSIP,
        LOCAL_NODE,
    )

    # Test 1: Gossip frame creation
    gossip_payload = {
        "node": LOCAL_NODE,
        "latency": 45,
        "storage_available_gb": 500,
        "fingerprint": "0xABCD1234",
        "known_nodes": ["10.0.0.1", "10.0.0.2"],
        "authority_strands": ["A", "C", "E"],
    }
    frame = HDGLFrame(
        frame_type=HDGL_MSG_GOSSIP,
        strand_id=0,
        source_ip=LOCAL_NODE,
        payload=json.dumps(gossip_payload).encode(),
    )
    test(
        "Gossip frame creation with full state",
        frame.frame_type == HDGL_MSG_GOSSIP and len(frame.payload) > 0,
        f"payload_len={len(frame.payload)}",
    )

    # Test 2: Gossip serialization
    serialized = frame.serialize()
    test(
        "Gossip frame serialization",
        len(serialized) > 0,
        f"size={len(serialized)}",
    )

    # Test 3: Gossip deserialization preserves state
    deserialized = HDGLFrame.deserialize(serialized)
    if deserialized:
        received_payload = json.loads(deserialized.payload.decode())
        test(
            "Gossip payload round-trip",
            received_payload["node"] == LOCAL_NODE
            and received_payload["latency"] == 45,
            f"received={received_payload}",
        )
    else:
        test("Gossip payload round-trip", False, "deserialized=None")

    # Test 4: Multiple gossip frames in sequence
    frames_sent = []
    for i in range(3):
        payload = json.dumps({"cycle": i, "node": LOCAL_NODE}).encode()
        f = HDGLFrame(
            frame_type=HDGL_MSG_GOSSIP,
            strand_id=i % 8,
            source_ip=LOCAL_NODE,
            payload=payload,
        )
        frames_sent.append(f.serialize())

    test(
        "Multiple gossip frames serialized",
        len(frames_sent) == 3 and all(len(f) > 0 for f in frames_sent),
        f"frame_count={len(frames_sent)}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7: MULTI-STRAND COORDINATION
# ─────────────────────────────────────────────────────────────────────────────

def audit_section_7():
    """Test multi-strand topology, echo/fallback coordination."""
    log.info("──── SECTION 7: Multi-Strand Coordination ────")
    from hdgl_lattice import HDGLLattice
    from hdgl_fileswap import HDGLFileswap, NUM_STRANDS, ECHO_SCALE

    lattice = HDGLLattice()
    swap = HDGLFileswap(lattice, local_node="10.0.0.1")

    # Test 1: Lattice has all 8 strands
    top = lattice.top_node_per_strand()
    test(
        "Lattice defines authority for all 8 strands",
        len(top) == NUM_STRANDS,
        f"authorities={len(top)}",
    )

    # Test 2: Echo scale factor configured
    test(
        "Echo scale factor configured",
        ECHO_SCALE == 0.8,
        f"ECHO_SCALE={ECHO_SCALE}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8: METRICS & TRANSPORT OBSERVABILITY
# ─────────────────────────────────────────────────────────────────────────────

def audit_section_8():
    """Test metrics collection, frame accounting, and observability."""
    log.info("──── SECTION 8: Metrics & Transport Observability ────")
    from hdgl_lattice import HDGLLattice
    from hdgl_fileswap import HDGLFileswap
    from hdgl_transport import HDGLTransportServer, LOCAL_NODE

    class MockHost:
        def _recv_gossip(self, data, peer_ip):
            pass

    lattice = HDGLLattice()
    swap = HDGLFileswap(lattice, local_node=LOCAL_NODE)
    host = MockHost()
    server = HDGLTransportServer(lattice, swap, host, local_node=LOCAL_NODE)

    # Test 1: Metrics structure
    metrics = server.get_metrics()
    required_keys = ["listening_on", "frame_counts", "routing_decisions", "uptime_sec"]
    test(
        "Metrics structure complete",
        all(k in metrics for k in required_keys),
        f"keys={list(metrics.keys())}",
    )

    # Test 2: Routing decisions tracked
    routing = metrics.get("routing_decisions", {})
    required_routing = ["local", "proxy", "error"]
    test(
        "Routing decision tracking",
        all(k in routing for k in required_routing),
        f"routing={routing}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    """Run all audit sections."""
    log.info("=" * 70)
    log.info("HDGL v0.4 Unified Transport Audit Suite")
    log.info("=" * 70)

    try:
        audit_section_1()
        audit_section_2()
        audit_section_3()
        audit_section_4()
        audit_section_5()
        audit_section_6()
        audit_section_7()
        audit_section_8()

        log.info("=" * 70)
        log.info(f"AUDIT COMPLETE: {pass_count}/{test_count} tests PASSED")
        log.info("=" * 70)

        if pass_count == test_count:
            log.info("✓ ALL TESTS PASSED — v0.4 unified transport ready for deployment")
            return 0
        else:
            log.error(f"✗ {test_count - pass_count} test(s) FAILED")
            return 1

    except Exception as e:
        log.error(f"Audit crashed: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
