#!/usr/bin/env python3
"""Stress tests for ground truth providers.

These tests are designed to test performance and resource usage under high load.
Run with: pytest tests/test_stress.py -v -m stress
"""

import asyncio
import logging
import pytest
import time
from datetime import datetime, timezone
from ground_truth import MQTTGroundTruthProvider, Claim, ValidationResult

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(name)s: %(message)s",
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# STRESS TESTS - Mark with @pytest.mark.stress for optional run
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.stress
@pytest.mark.asyncio
async def test_1000_claims_validation():
    """Test validating 1000 claims against MQTT provider."""
    log.info("Stress test: validating 1000 claims...")
    
    provider = MQTTGroundTruthProvider(
        broker_host="mock-broker",
        broker_port=1883,
        cache_ttl_seconds=30,
    )
    
    # Populate with test data
    now = datetime.now(timezone.utc)
    provider._sensor_cache["stress-device"] = {
        "GPS.POSITION": {
            "data": {
                "gps_fix": True,
                "valid_fix": True,
                "latitude": 52.5,
                "longitude": 13.4,
                "age_ms": 100,
            },
            "timestamp": now,
            "freshness_ms": 100,
        },
        "BATTERY.LEVEL": {
            "data": {
                "battery_pct": 75,
                "age_ms": 200,
            },
            "timestamp": now,
            "freshness_ms": 200,
        },
    }
    provider._device_presence["stress-device"] = {
        "present": True,
        "last_heartbeat": now,
    }
    
    # Create 1000 claims
    claims = []
    for i in range(1000):
        claim_type = "GPS" if i % 2 == 0 else "BATTERY"
        subject = "GPS.POSITION" if claim_type == "GPS" else "BATTERY.LEVEL"
        expected = {"valid_fix": True} if claim_type == "GPS" else 75
        
        claim = Claim(
            id=f"stress_claim_{i:04d}",
            statement=f"Stress test claim {i}",
            claim_type=claim_type,
            subject=subject,
            expected_value=expected,
            confidence_model=0.8,
        )
        claims.append(claim)
    
    # Validate all claims
    start_time = time.time()
    results = await provider.validate_batch(claims)
    elapsed = time.time() - start_time
    
    # Verify results
    assert len(results) == 1000, "Should have 1000 results"
    assert all(isinstance(r, ValidationResult) for r in results), "All should be ValidationResult"
    
    successful = sum(1 for r in results if r.confidence > 0)
    log.info(f"✓ Validated 1000 claims in {elapsed:.2f}s ({successful} successful)")
    log.info(f"  Average time per claim: {(elapsed/1000)*1000:.2f}ms")


@pytest.mark.stress
@pytest.mark.asyncio
async def test_concurrent_validations_100():
    """Test 100 concurrent validation calls."""
    log.info("Stress test: 100 concurrent validations...")
    
    provider = MQTTGroundTruthProvider(
        broker_host="mock-broker",
        broker_port=1883,
        cache_ttl_seconds=30,
    )
    
    # Populate with test data
    now = datetime.now(timezone.utc)
    provider._sensor_cache["concurrent-device"] = {
        "GPS.POSITION": {
            "data": {
                "gps_fix": True,
                "valid_fix": True,
                "latitude": 52.5,
                "longitude": 13.4,
                "age_ms": 100,
            },
            "timestamp": now,
            "freshness_ms": 100,
        },
    }
    provider._device_presence["concurrent-device"] = {
        "present": True,
        "last_heartbeat": now,
    }
    
    # Create 100 claims
    claims = [
        Claim(
            id=f"concurrent_{i:03d}",
            statement=f"Concurrent claim {i}",
            claim_type="test",
            subject="GPS.POSITION",
            expected_value={"valid_fix": True},
            confidence_model=0.8,
        )
        for i in range(100)
    ]
    
    # Run 100 concurrent validations
    start_time = time.time()
    results = await asyncio.gather(
        *[provider.validate(claim, context={"device_id": "concurrent-device"}) for claim in claims]
    )
    elapsed = time.time() - start_time
    
    # Verify results
    assert len(results) == 100, "Should have 100 results"
    assert all(isinstance(r, ValidationResult) for r in results), "All should be ValidationResult"
    
    log.info(f"✓ Completed 100 concurrent validations in {elapsed:.2f}s")
    log.info(f"  Throughput: {100/elapsed:.1f} claims/second")


@pytest.mark.stress
@pytest.mark.asyncio
async def test_rapid_device_updates():
    """Test rapid updates from multiple devices."""
    log.info("Stress test: rapid device updates...")
    
    provider = MQTTGroundTruthProvider(
        broker_host="mock-broker",
        broker_port=1883,
        cache_ttl_seconds=30,
    )
    
    # Simulate 10 devices sending 100 updates each
    num_devices = 10
    updates_per_device = 100
    
    async def update_device(device_id: int):
        """Simulate updates from a single device."""
        for update_num in range(updates_per_device):
            now = datetime.now(timezone.utc)
            payload = {
                "device_id": f"device_{device_id:02d}",
                "timestamp": now,
                "gps": {
                    "valid_fix": True,
                    "latitude": 52.5 + (device_id * 0.01),
                    "longitude": 13.4 + (update_num * 0.001),
                    "age_ms": update_num % 5000,
                }
            }
            await provider._cache_telemetry(f"device_{device_id:02d}", payload)
    
    # Run all device updates concurrently
    start_time = time.time()
    await asyncio.gather(
        *[update_device(i) for i in range(num_devices)]
    )
    elapsed = time.time() - start_time
    
    total_updates = num_devices * updates_per_device
    active_devices = await provider.available_devices()
    
    log.info(f"✓ Processed {total_updates} device updates in {elapsed:.2f}s")
    log.info(f"  Throughput: {total_updates/elapsed:.1f} updates/second")
    log.info(f"  Active devices: {len(active_devices)}")


@pytest.mark.stress
@pytest.mark.asyncio
async def test_cache_contention():
    """Test cache lock contention with many concurrent reads/writes."""
    log.info("Stress test: cache lock contention...")
    
    provider = MQTTGroundTruthProvider(
        broker_host="mock-broker",
        broker_port=1883,
        cache_ttl_seconds=30,
    )
    
    # Populate initial data
    now = datetime.now(timezone.utc)
    for i in range(5):
        provider._sensor_cache[f"device_{i}"] = {
            "GPS.POSITION": {
                "data": {
                    "gps_fix": True,
                    "valid_fix": True,
                    "latitude": 52.5 + i * 0.1,
                    "longitude": 13.4,
                    "age_ms": 100,
                },
                "timestamp": now,
                "freshness_ms": 100,
            },
        }
        provider._device_presence[f"device_{i}"] = {
            "present": True,
            "last_heartbeat": now,
        }
    
    async def reader_task():
        """Simulate read operations (validations)."""
        for _ in range(50):
            await provider.available_devices()
    
    async def writer_task():
        """Simulate write operations (telemetry updates)."""
        for _ in range(50):
            payload = {
                "device_id": "device_0",
                "timestamp": datetime.now(timezone.utc),
                "gps": {
                    "valid_fix": True,
                    "latitude": 52.5,
                    "longitude": 13.4,
                    "age_ms": 100,
                }
            }
            await provider._cache_telemetry("device_0", payload)
    
    # Run readers and writers concurrently
    start_time = time.time()
    await asyncio.gather(
        *[reader_task() for _ in range(10)],
        *[writer_task() for _ in range(5)]
    )
    elapsed = time.time() - start_time
    
    log.info(f"✓ Completed read/write contention test in {elapsed:.2f}s")
    log.info(f"  Total operations: {(10 * 50) + (5 * 50)}")


def run_stress_tests_summary():
    """Print guidance for running stress tests."""
    print("""
╔═════════════════════════════════════════════════════════════════════════════╗
║                        STRESS TEST INSTRUCTIONS                            ║
╠═════════════════════════════════════════════════════════════════════════════╣
║                                                                             ║
║ These tests simulate high load scenarios. They are optional and marked     ║
║ with @pytest.mark.stress to allow selective execution.                     ║
║                                                                             ║
║ Run all tests:                                                              ║
║   pytest tests/test_stress.py -v -m stress                                ║
║                                                                             ║
║ Run a specific stress test:                                                 ║
║   pytest tests/test_stress.py::test_1000_claims_validation -v -m stress   ║
║                                                                             ║
║ Skip stress tests (default):                                                ║
║   pytest tests/test_stress.py -v -m "not stress"                          ║
║                                                                             ║
║ Scenarios tested:                                                            ║
║   1. 1000 claims validation - throughput test                              ║
║   2. 100 concurrent validations - concurrency test                         ║
║   3. Rapid device updates (10 devices, 100 updates each)                   ║
║   4. Cache lock contention (readers and writers together)                  ║
║                                                                             ║
╚═════════════════════════════════════════════════════════════════════════════╝
""")


if __name__ == "__main__":
    run_stress_tests_summary()
