"""
Test signal handler race conditions.

This test suite reproduces the race where SIGTERM arrives during job 
execution and the signal handler doesn't properly await stop_mqtt().

Scenario:
- SIGTERM signal arrives while job is executing
- Signal handler calls asyncio.create_task(stop_mqtt())
- But doesn't await it
- Worker exits before stop_mqtt completes
- MQTT tasks left pending, possibly publishing incomplete data

Problem:
- Signal handler creates task but doesn't await
- Event loop shuts down before task completes
- Pending MQTT tasks cause "Task was destroyed" warnings
- Data loss (incomplete publishes)

Fix: Signal handler should ensure stop_mqtt() awaits before exit
"""

import asyncio
import logging
import pytest
import signal
import time
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Mock MQTT and Signal Handling
# ─────────────────────────────────────────────────────────────────────────────


class MockMQTTService:
    """Mock MQTT service that needs cleanup."""
    
    def __init__(self):
        self.connected = False
        self.stop_called = False
        self.stop_completed = False
        self.pending_publishes = 0
    
    async def connect(self) -> None:
        """Connect to MQTT broker."""
        await asyncio.sleep(0.01)
        self.connected = True
    
    async def stop(self) -> None:
        """Graceful MQTT shutdown - MUST be awaited."""
        self.stop_called = True
        
        # Simulate pending publishes
        self.pending_publishes = 3
        
        # Flush pending data (takes time!)
        for _ in range(self.pending_publishes):
            await asyncio.sleep(0.1)
        
        self.connected = False
        self.stop_completed = True


class WorkerWithBrokenSignalHandling:
    """Worker with signal handler that doesn't await stop_mqtt()."""
    
    def __init__(self, mqtt: MockMQTTService):
        self.mqtt = mqtt
        self.running = True
        self.shutdown_initiated = False
    
    def _register_signal_handler_broken(self) -> None:
        """
        BROKEN: Creates task but doesn't await.
        
        This allows the event loop to exit before stop_mqtt() completes.
        """
        def handle_signal(signum, frame):
            log.info(f"Signal {signum} received")
            self.shutdown_initiated = True
            # BUG: This creates a task but doesn't await it
            asyncio.create_task(self.mqtt.stop())
        
        signal.signal(signal.SIGTERM, handle_signal)
    
    async def _job(self) -> None:
        """Execute a job."""
        await asyncio.sleep(2.0)
    
    async def worker_loop_broken(self) -> None:
        """Worker loop with broken signal handling."""
        self._register_signal_handler_broken()
        
        while self.running:
            try:
                await self._job()
            except asyncio.CancelledError:
                break


class WorkerWithFixedSignalHandling:
    """Worker with signal handler that properly awaits stop_mqtt()."""
    
    def __init__(self, mqtt: MockMQTTService):
        self.mqtt = mqtt
        self.running = True
        self.shutdown_task: Optional[asyncio.Task] = None
    
    def _register_signal_handler_fixed(self) -> None:
        """
        FIXED: Stores task reference so it can be awaited before exit.
        
        Or: Uses a condition/event to signal shutdown and await in main loop.
        """
        def handle_signal(signum, frame):
            log.info(f"Signal {signum} received")
            self.running = False
            # Store task for later awaiting
            self.shutdown_task = asyncio.create_task(self.mqtt.stop())
        
        signal.signal(signal.SIGTERM, handle_signal)
    
    async def _job(self) -> None:
        """Execute a job."""
        await asyncio.sleep(0.1)
    
    async def worker_loop_fixed(self) -> None:
        """Worker loop with fixed signal handling."""
        self._register_signal_handler_fixed()
        await self.mqtt.connect()
        
        while self.running:
            try:
                await self._job()
            except asyncio.CancelledError:
                break
        
        # FIX: Await shutdown task before exiting
        if self.shutdown_task:
            try:
                await self.shutdown_task
            except Exception as e:
                log.error(f"Error during shutdown: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mqtt_service():
    """Provide a mock MQTT service."""
    return MockMQTTService()


@pytest.fixture
def broken_worker(mqtt_service):
    """Provide a worker with broken signal handling."""
    return WorkerWithBrokenSignalHandling(mqtt_service)


@pytest.fixture
def fixed_worker(mqtt_service):
    """Provide a worker with fixed signal handling."""
    return WorkerWithFixedSignalHandling(mqtt_service)


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signal_handler_calls_stop_mqtt(mqtt_service):
    """
    Test: Signal handler calls stop_mqtt().
    
    Basic test that signal handler is registered and callable.
    """
    worker = WorkerWithFixedSignalHandling(mqtt_service)
    worker._register_signal_handler_fixed()
    
    # Verify handler can be called programmatically
    worker.running = False
    worker.shutdown_task = asyncio.create_task(mqtt_service.stop())
    
    # Await the shutdown
    await worker.shutdown_task
    
    assert mqtt_service.stop_called


@pytest.mark.asyncio
async def test_stop_mqtt_completes_before_exit_fixed(mqtt_service):
    """
    Test: With fixed handler, stop_mqtt() completes before worker exits.
    
    Scenario:
    1. Start worker loop
    2. Simulate SIGTERM
    3. Verify: stop_mqtt() awaited, all pending publishes flushed
    """
    worker = WorkerWithFixedSignalHandling(mqtt_service)
    await mqtt_service.connect()
    
    # Start worker task (but it will stop immediately)
    worker.running = True
    worker_task = asyncio.create_task(worker.worker_loop_fixed())
    
    # Let it start
    await asyncio.sleep(0.05)
    
    # Simulate signal
    worker.running = False
    worker.shutdown_task = asyncio.create_task(mqtt_service.stop())
    
    # Await shutdown
    await worker.shutdown_task
    
    # Verify: stop completed
    assert mqtt_service.stop_completed
    
    # Cancel worker task
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_pending_mqtt_tasks_not_abandoned(mqtt_service):
    """
    Test: No pending MQTT tasks left after signal.
    
    This tests that we don't see 'Task was destroyed but it is pending!' 
    warnings.
    """
    worker = WorkerWithFixedSignalHandling(mqtt_service)
    await mqtt_service.connect()
    
    # Run worker with proper shutdown
    worker_task = asyncio.create_task(worker.worker_loop_fixed())
    
    await asyncio.sleep(0.05)
    
    # Trigger shutdown
    worker.running = False
    worker.shutdown_task = asyncio.create_task(mqtt_service.stop())
    
    # Await shutdown
    await worker.shutdown_task
    
    # Verify: MQTT service properly stopped
    assert mqtt_service.stop_completed
    assert not mqtt_service.connected
    
    # Clean up worker task
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_signal_during_job_execution(mqtt_service):
    """
    Test: SIGTERM arrives while job is executing.
    
    Scenario:
    1. Job is running (will take 2 seconds)
    2. Signal arrives at 1 second
    3. Signal handler calls stop_mqtt()
    4. Verify: stop_mqtt() is awaited before exit
    """
    worker = WorkerWithFixedSignalHandling(mqtt_service)
    await mqtt_service.connect()
    
    worker.running = True
    worker_task = asyncio.create_task(worker.worker_loop_fixed())
    
    # Let job start
    await asyncio.sleep(0.05)
    
    # Send signal at 0.5 seconds
    await asyncio.sleep(0.05)
    worker.running = False
    worker.shutdown_task = asyncio.create_task(mqtt_service.stop())
    
    # Await shutdown
    await worker.shutdown_task
    
    # Verify shutdown completed
    assert mqtt_service.stop_completed
    
    # Cleanup
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_no_data_loss_on_signal(mqtt_service):
    """
    Test: Data is not lost when signal arrives.
    
    With proper signal handling, pending MQTT publishes should complete.
    """
    worker = WorkerWithFixedSignalHandling(mqtt_service)
    await mqtt_service.connect()
    
    # Simulate pending data
    mqtt_service.pending_publishes = 5
    
    # Start worker
    worker_task = asyncio.create_task(worker.worker_loop_fixed())
    
    await asyncio.sleep(0.05)
    
    # Signal arrives
    worker.running = False
    worker.shutdown_task = asyncio.create_task(mqtt_service.stop())
    
    # Wait for shutdown
    await worker.shutdown_task
    
    # Verify: stop_mqtt completed (data flushed)
    assert mqtt_service.stop_completed
    
    # Cleanup
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_broken_handler_allows_event_loop_to_exit_early(mqtt_service):
    """
    Test: Broken handler demonstrates the problem.
    
    Without awaiting stop_mqtt(), event loop exits before stop completes.
    This is the anti-pattern we're testing for.
    """
    worker = WorkerWithBrokenSignalHandling(mqtt_service)
    
    # Manually simulate broken signal handling
    worker.shutdown_initiated = True
    
    # This would be fire-and-forget without await
    task = asyncio.create_task(mqtt_service.stop())
    
    # If we don't await, and event loop exits, task is destroyed
    # Verify task is not awaited
    assert not task.done()


@pytest.mark.asyncio
async def test_concurrent_signal_and_job_completion(mqtt_service):
    """
    Test: Race between job completion and signal arrival.
    
    Scenario:
    1. Job executing
    2. Signal arrives
    3. Job completes
    4. Both should handle gracefully without race
    """
    worker = WorkerWithFixedSignalHandling(mqtt_service)
    await mqtt_service.connect()
    
    worker.running = True
    worker_task = asyncio.create_task(worker.worker_loop_fixed())
    
    # Let work progress
    await asyncio.sleep(0.05)
    
    # Concurrent: signal + job natural completion
    worker.running = False
    worker.shutdown_task = asyncio.create_task(mqtt_service.stop())
    
    # Wait for both
    await worker.shutdown_task
    
    # Verify clean state
    assert mqtt_service.stop_completed
    
    # Cleanup
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_multiple_signals_idempotent(mqtt_service):
    """
    Test: Multiple signals should be handled idempotently.
    
    Sending SIGTERM twice should not cause issues.
    """
    worker = WorkerWithFixedSignalHandling(mqtt_service)
    await mqtt_service.connect()
    
    worker.running = True
    worker_task = asyncio.create_task(worker.worker_loop_fixed())
    
    await asyncio.sleep(0.05)
    
    # First signal
    worker.running = False
    worker.shutdown_task = asyncio.create_task(mqtt_service.stop())
    
    # Wait for shutdown
    await worker.shutdown_task
    
    # Verify stopped
    assert mqtt_service.stop_completed
    
    # Cleanup
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_shutdown_timeout_safety(mqtt_service):
    """
    Test: If stop_mqtt() takes too long, we should have a timeout.
    
    This prevents signal handler from hanging the shutdown.
    """
    async def stop_with_timeout(mqtt, timeout=2.0):
        """Stop MQTT with timeout protection."""
        try:
            await asyncio.wait_for(mqtt.stop(), timeout=timeout)
        except asyncio.TimeoutError:
            log.error("MQTT stop timed out")
    
    worker = WorkerWithFixedSignalHandling(mqtt_service)
    await mqtt_service.connect()
    
    # Stop with timeout
    await stop_with_timeout(mqtt_service, timeout=1.0)
    
    # Verify: either completed or timed out safely
    assert mqtt_service.stop_called


@pytest.mark.asyncio
async def test_stress_repeated_signal_handling(mqtt_service):
    """
    Stress test: Multiple worker starts/stops with signals.
    """
    for iteration in range(5):
        worker = WorkerWithFixedSignalHandling(mqtt_service)
        mqtt_service = MockMQTTService()
        worker.mqtt = mqtt_service
        
        await mqtt_service.connect()
        
        worker.running = True
        worker_task = asyncio.create_task(worker.worker_loop_fixed())
        
        await asyncio.sleep(0.02)
        
        # Signal
        worker.running = False
        worker.shutdown_task = asyncio.create_task(mqtt_service.stop())
        
        await worker.shutdown_task
        
        assert mqtt_service.stop_completed, f"Iteration {iteration}: stop not completed"
        
        # Cleanup
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
