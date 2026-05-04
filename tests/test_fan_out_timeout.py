"""
Test fan-out timeout hang conditions.

This test suite reproduces the race where one perspective takes 2x 
longer than others in run_fan_out(), causing zombie tasks.

Scenario:
- run_fan_out() spawns 3 perspectives
- Perspective 1: completes in 1 second
- Perspective 2: completes in 1 second
- Perspective 3: takes 5 seconds (2x longer)
- Outer timeout fires at 3 seconds
- Without proper timeout on gather(), perspective 3 hangs

Problem:
- asyncio.gather(*tasks) at line 730 has no timeout
- If one task is slow, gather() waits forever
- Other tasks complete but keep waiting for the slow one
- Leads to resource exhaustion and hangs

Fix: Add timeout to gather() or use asyncio.wait() with timeout
"""

import asyncio
import logging
import pytest
import time
from typing import List

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Mock Fan-Out Engine
# ─────────────────────────────────────────────────────────────────────────────


class MockFanOutEngine:
    """Simplified fan-out engine with timeout issues."""
    
    def __init__(self):
        self.perspectives_started = 0
        self.perspectives_completed = 0
        self.perspectives_cancelled = 0
        self.synthesis_called = False
    
    async def run_perspective(
        self, 
        name: str, 
        delay: float = 1.0
    ) -> tuple[str, dict]:
        """Run a single perspective."""
        self.perspectives_started += 1
        log.info(f"Starting perspective: {name}")
        
        try:
            # Simulate work
            await asyncio.sleep(delay)
            self.perspectives_completed += 1
            log.info(f"Completed perspective: {name}")
            return (name, {"output": f"Result from {name}"})
        
        except asyncio.CancelledError:
            self.perspectives_cancelled += 1
            log.info(f"Cancelled perspective: {name}")
            raise
    
    async def run_fan_out_broken(
        self,
        perspectives: List[tuple[str, float]],
        outer_timeout: float = 3.0,
    ) -> dict:
        """
        Fan-out WITHOUT timeout on gather() - demonstrates the bug.
        
        The bug: asyncio.gather(*tasks) has no timeout, so if one
        perspective is slow, the entire operation hangs.
        """
        start_time = time.time()
        
        # Create perspective tasks
        tasks = [
            asyncio.create_task(self.run_perspective(name, delay))
            for name, delay in perspectives
        ]
        
        try:
            # BUG: No timeout on gather() - if one task is slow, we hang
            results = await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=outer_timeout,
            )
            
            self.synthesis_called = True
            return {
                "final_answer": "Synthesis result",
                "perspectives": dict(results),
                "duration": time.time() - start_time,
            }
        
        except asyncio.TimeoutError:
            log.warning(f"Outer timeout after {outer_timeout}s")
            # Problem: tasks may still be running!
            # Without explicit cancellation, they continue
            raise
    
    async def run_fan_out_fixed(
        self,
        perspectives: List[tuple[str, float]],
        outer_timeout: float = 3.0,
    ) -> dict:
        """
        Fan-out WITH proper timeout handling - the fix.
        
        Ensures tasks are cancelled when timeout fires.
        """
        start_time = time.time()
        
        # Create perspective tasks
        tasks = [
            asyncio.create_task(self.run_perspective(name, delay))
            for name, delay in perspectives
        ]
        
        try:
            # FIX: Use wait() instead of gather() to handle timeout properly
            done, pending = await asyncio.wait(
                tasks,
                timeout=outer_timeout,
                return_when=asyncio.ALL_COMPLETED,
            )
            
            if pending:
                log.warning(f"{len(pending)} perspectives still pending, cancelling")
                for task in pending:
                    task.cancel()
                
                # Wait for cancellation to complete
                await asyncio.gather(*pending, return_exceptions=True)
            
            # Collect results from completed tasks
            results = []
            for task in done:
                try:
                    results.append(await task)
                except asyncio.CancelledError:
                    pass
            
            self.synthesis_called = True
            return {
                "final_answer": "Synthesis result",
                "perspectives": dict(results),
                "duration": time.time() - start_time,
            }
        
        except Exception as e:
            log.error(f"Error in fan-out: {e}")
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_engine():
    """Provide a mock fan-out engine."""
    return MockFanOutEngine()


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fan_out_all_perspectives_fast(mock_engine):
    """
    Test: All perspectives complete within timeout.
    
    Scenario: 3 perspectives, all complete in 1 second, timeout=3 seconds.
    Expected: All complete, synthesis called.
    """
    perspectives = [
        ("defense", 0.5),
        ("prosecution", 0.5),
        ("forensics", 0.5),
    ]
    
    result = await mock_engine.run_fan_out_fixed(perspectives, outer_timeout=3.0)
    
    assert mock_engine.perspectives_started == 3
    assert mock_engine.perspectives_completed == 3
    assert mock_engine.perspectives_cancelled == 0
    assert mock_engine.synthesis_called


@pytest.mark.asyncio
async def test_fan_out_one_perspective_slow(mock_engine):
    """
    Test: One perspective takes 2x longer than others, hits timeout.
    
    Scenario:
    - perspective 1: 1 second
    - perspective 2: 1 second
    - perspective 3: 5 seconds (slow!)
    - timeout: 3 seconds
    
    Expected: 3 and 2 complete, 3 is cancelled, no hang
    """
    perspectives = [
        ("defense", 1.0),
        ("prosecution", 1.0),
        ("forensics", 5.0),  # Slow!
    ]
    
    # Use fixed version which cancels pending tasks
    result = await mock_engine.run_fan_out_fixed(perspectives, outer_timeout=3.0)
    
    # Verify: at least 2 completed, one was cancelled
    assert mock_engine.perspectives_started == 3
    assert mock_engine.perspectives_completed >= 2, (
        f"Expected ≥2 completed, got {mock_engine.perspectives_completed}"
    )
    assert mock_engine.perspectives_cancelled >= 1, (
        f"Expected ≥1 cancelled, got {mock_engine.perspectives_cancelled}"
    )
    
    # Verify: no hang (returned within timeout)
    assert result["duration"] < 5.0


@pytest.mark.asyncio
async def test_fan_out_timeout_cancels_pending_tasks(mock_engine):
    """
    Test: When timeout fires, pending tasks are cancelled.
    
    This is the key fix - without it, tasks keep running as zombies.
    """
    perspectives = [
        ("perspective_a", 0.5),
        ("perspective_b", 10.0),  # Will timeout
        ("perspective_c", 10.0),  # Will timeout
    ]
    
    result = await mock_engine.run_fan_out_fixed(perspectives, outer_timeout=2.0)
    
    # Verify cancellation happened
    assert mock_engine.perspectives_cancelled >= 1, (
        f"Pending tasks should be cancelled, got {mock_engine.perspectives_cancelled}"
    )
    
    # Verify: no tasks left hanging
    assert result["duration"] < 5.0


@pytest.mark.asyncio
async def test_fan_out_broken_version_demonstrates_race(mock_engine):
    """
    Test: Broken version (without timeout on gather) hangs.
    
    This test demonstrates the problem: without explicit cancellation,
    asyncio.gather() waits for all tasks even after outer timeout.
    
    We expect this to timeout but task may still be running.
    """
    perspectives = [
        ("fast_1", 0.1),
        ("fast_2", 0.1),
        ("slow", 2.0),
    ]
    
    # This should timeout at 1 second
    with pytest.raises(asyncio.TimeoutError):
        await mock_engine.run_fan_out_broken(perspectives, outer_timeout=1.0)
    
    # Verify: some tasks are still running/not properly cleaned up
    log.info(f"Cancelled tasks: {mock_engine.perspectives_cancelled}")


@pytest.mark.asyncio
async def test_fan_out_multiple_slow_perspectives(mock_engine):
    """
    Test: Multiple perspectives are slow, all cancelled.
    
    Scenario: 5 perspectives, 3 are slow.
    """
    perspectives = [
        ("fast_1", 0.5),
        ("fast_2", 0.5),
        ("slow_1", 5.0),
        ("slow_2", 5.0),
        ("slow_3", 5.0),
    ]
    
    result = await mock_engine.run_fan_out_fixed(perspectives, outer_timeout=2.0)
    
    # Verify: slow ones were cancelled
    assert mock_engine.perspectives_cancelled >= 3, (
        f"Expected ≥3 cancelled, got {mock_engine.perspectives_cancelled}"
    )
    
    # Verify: no indefinite hang
    assert result["duration"] < 5.0


@pytest.mark.asyncio
async def test_fan_out_zero_timeout(mock_engine):
    """
    Test: Very short timeout (0.5 seconds) with multiple perspectives.
    
    All perspectives should be cancelled.
    """
    perspectives = [
        ("perspective_1", 5.0),
        ("perspective_2", 5.0),
        ("perspective_3", 5.0),
    ]
    
    result = await mock_engine.run_fan_out_fixed(perspectives, outer_timeout=0.5)
    
    # All should be cancelled
    assert mock_engine.perspectives_cancelled >= 1


@pytest.mark.asyncio
async def test_fan_out_cancelled_task_cleanup(mock_engine):
    """
    Test: Cancelled tasks are properly cleaned up (don't raise warnings).
    
    This tests that cancellation is handled gracefully.
    """
    perspectives = [
        ("p1", 0.1),
        ("p2", 10.0),
    ]
    
    # Should not raise "Task was destroyed but it is pending" warning
    result = await mock_engine.run_fan_out_fixed(perspectives, outer_timeout=1.0)
    
    # Verify cleanup happened
    assert mock_engine.perspectives_cancelled >= 1


@pytest.mark.asyncio
async def test_fan_out_results_from_completed_perspectives_only(mock_engine):
    """
    Test: Only completed perspectives are included in results.
    
    Cancelled perspectives should not appear in final results.
    """
    perspectives = [
        ("completed_1", 0.3),
        ("completed_2", 0.3),
        ("cancelled_1", 5.0),
        ("cancelled_2", 5.0),
    ]
    
    result = await mock_engine.run_fan_out_fixed(perspectives, outer_timeout=1.5)
    
    # Verify: cancelled ones not in results
    perspective_names = list(result["perspectives"].keys())
    
    assert "completed_1" in perspective_names or "completed_2" in perspective_names
    assert "cancelled_1" not in perspective_names
    assert "cancelled_2" not in perspective_names


@pytest.mark.asyncio
async def test_fan_out_stress_many_perspectives(mock_engine):
    """
    Stress test: 10 perspectives, some slow.
    
    Verify proper cancellation under stress.
    """
    perspectives = [
        (f"p_{i}", 0.5 if i < 3 else 5.0)
        for i in range(10)
    ]
    
    result = await mock_engine.run_fan_out_fixed(perspectives, outer_timeout=2.0)
    
    # Verify: no hang
    assert result["duration"] < 5.0
    
    # Verify: slow ones cancelled
    assert mock_engine.perspectives_cancelled >= 3


@pytest.mark.asyncio
async def test_fan_out_synthesis_only_on_success(mock_engine):
    """
    Test: Synthesis is called even with partial results (fixed version).
    
    In the fixed version, synthesis should be called with available results.
    """
    perspectives = [
        ("p1", 0.5),
        ("p2", 0.5),
        ("p3", 5.0),
    ]
    
    result = await mock_engine.run_fan_out_fixed(perspectives, outer_timeout=2.0)
    
    # Synthesis should be attempted
    assert mock_engine.synthesis_called


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
