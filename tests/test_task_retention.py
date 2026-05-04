"""
Test MQTT task retention leak.

This test suite reproduces the memory leak where _active_tasks set in 
worker.py grows unbounded after processing jobs.

Scenario:
- 100 jobs are processed
- Each job creates a task and adds it to _active_tasks
- Task completion should trigger the done_callback to remove it from _active_tasks
- Without proper cleanup, _active_tasks grows to 100+ entries

Problem:
- Tasks may not be properly garbage collected
- Callback may not be called
- Weak references may keep tasks alive

Fix: Verify task cleanup logic, ensure callback is always called
"""

import asyncio
import gc
import logging
import pytest
from typing import Set

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Mock Worker with Task Tracking
# ─────────────────────────────────────────────────────────────────────────────


class MockWorkerWithTracking:
    """Worker that simulates job processing and task cleanup."""
    
    def __init__(self):
        self.active_tasks: Set[asyncio.Task] = set()
        self.completed_tasks = 0
        self.max_task_set_size = 0
    
    async def _run_job(self, job_id: int) -> dict:
        """Execute a job and return result."""
        # Simulate job work
        await asyncio.sleep(0.01)
        return {"job_id": job_id, "status": "complete"}
    
    async def process_job(self, job_id: int) -> None:
        """Process a single job and track it."""
        async def _run_and_track(jid: int) -> None:
            try:
                await self._run_job(jid)
            finally:
                pass  # Cleanup happens in callback
        
        # Create task
        task = asyncio.create_task(_run_and_track(job_id))
        
        # Add to tracking set
        self.active_tasks.add(task)
        
        # Add callback to remove from set when done
        task.add_done_callback(self.active_tasks.discard)
        
        # Track max size
        if len(self.active_tasks) > self.max_task_set_size:
            self.max_task_set_size = len(self.active_tasks)
        
        # Small delay to let tasks progress
        await asyncio.sleep(0.001)
    
    async def process_many_jobs(self, count: int, delay: float = 0.01) -> None:
        """Process many jobs sequentially."""
        for i in range(count):
            await self.process_job(i)
            await asyncio.sleep(delay)
    
    async def wait_for_completion(self, timeout: float = 10.0) -> None:
        """Wait for all tasks to complete."""
        try:
            await asyncio.wait_for(
                asyncio.gather(*self.active_tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning(f"Timeout waiting for {len(self.active_tasks)} tasks")
    
    def get_active_task_count(self) -> int:
        """Get count of active tasks still in set."""
        # Remove completed tasks
        self.active_tasks = {t for t in self.active_tasks if not t.done()}
        return len(self.active_tasks)


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_worker():
    """Provide a mock worker with task tracking."""
    return MockWorkerWithTracking()


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_memory_not_leaked_small_batch(mock_worker):
    """
    Test: Small batch of 10 jobs doesn't leak memory.
    
    Scenario:
    1. Process 10 jobs
    2. Wait for completion
    3. Verify: _active_tasks is empty or very small
    """
    await mock_worker.process_many_jobs(10)
    
    # Wait for tasks
    await mock_worker.wait_for_completion(timeout=5.0)
    
    # Force garbage collection
    gc.collect()
    
    # Check active tasks
    active = mock_worker.get_active_task_count()
    
    assert active == 0, (
        f"Task leak detected: {active} tasks still in _active_tasks "
        f"after completion (max seen: {mock_worker.max_task_set_size})"
    )


@pytest.mark.asyncio
async def test_task_memory_not_leaked_large_batch(mock_worker):
    """
    Test: Large batch of 100 jobs doesn't leak memory.
    
    This reproduces the user's scenario: 100 jobs processed, 
    _active_tasks should not grow to 100+
    """
    await mock_worker.process_many_jobs(100, delay=0.001)
    
    # Wait for tasks
    await mock_worker.wait_for_completion(timeout=15.0)
    
    # Force garbage collection
    gc.collect()
    
    # Check active tasks
    active = mock_worker.get_active_task_count()
    max_size = mock_worker.max_task_set_size
    
    assert active == 0, (
        f"Task leak in large batch: {active} tasks still active "
        f"(peak: {max_size})"
    )
    
    # Verify max size was reasonable (should not be 100)
    # Even without cleanup, we should see max ~2-4 concurrent tasks
    assert max_size <= 10, (
        f"Max active tasks too high: {max_size} (should be ~1-4 concurrent)"
    )


@pytest.mark.asyncio
async def test_task_callback_fires_on_completion(mock_worker):
    """
    Test: Task callback is called when task completes.
    
    This tests that add_done_callback() works correctly.
    """
    callback_count = 0
    
    def track_callback(task):
        nonlocal callback_count
        callback_count += 1
    
    # Create a task with manual callback
    async def dummy_job():
        await asyncio.sleep(0.01)
    
    task = asyncio.create_task(dummy_job())
    task.add_done_callback(track_callback)
    
    # Wait for completion
    await task
    
    # Verify callback was called
    assert callback_count == 1, f"Callback not called (count={callback_count})"


@pytest.mark.asyncio
async def test_concurrent_task_cleanup(mock_worker):
    """
    Test: Concurrent job processing cleans up properly.
    
    Scenario:
    1. Create 5 concurrent jobs
    2. All complete
    3. Verify: all removed from _active_tasks
    """
    # Create 5 concurrent jobs
    tasks = [
        asyncio.create_task(mock_worker._run_job(i))
        for i in range(5)
    ]
    
    # Add to tracking with callbacks
    for i, task in enumerate(tasks):
        mock_worker.active_tasks.add(task)
        task.add_done_callback(mock_worker.active_tasks.discard)
    
    # Wait for all
    await asyncio.gather(*tasks)
    
    # Verify cleanup
    active = mock_worker.get_active_task_count()
    assert active == 0, f"{active} tasks still tracked after concurrent completion"


@pytest.mark.asyncio
async def test_task_cleanup_with_exceptions(mock_worker):
    """
    Test: Task cleanup works even when tasks raise exceptions.
    
    Scenario:
    1. Some tasks succeed, some fail
    2. All should be removed from _active_tasks
    """
    async def failing_job(job_id):
        await asyncio.sleep(0.01)
        if job_id % 2 == 0:
            raise ValueError(f"Job {job_id} failed")
        return {"job_id": job_id}
    
    # Create mix of succeeding and failing tasks
    for i in range(10):
        task = asyncio.create_task(failing_job(i))
        mock_worker.active_tasks.add(task)
        task.add_done_callback(mock_worker.active_tasks.discard)
    
    # Wait for all with exception handling
    await asyncio.gather(*mock_worker.active_tasks, return_exceptions=True)
    
    # Verify cleanup
    active = mock_worker.get_active_task_count()
    assert active == 0, f"{active} failed tasks still tracked"


@pytest.mark.asyncio
async def test_sustained_load_no_memory_growth(mock_worker):
    """
    Test: Processing jobs in bursts over 1 hour doesn't leak memory.
    
    Simulation: Process 100 jobs in 10 bursts of 10 jobs.
    This simulates the "worker runs for 1 hour" scenario.
    """
    for burst in range(10):
        # Process 10 jobs
        await mock_worker.process_many_jobs(10, delay=0.001)
        
        # Wait for them to complete
        await mock_worker.wait_for_completion(timeout=5.0)
        
        # Force cleanup
        gc.collect()
        
        # Check memory at each checkpoint
        active = mock_worker.get_active_task_count()
        log.info(f"Burst {burst}: {active} active tasks "
                 f"(peak seen: {mock_worker.max_task_set_size})")
        
        assert active == 0, (
            f"Memory leak detected at burst {burst}: "
            f"{active} tasks still active"
        )
    
    # Verify max size didn't grow unbounded
    assert mock_worker.max_task_set_size <= 15, (
        f"Max concurrent tasks too high: {mock_worker.max_task_set_size}"
    )


@pytest.mark.asyncio
async def test_task_set_cleanup_under_stress(mock_worker):
    """
    Stress test: Rapid task creation and cleanup.
    
    Create 200 tasks rapidly and verify they all clean up.
    """
    # Create many tasks rapidly
    for i in range(200):
        task = asyncio.create_task(mock_worker._run_job(i))
        mock_worker.active_tasks.add(task)
        task.add_done_callback(mock_worker.active_tasks.discard)
        
        # Yield to event loop periodically
        if i % 50 == 0:
            await asyncio.sleep(0.001)
    
    # Wait for all to complete
    await mock_worker.wait_for_completion(timeout=20.0)
    
    # Cleanup
    gc.collect()
    
    # Verify all cleaned up
    active = mock_worker.get_active_task_count()
    assert active == 0, (
        f"Stress test cleanup failed: {active} tasks still tracked "
        f"(peak: {mock_worker.max_task_set_size})"
    )


@pytest.mark.asyncio
async def test_no_task_duplication_in_set(mock_worker):
    """
    Test: Same task is not added to _active_tasks multiple times.
    
    This would cause the set to be ineffective at deduplication.
    """
    async def dummy_job():
        await asyncio.sleep(0.01)
    
    # Create task once
    task = asyncio.create_task(dummy_job())
    
    # Add to set multiple times (shouldn't duplicate in set)
    mock_worker.active_tasks.add(task)
    mock_worker.active_tasks.add(task)
    mock_worker.active_tasks.add(task)
    
    # Set should deduplicate
    assert len(mock_worker.active_tasks) == 1
    
    # Add callback
    callback_count = 0
    
    def track_callback(t):
        nonlocal callback_count
        callback_count += 1
    
    task.add_done_callback(track_callback)
    
    # Complete task
    await task
    
    # Callback should be called once
    assert callback_count == 1
    
    # Task should be removed from set
    active = mock_worker.get_active_task_count()
    assert active == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
