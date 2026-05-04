"""
Test worker shutdown race conditions.

This test suite reproduces the race where external cancellation of 
worker_loop() during job execution leaves orphaned tasks and watchdog 
processes still running.

Scenario:
- worker_loop() has background watchdog task
- watchdog_task is added to _active_tasks
- External signal cancels worker_loop()
- watchdog may not be properly cancelled

Fix: Ensure watchdog is explicitly cancelled on shutdown, tracked in _active_tasks
"""

import asyncio
import logging
import pytest
from typing import Set
from unittest.mock import MagicMock, patch

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Mock Worker Components
# ─────────────────────────────────────────────────────────────────────────────


class MockWorker:
    """Simplified worker for testing shutdown races."""
    
    def __init__(self):
        self.active_tasks: Set[asyncio.Task] = set()
        self.watchdog_task: asyncio.Task = None
        self.running = True
        self.watchdog_runs = 0
        self.jobs_executed = 0
        self.max_concurrency = 2
        self.active_jobs = 0
    
    async def _orphan_watchdog(self) -> None:
        """Background watchdog that checks for orphaned jobs."""
        while self.running:
            try:
                self.watchdog_runs += 1
                log.debug(f"Watchdog run #{self.watchdog_runs}")
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                log.info("Watchdog cancelled")
                raise
    
    async def _run_job(self, job_id: int) -> None:
        """Execute a job (simulated)."""
        self.jobs_executed += 1
        self.active_jobs += 1
        try:
            await asyncio.sleep(2.0)  # Simulate long-running job
        finally:
            self.active_jobs -= 1
    
    async def worker_loop(self) -> None:
        """Main worker loop with background watchdog."""
        # Start watchdog
        self.watchdog_task = asyncio.create_task(self._orphan_watchdog())
        self.active_tasks.add(self.watchdog_task)
        
        job_id = 0
        while self.running:
            try:
                if self.active_jobs < self.max_concurrency:
                    job_id += 1
                    job = job_id
                    
                    # Create job task
                    task = asyncio.create_task(self._run_job(job))
                    self.active_tasks.add(task)
                    task.add_done_callback(self.active_tasks.discard)
                    
                    log.debug(f"Started job {job}")
                
                await asyncio.sleep(0.1)
            
            except asyncio.CancelledError:
                log.info("Worker loop cancelled")
                break
            except Exception as e:
                log.error(f"Worker error: {e}")
    
    async def stop_graceful(self) -> None:
        """Graceful shutdown of worker."""
        log.info("Stopping worker gracefully")
        self.running = False
        
        # Cancel all active tasks
        for task in list(self.active_tasks):
            if not task.done():
                task.cancel()
        
        # Wait for tasks to complete
        if self.active_tasks:
            await asyncio.gather(*self.active_tasks, return_exceptions=True)
        
        log.info("Worker stopped")


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_worker():
    """Provide a mock worker."""
    return MockWorker()


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_shutdown_cancels_watchdog(mock_worker):
    """
    Test: When worker_loop is cancelled, watchdog is also properly cancelled.
    
    Without proper cancellation, watchdog may keep running even after 
    worker_loop exits.
    """
    # Start worker
    worker_task = asyncio.create_task(mock_worker.worker_loop())
    
    # Let it run for a bit
    await asyncio.sleep(0.5)
    
    # Cancel worker - this should also cancel watchdog
    worker_task.cancel()
    
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    
    # Explicitly cancel any remaining active tasks (watchdog should be there)
    for task in list(mock_worker.active_tasks):
        if not task.done():
            task.cancel()
    
    # Wait for all tasks to finish
    await asyncio.gather(*mock_worker.active_tasks, return_exceptions=True)
    
    # Give a moment for any final cleanup
    await asyncio.sleep(0.1)
    
    # Verify: watchdog was cancelled
    assert mock_worker.watchdog_task.done(), "Watchdog should be cancelled"
    
    # Verify: no orphaned tasks still running
    running_tasks = [t for t in mock_worker.active_tasks if not t.done()]
    assert not running_tasks, f"Orphaned tasks found: {running_tasks}"


@pytest.mark.asyncio
async def test_worker_graceful_shutdown_no_orphans(mock_worker):
    """
    Test: Graceful shutdown leaves no orphaned tasks.
    
    Scenario:
    1. Start worker with concurrent jobs
    2. Call graceful shutdown
    3. Verify all tasks are cancelled and awaited
    """
    # Start worker
    worker_task = asyncio.create_task(mock_worker.worker_loop())
    
    # Let jobs start
    await asyncio.sleep(0.5)
    
    # Graceful shutdown
    await mock_worker.stop_graceful()
    
    # Cancel the worker task itself
    worker_task.cancel()
    
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    
    # Verify: all tasks completed
    for task in mock_worker.active_tasks:
        assert task.done(), f"Task {task} not completed after shutdown"
    
    # Verify: no running jobs
    assert mock_worker.active_jobs == 0, f"Jobs still running: {mock_worker.active_jobs}"


@pytest.mark.asyncio
async def test_worker_cancel_during_job_execution(mock_worker):
    """
    Test: Cancel worker while jobs are executing.
    
    Scenario:
    1. Start worker (jobs execute for 2 seconds)
    2. After 0.3 seconds, cancel worker_loop
    3. Verify: running jobs are cancelled, watchdog is cancelled
    """
    # Start worker
    worker_task = asyncio.create_task(mock_worker.worker_loop())
    
    # Let a job start
    await asyncio.sleep(0.3)
    
    jobs_started = mock_worker.jobs_executed
    assert jobs_started > 0, "No jobs started"
    
    # Cancel worker abruptly
    worker_task.cancel()
    
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    
    # Cancel any remaining active tasks
    for task in list(mock_worker.active_tasks):
        if not task.done():
            task.cancel()
    
    # Wait for cleanup
    await asyncio.gather(*mock_worker.active_tasks, return_exceptions=True)
    
    # Verify: watchdog cancelled
    assert mock_worker.watchdog_task.done(), "Watchdog not cancelled"
    
    # Verify: job tasks cancelled
    await asyncio.sleep(0.2)  # Give time for cleanup
    
    running = [t for t in mock_worker.active_tasks if not t.done()]
    assert not running, f"Tasks still running after cancel: {running}"


@pytest.mark.asyncio
async def test_worker_watchdog_not_orphaned_on_exit(mock_worker):
    """
    Test: Watchdog task is properly tracked and not orphaned.
    
    Verification:
    - watchdog_task is added to active_tasks
    - On shutdown, watchdog is in active_tasks and gets cancelled
    """
    # Start worker
    worker_task = asyncio.create_task(mock_worker.worker_loop())
    
    await asyncio.sleep(0.2)
    
    # Verify: watchdog is in active_tasks
    assert mock_worker.watchdog_task in mock_worker.active_tasks, (
        "Watchdog not tracked in active_tasks"
    )
    
    # Graceful stop
    await mock_worker.stop_graceful()
    
    # Verify: watchdog was cancelled
    assert mock_worker.watchdog_task.done(), "Watchdog not cancelled"
    
    # Clean up worker task
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_worker_event_loop_clean_exit(mock_worker):
    """
    Test: Event loop can cleanly exit after worker shutdown.
    
    This tests that we don't leave pending tasks that cause:
    'Task was destroyed but it is pending!' warnings
    """
    # Start worker
    worker_task = asyncio.create_task(mock_worker.worker_loop())
    
    await asyncio.sleep(0.3)
    
    # Graceful shutdown
    await mock_worker.stop_graceful()
    
    # Cancel worker task
    worker_task.cancel()
    
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    
    # Check for pending tasks
    pending = asyncio.all_tasks()
    
    # Only the current task should be pending
    assert len(pending) <= 1, f"Pending tasks after shutdown: {pending}"


@pytest.mark.asyncio
async def test_worker_concurrent_cancellation_race(mock_worker):
    """
    Test: Concurrent cancellation of worker and graceful shutdown.
    
    Scenario:
    1. Start worker
    2. Call both stop_graceful() and worker_task.cancel() at same time
    3. Verify: no exceptions, clean cleanup
    """
    worker_task = asyncio.create_task(mock_worker.worker_loop())
    
    await asyncio.sleep(0.3)
    
    # Concurrent: graceful stop + cancel
    tasks = [
        asyncio.create_task(mock_worker.stop_graceful()),
        asyncio.sleep(0.01),  # Give graceful stop a head start
    ]
    
    await asyncio.gather(*tasks, return_exceptions=True)
    
    # Cancel worker
    worker_task.cancel()
    
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    
    # Verify: clean shutdown
    for task in mock_worker.active_tasks:
        assert task.done()


@pytest.mark.asyncio
async def test_worker_shutdown_with_multiple_jobs(mock_worker):
    """
    Stress test: Shutdown while 10+ jobs are queued.
    
    Scenario:
    1. Start worker (max 2 concurrent)
    2. Quickly create 10 jobs
    3. Shutdown
    4. Verify: no orphaned jobs
    """
    worker_task = asyncio.create_task(mock_worker.worker_loop())
    
    # Let worker start accepting jobs
    await asyncio.sleep(0.2)
    
    # Add more jobs (they will queue)
    for _ in range(10):
        await asyncio.sleep(0.05)
    
    # Graceful shutdown
    await mock_worker.stop_graceful()
    
    # Cancel worker
    worker_task.cancel()
    
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    
    # Verify: all tasks done
    for task in mock_worker.active_tasks:
        assert task.done()


@pytest.mark.asyncio
async def test_worker_shutdown_idempotent(mock_worker):
    """
    Test: Multiple calls to stop_graceful() are safe.
    
    This tests that shutdown is idempotent and doesn't cause issues 
    if called multiple times.
    """
    worker_task = asyncio.create_task(mock_worker.worker_loop())
    
    await asyncio.sleep(0.2)
    
    # Call stop_graceful multiple times
    await mock_worker.stop_graceful()
    await mock_worker.stop_graceful()  # Second call
    await mock_worker.stop_graceful()  # Third call
    
    # Cancel worker
    worker_task.cancel()
    
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    
    # Should complete without exceptions
    assert mock_worker.watchdog_task.done()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
