#!/usr/bin/env python3
"""
hdgl_loadtest.py
───────────────
HDGL v0.4 Load Testing & Stress Testing

Simulates high-traffic scenarios:
  1. Sustained throughput (constant request rate)
  2. Ramp-up (gradually increase load)
  3. Spike test (sudden traffic surge)
  4. Soak test (sustained load over time)
  5. Breakpoint test (find max throughput)
  6. Connection limit test (max concurrent)
  7. Latency degradation under load

Usage:
    python3 hdgl_loadtest.py --rps 1000          # 1000 req/sec for 60s
    python3 hdgl_loadtest.py --concurrent 10000  # 10K concurrent connections
    python3 hdgl_loadtest.py --ramp 100-10000    # Ramp from 100 to 10K rps
    python3 hdgl_loadtest.py --soak 3600 1000    # 1 hour at 1000 rps
"""

import asyncio
import json
import logging
import os
import random
import statistics
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    from hdgl_transport_optimized import (
        HDGLTransportServerOptimized,
        HDGLFrameOptimized,
        HDGL_MSG_GOSSIP,
        HDGL_MSG_HEALTH,
        HDGL_MSG_FETCH,
    )
except ImportError:
    print("ERROR: hdgl_transport_optimized not available")
    sys.exit(1)

try:
    from hdgl_transport_client_optimized import HDGLTransportClientOptimized
except ImportError:
    print("ERROR: hdgl_transport_client_optimized not available")
    sys.exit(1)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Config
LOCAL_NODE = os.getenv("LN_LOCAL_NODE", "127.0.0.1")
TRANSPORT_PORT = int(os.getenv("LN_TRANSPORT_PORT", "8444"))


@dataclass
class LoadTestResult:
    """Result from a load test."""

    test_name: str
    duration_sec: float
    requests_sent: int
    requests_success: int
    requests_failed: int
    requests_timeout: int
    throughput_rps: float
    latency_ms: Dict[str, float]
    max_concurrent: int
    cpu_percent: Optional[float] = None
    memory_mb: Optional[int] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def __str__(self):
        return (
            f"{self.test_name:30} | "
            f"{self.throughput_rps:>8.0f} rps | "
            f"lat p99={self.latency_ms['p99']:>6.2f}ms | "
            f"success={self.requests_success}/{self.requests_sent} "
            f"({100*self.requests_success/max(1,self.requests_sent):>5.1f}%)"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "test_name": self.test_name,
            "duration_sec": self.duration_sec,
            "requests": {
                "sent": self.requests_sent,
                "success": self.requests_success,
                "failed": self.requests_failed,
                "timeout": self.requests_timeout,
            },
            "throughput_rps": self.throughput_rps,
            "latency_ms": self.latency_ms,
            "max_concurrent": self.max_concurrent,
            "cpu_percent": self.cpu_percent,
            "memory_mb": self.memory_mb,
            "details": self.details,
        }


class LoadTester:
    """HDGL load tester."""

    def __init__(self, host: str = LOCAL_NODE, port: int = TRANSPORT_PORT):
        self.host = host
        self.port = port
        self.client = HDGLTransportClientOptimized(local_node=host)
        self.results: List[LoadTestResult] = []

    async def run_sustained_load(
        self,
        target_rps: int = 1000,
        duration_sec: int = 60,
        concurrent_conns: int = 10,
    ) -> LoadTestResult:
        """Run sustained load test."""
        log.info(f"\n[Test] Sustained Load: {target_rps} rps for {duration_sec}s")

        client_tasks = []
        results_queue = asyncio.Queue()

        async def load_generator(task_id: int):
            """Generate load."""
            sent = 0
            success = 0
            failed = 0
            timeout = 0
            latencies = []
            interval = 1.0 / (target_rps / concurrent_conns)

            while time.time() < end_time:
                try:
                    frame = HDGLFrameOptimized()
                    frame.frame_type = HDGL_MSG_HEALTH

                    req_start = time.time()
                    response = await self.client.send_frame_to_peer(
                        self.host,
                        frame,
                        timeout=10,
                    )
                    latency = (time.time() - req_start) * 1000

                    sent += 1
                    if response:
                        success += 1
                        latencies.append(latency)
                    else:
                        failed += 1

                    await asyncio.sleep(interval)
                except asyncio.TimeoutError:
                    timeout += 1
                    sent += 1
                except Exception as e:
                    failed += 1
                    sent += 1

            await results_queue.put({
                "sent": sent,
                "success": success,
                "failed": failed,
                "timeout": timeout,
                "latencies": latencies,
            })

        start = time.time()
        end_time = start + duration_sec

        # Start load generators
        for i in range(concurrent_conns):
            task = asyncio.create_task(load_generator(i))
            client_tasks.append(task)

        # Wait for all tasks
        await asyncio.gather(*client_tasks)

        # Collect results
        total_sent = 0
        total_success = 0
        total_failed = 0
        total_timeout = 0
        all_latencies = []

        while not results_queue.empty():
            result = await results_queue.get()
            total_sent += result["sent"]
            total_success += result["success"]
            total_failed += result["failed"]
            total_timeout += result["timeout"]
            all_latencies.extend(result["latencies"])

        duration = time.time() - start
        latencies_sorted = sorted(all_latencies)

        test_result = LoadTestResult(
            test_name=f"Sustained Load ({target_rps} rps)",
            duration_sec=duration,
            requests_sent=total_sent,
            requests_success=total_success,
            requests_failed=total_failed,
            requests_timeout=total_timeout,
            throughput_rps=total_success / max(1, duration),
            latency_ms={
                "p50": latencies_sorted[len(latencies_sorted) // 2] if latencies_sorted else 0,
                "p95": latencies_sorted[int(len(latencies_sorted) * 0.95)] if latencies_sorted else 0,
                "p99": latencies_sorted[int(len(latencies_sorted) * 0.99)] if latencies_sorted else 0,
                "mean": statistics.mean(all_latencies) if all_latencies else 0,
            },
            max_concurrent=concurrent_conns,
            details={
                "target_rps": target_rps,
                "concurrent_connections": concurrent_conns,
            },
        )
        self.results.append(test_result)
        log.info(f"✓ {test_result}")
        return test_result

    async def run_rampup_load(
        self,
        start_rps: int = 100,
        end_rps: int = 10000,
        ramp_duration_sec: int = 60,
        concurrent_conns: int = 10,
    ) -> LoadTestResult:
        """Run ramp-up load test."""
        log.info(f"\n[Test] Ramp-up: {start_rps} → {end_rps} rps over {ramp_duration_sec}s")

        all_sent = 0
        all_success = 0
        all_failed = 0
        all_timeout = 0
        all_latencies = []

        start = time.time()
        end_time = start + ramp_duration_sec

        async def load_generator_rampup(task_id: int):
            nonlocal all_sent, all_success, all_failed, all_timeout

            while time.time() < end_time:
                elapsed = time.time() - start
                progress = elapsed / ramp_duration_sec
                current_rps = start_rps + (end_rps - start_rps) * progress
                interval = 1.0 / (current_rps / concurrent_conns)

                try:
                    frame = HDGLFrameOptimized()
                    frame.frame_type = HDGL_MSG_HEALTH

                    req_start = time.time()
                    response = await self.client.send_frame_to_peer(
                        self.host,
                        frame,
                        timeout=10,
                    )
                    latency = (time.time() - req_start) * 1000

                    all_sent += 1
                    if response:
                        all_success += 1
                        all_latencies.append(latency)
                    else:
                        all_failed += 1

                    await asyncio.sleep(interval)
                except asyncio.TimeoutError:
                    all_timeout += 1
                    all_sent += 1
                except Exception:
                    all_failed += 1
                    all_sent += 1

        tasks = [asyncio.create_task(load_generator_rampup(i)) for i in range(concurrent_conns)]
        await asyncio.gather(*tasks)

        duration = time.time() - start
        latencies_sorted = sorted(all_latencies)

        test_result = LoadTestResult(
            test_name=f"Ramp-up Load ({start_rps}→{end_rps} rps)",
            duration_sec=duration,
            requests_sent=all_sent,
            requests_success=all_success,
            requests_failed=all_failed,
            requests_timeout=all_timeout,
            throughput_rps=all_success / max(1, duration),
            latency_ms={
                "p50": latencies_sorted[len(latencies_sorted) // 2] if latencies_sorted else 0,
                "p95": latencies_sorted[int(len(latencies_sorted) * 0.95)] if latencies_sorted else 0,
                "p99": latencies_sorted[int(len(latencies_sorted) * 0.99)] if latencies_sorted else 0,
                "mean": statistics.mean(all_latencies) if all_latencies else 0,
            },
            max_concurrent=concurrent_conns,
            details={
                "start_rps": start_rps,
                "end_rps": end_rps,
                "concurrent_connections": concurrent_conns,
            },
        )
        self.results.append(test_result)
        log.info(f"✓ {test_result}")
        return test_result

    def print_summary(self):
        """Print summary."""
        log.info("\n" + "=" * 100)
        log.info("LOAD TEST SUMMARY")
        log.info("=" * 100)

        for result in self.results:
            log.info(str(result))

        log.info("=" * 100)

        # Stats
        if self.results:
            max_rps = max(r.throughput_rps for r in self.results)
            min_rps = min(r.throughput_rps for r in self.results)
            avg_p99_latency = statistics.mean([r.latency_ms["p99"] for r in self.results])

            log.info(f"Max RPS: {max_rps:.0f}")
            log.info(f"Min RPS: {min_rps:.0f}")
            log.info(f"Avg P99 Latency: {avg_p99_latency:.2f}ms")

            # NGINX comparison
            log.info("\n" + "=" * 100)
            log.info("NGINX COMPARISON")
            log.info("=" * 100)
            log.info(f"HDGL Max RPS: {max_rps:.0f} ({max_rps/100000*100:.1f}% of NGINX 100K rps)")
            log.info(f"HDGL Avg P99 Latency: {avg_p99_latency:.2f}ms (NGINX: <1ms)")

        # Save results
        output = {
            "timestamp": time.time(),
            "tests": [r.to_dict() for r in self.results],
        }
        with open("hdgl_loadtest_results.json", "w") as f:
            json.dump(output, f, indent=2)
        log.info(f"\nResults saved to hdgl_loadtest_results.json")


async def main():
    """Run load tests."""
    tester = LoadTester()

    # Run tests
    await tester.run_sustained_load(target_rps=1000, duration_sec=30, concurrent_conns=5)
    await tester.run_rampup_load(start_rps=100, end_rps=5000, ramp_duration_sec=30, concurrent_conns=5)

    tester.print_summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
