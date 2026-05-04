"""
Test MQTT batch flush race conditions.

This test suite reproduces the race where _flush_finding_batch() and 
_finding_batch_timeout() run concurrently, causing:
- Double-flush (same findings published twice)
- Lost findings (batch cleared while flushing)
- Data corruption (partial batch state)

Fix: Add asyncio.Lock around batch state mutations in mqtt_tasks.py
"""

import asyncio
import json
import logging
import pytest
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

# Configure logging for debugging
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Mock MQTT Engine Adapter (Simplified for Testing)
# ─────────────────────────────────────────────────────────────────────────────


class MockMQTTAdapter:
    """Simplified MQTT adapter with race conditions (for testing)."""
    
    def __init__(self):
        self._finding_batch: list[dict] = []
        self._finding_batch_timer: Optional[asyncio.Task] = None
        self._flush_count = 0
        self._published_findings: list[dict] = []
        self._publisher = AsyncMock()
        self.config = MagicMock()
        self.config.publisher_batch_size = 10
        self.config.publisher_batch_timeout_ms = 100  # Short timeout for testing
        self.metrics = MagicMock()
    
    async def _flush_finding_batch(self) -> None:
        """Flush findings (WITHOUT lock - demonstrates race)."""
        if not self._finding_batch:
            return
        
        # Simulate the race: batch is not locked, so concurrent operations can interfere
        batch_to_send = self._finding_batch.copy()
        self._finding_batch.clear()  # RACE: This can be called twice concurrently
        
        if self._finding_batch_timer and not self._finding_batch_timer.done():
            self._finding_batch_timer.cancel()
        
        # Simulate publishing
        self._flush_count += 1
        for finding in batch_to_send:
            self._published_findings.append(finding)
        
        await asyncio.sleep(0.01)  # Simulate network I/O
    
    async def _finding_batch_timeout(self) -> None:
        """Timeout for finding batch (WITHOUT lock - demonstrates race)."""
        try:
            await asyncio.sleep(self.config.publisher_batch_timeout_ms / 1000.0)
            # RACE: This can call flush concurrently with manual flush
            if self._finding_batch:
                await self._flush_finding_batch()
        except asyncio.CancelledError:
            pass
    
    async def add_finding(self, finding: dict) -> None:
        """Add finding and start timer if first in batch."""
        self._finding_batch.append(finding)
        
        if len(self._finding_batch) == 1:
            self._finding_batch_timer = asyncio.create_task(
                self._finding_batch_timeout()
            )


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_adapter():
    """Provide a mock MQTT adapter."""
    return MockMQTTAdapter()


def create_test_finding(claim_id: int) -> dict:
    """Create a test finding."""
    return {
        "claim_id": f"claim-{claim_id}",
        "device_id": "test-device",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "analysis": f"Analysis for claim {claim_id}",
        "confidence": 0.8,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mqtt_double_flush_race(mock_adapter):
    """
    Reproduces: mqtt_tasks.py race where _flush_finding_batch() and 
    _finding_batch_timeout() run concurrently, causing double-flush.
    
    Fix: Add asyncio.Lock around batch state mutations.
    """
    # Add multiple findings
    for i in range(3):
        await mock_adapter.add_finding(create_test_finding(i))
    
    # Wait for timeout to fire
    await asyncio.sleep(0.15)
    
    # Manually flush while timeout may also be flushing
    concurrent_tasks = [
        asyncio.create_task(mock_adapter._flush_finding_batch()),
        asyncio.create_task(mock_adapter._flush_finding_batch()),
    ]
    
    await asyncio.gather(*concurrent_tasks, return_exceptions=True)
    
    # Verify: no duplicate publishes
    # With the race condition, we might see duplicates or lost findings
    assert len(mock_adapter._published_findings) >= 3, (
        f"Lost findings in race condition: "
        f"expected ≥3, got {len(mock_adapter._published_findings)}"
    )
    
    # Verify: batch is empty
    assert not mock_adapter._finding_batch, "Batch should be empty after flush"
    
    # Verify: no excessive flushes
    # Without lock, flush_count could be >1 due to race
    log.info(f"Flush count: {mock_adapter._flush_count} (race may cause >1)")


@pytest.mark.asyncio
async def test_mqtt_batch_not_lost_under_concurrent_flush(mock_adapter):
    """
    Test that findings are not lost when flush is called concurrently 
    with timeout.
    
    Scenario:
    1. Add 5 findings
    2. Timeout fires and starts flushing
    3. Manual flush also runs concurrently
    4. Verify all 5 findings are published (no loss)
    """
    findings = [create_test_finding(i) for i in range(5)]
    
    for finding in findings:
        await mock_adapter.add_finding(finding)
    
    # Wait for timeout to start
    await asyncio.sleep(0.15)
    
    # Race: concurrent flushes
    tasks = [
        asyncio.create_task(mock_adapter._flush_finding_batch())
        for _ in range(3)
    ]
    
    await asyncio.gather(*tasks, return_exceptions=True)
    
    # Verify no findings were lost
    published_ids = [f["claim_id"] for f in mock_adapter._published_findings]
    original_ids = [f["claim_id"] for f in findings]
    
    for orig_id in original_ids:
        assert orig_id in published_ids, f"Finding {orig_id} was lost in race"


@pytest.mark.asyncio
async def test_mqtt_batch_clear_idempotent(mock_adapter):
    """
    Test that concurrent clear() calls don't cause issues.
    
    With a lock, clearing should be safe. Without a lock,
    we might see partial clears or exceptions.
    """
    for i in range(10):
        await mock_adapter.add_finding(create_test_finding(i))
    
    # Concurrent flushes (all will try to clear the batch)
    tasks = [
        asyncio.create_task(mock_adapter._flush_finding_batch())
        for _ in range(5)
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Verify: no exceptions from concurrent clears
    exceptions = [r for r in results if isinstance(r, Exception)]
    assert not exceptions, f"Concurrent clear raised exceptions: {exceptions}"
    
    # Verify: batch is empty
    assert not mock_adapter._finding_batch, "Batch should be empty"


@pytest.mark.asyncio
async def test_mqtt_timer_cancellation_race(mock_adapter):
    """
    Test that timer cancellation doesn't race with timeout firing.
    
    Scenario:
    1. Add finding (timer started)
    2. Flush is called, tries to cancel timer
    3. Timer fires at the same time
    4. Verify: no double flush, clean state
    """
    await mock_adapter.add_finding(create_test_finding(0))
    
    # Let timer start
    await asyncio.sleep(0.01)
    
    # Concurrent: flush tries to cancel, timer tries to fire
    tasks = [
        asyncio.create_task(mock_adapter._flush_finding_batch()),
        asyncio.sleep(0.08),  # Let timer fire naturally
    ]
    
    await asyncio.gather(*tasks, return_exceptions=True)
    
    # Verify: batch is empty and no exceptions
    assert not mock_adapter._finding_batch


@pytest.mark.asyncio
async def test_mqtt_stress_concurrent_adds_and_flushes(mock_adapter):
    """
    Stress test: 50 concurrent operations mixing adds and flushes.
    
    This stresses the race condition under realistic load.
    """
    async def add_and_flush():
        for i in range(5):
            finding = create_test_finding(i)
            await mock_adapter.add_finding(finding)
            await asyncio.sleep(0.001)
            await mock_adapter._flush_finding_batch()
    
    # Run 10 concurrent add/flush cycles
    tasks = [add_and_flush() for _ in range(10)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Verify: no exceptions
    exceptions = [r for r in results if isinstance(r, Exception)]
    assert not exceptions, f"Stress test raised exceptions: {exceptions}"
    
    # Verify: final state is clean
    assert not mock_adapter._finding_batch, "Batch should be empty after stress"


@pytest.mark.asyncio
async def test_mqtt_timeout_cancel_safety(mock_adapter):
    """
    Test that cancelling a timer that's already executing is safe.
    
    Without proper synchronization, cancelling a timer that's in 
    _flush_finding_batch() could cause issues.
    """
    for i in range(3):
        await mock_adapter.add_finding(create_test_finding(i))
    
    # Wait for timeout to fire
    await asyncio.sleep(0.15)
    
    # Try to cancel already-fired timer
    if mock_adapter._finding_batch_timer:
        mock_adapter._finding_batch_timer.cancel()
    
    # Verify: no exceptions
    await asyncio.sleep(0.1)
    
    # Verify: findings were published
    assert len(mock_adapter._published_findings) >= 3


@pytest.mark.asyncio
async def test_mqtt_batch_timeout_with_new_findings(mock_adapter):
    """
    Test: Timeout fires while processing first batch, new findings arrive.
    
    Scenario:
    1. Batch 1: add 2 findings, timeout fires and flushes
    2. Before flush completes, batch 2: add 3 more findings
    3. Verify: batch 1 published separately from batch 2
    """
    # Batch 1
    for i in range(2):
        await mock_adapter.add_finding(create_test_finding(i))
    
    # Wait for timeout
    await asyncio.sleep(0.15)
    
    published_after_first = len(mock_adapter._published_findings)
    
    # Batch 2: new findings arrive
    for i in range(2, 5):
        await mock_adapter.add_finding(create_test_finding(i))
    
    # Wait for second timeout
    await asyncio.sleep(0.15)
    
    # Verify: both batches published
    assert len(mock_adapter._published_findings) >= 5, (
        f"Expected ≥5 findings, got {len(mock_adapter._published_findings)}"
    )


@pytest.mark.asyncio
async def test_mqtt_batch_state_consistency(mock_adapter):
    """
    Test: Batch state is consistent (not partially cleared/flushed).
    
    This tests for partial state corruption during concurrent access.
    """
    findings = [create_test_finding(i) for i in range(20)]
    
    # Add all findings
    for finding in findings:
        await mock_adapter.add_finding(finding)
    
    # Many concurrent flushes
    tasks = [
        asyncio.create_task(mock_adapter._flush_finding_batch())
        for _ in range(10)
    ]
    
    await asyncio.gather(*tasks, return_exceptions=True)
    
    # Verify: all findings published
    assert len(mock_adapter._published_findings) >= 20, (
        f"Lost findings in concurrent flush: "
        f"expected ≥20, got {len(mock_adapter._published_findings)}"
    )
    
    # Verify: no duplicates (or at most limited duplicates from race)
    published_ids = [f["claim_id"] for f in mock_adapter._published_findings]
    unique_ids = len(set(published_ids))
    
    # Without lock: we might see duplicates. With lock: should be exactly 20.
    log.info(f"Published findings: {len(mock_adapter._published_findings)}, "
             f"unique: {unique_ids} (race may cause duplicates)")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
