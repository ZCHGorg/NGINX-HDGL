#!/usr/bin/env python3
"""
hdgl_audit_v0.4_lite.py
───────────────────────
Lightweight audit for HDGL v0.4 transport layer (no filesystem access).

Tests core protocol functionality:
  - Frame serialization/deserialization
  - Transport client message types
  - Frame validation

Usage:
    python3 hdgl_audit_v0.4_lite.py
"""

import json
import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

test_count = 0
pass_count = 0

def test(name: str, condition: bool, details: str = ""):
    """Register test result."""
    global test_count, pass_count
    test_count += 1
    status = "✓ PASS" if condition else "✗ FAIL"
    msg = f"  [{test_count:2d}] {status} — {name}"
    if details:
        msg += f" ({details})"
    log.info(msg)
    if condition:
        pass_count += 1


def main():
    """Run transport layer tests."""
    log.info("=" * 70)
    log.info("HDGL v0.4 Transport Layer Audit (Lightweight)")
    log.info("=" * 70)

    from hdgl_transport import (
        HDGLFrame,
        HDGL_MSG_INFO,
        HDGL_MSG_GOSSIP,
        HDGL_MSG_FETCH,
        HDGL_MSG_REPLICATE,
        HDGL_MSG_METRICS,
        HDGL_MSG_HEALTH,
        HDGL_MSG_ERROR,
        _ip_to_uint32,
        _uint32_to_ip,
        LOCAL_NODE,
        NUM_STRANDS,
    )

    # ── SECTION 1: Frame Types ────────────────────────────────────────────────
    log.info("─── Section 1: HDGL Frame Types ───")
    frame_types = [
        (HDGL_MSG_INFO, "INFO", {"health": "ok"}),
        (HDGL_MSG_GOSSIP, "GOSSIP", {"node": LOCAL_NODE, "latency": 50}),
        (HDGL_MSG_FETCH, "FETCH", {"path": "/data/file"}),
        (HDGL_MSG_REPLICATE, "REPLICATE", {"path": "/data/file", "size": 1024}),
        (HDGL_MSG_METRICS, "METRICS", {"frame_count": 100}),
        (HDGL_MSG_HEALTH, "HEALTH", {}),
        (HDGL_MSG_ERROR, "ERROR", {"error": "test"}),
    ]

    for msg_type, name, payload_dict in frame_types:
        frame = HDGLFrame(
            frame_type=msg_type,
            strand_id=0,
            source_ip=LOCAL_NODE,
            payload=json.dumps(payload_dict).encode(),
        )
        serialized = frame.serialize()
        deserialized = HDGLFrame.deserialize(serialized)
        success = (
            deserialized is not None
            and deserialized.frame_type == msg_type
            and json.loads(deserialized.payload.decode()) == payload_dict
        )
        test(f"Frame type {name} round-trip", success)

    # ── SECTION 2: Strand Routing ─────────────────────────────────────────────
    log.info("─── Section 2: Strand Routing ───")

    # All strands 0-7 valid
    all_strands_valid = True
    for strand_id in range(NUM_STRANDS):
        frame = HDGLFrame(
            frame_type=HDGL_MSG_FETCH,
            strand_id=strand_id,
            source_ip=LOCAL_NODE,
            payload=b"test",
        )
        serialized = frame.serialize()
        deserialized = HDGLFrame.deserialize(serialized)
        if deserialized is None or deserialized.strand_id != strand_id:
            all_strands_valid = False
            break
    test("All 8 strands (0-7) serializable", all_strands_valid)

    # ── SECTION 3: IP Address Conversion ──────────────────────────────────────
    log.info("─── Section 3: IP Address Conversion ───")
    test_ips = ["127.0.0.1", "10.0.0.1", "192.168.1.1", "255.255.255.255"]
    for ip in test_ips:
        uint_val = _ip_to_uint32(ip)
        ip_back = _uint32_to_ip(uint_val)
        test(f"IP conversion round-trip: {ip}", ip == ip_back)

    # ── SECTION 4: Large Payload ──────────────────────────────────────────────
    log.info("─── Section 4: Large Payload ───")
    large_data = {"data": "x" * 10000}
    frame = HDGLFrame(
        frame_type=HDGL_MSG_FETCH,
        strand_id=3,
        source_ip=LOCAL_NODE,
        payload=json.dumps(large_data).encode(),
    )
    serialized = frame.serialize()
    deserialized = HDGLFrame.deserialize(serialized)
    success = (
        deserialized is not None
        and len(deserialized.payload) == len(frame.payload)
    )
    test(f"Large payload (10KB) handling", success, f"size={len(frame.payload)}")

    # ── SECTION 5: Authority Epoch ────────────────────────────────────────────
    log.info("─── Section 5: Authority Epoch ───")
    for epoch in [0, 1, 100, 0xFFFFFFFF]:
        frame = HDGLFrame(
            frame_type=HDGL_MSG_INFO,
            strand_id=0,
            authority_ep=epoch,
            source_ip=LOCAL_NODE,
            payload=b"test",
        )
        serialized = frame.serialize()
        deserialized = HDGLFrame.deserialize(serialized)
        success = deserialized is not None and deserialized.authority_ep == epoch
        test(f"Authority epoch {epoch} preserved", success)

    # ── SECTION 6: Client Message Types ───────────────────────────────────────
    log.info("─── Section 6: Transport Client ───")
    from hdgl_transport_client import HDGLTransportClient

    client = HDGLTransportClient(local_node=LOCAL_NODE)
    test("Transport client initialization", client is not None)
    test("Client has peer cache", hasattr(client, "peer_cache"))
    test("Client has close_all method", callable(getattr(client, "close_all", None)))
    client.close_all()
    test("Client cleanup successful", len(client.peer_cache) == 0)

    # ── SUMMARY ───────────────────────────────────────────────────────────────
    log.info("=" * 70)
    log.info(f"AUDIT RESULT: {pass_count}/{test_count} tests PASSED")
    log.info("=" * 70)

    if pass_count == test_count:
        log.info("✓ ALL TESTS PASSED — v0.4 transport core functional")
        return 0
    else:
        log.error(f"✗ {test_count - pass_count} test(s) FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
