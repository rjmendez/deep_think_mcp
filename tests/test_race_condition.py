"""Test race condition fixes for job claiming.

Tests for Bug 1: Race condition in claim_next_job()
Tests for Bug 2: Status mismatch between 'pending' and 'queued'
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from store import (
    claim_next_job,
    create_job,
    get_job,
    init_db,
    requeue_orphaned_job,
)


@pytest.fixture
def test_db():
    """Create a temporary test database with proper cleanup."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_race.db"
        old_db_path = os.environ.get("DEEP_THINK_DB")
        os.environ["DEEP_THINK_DB"] = str(db_path)
        try:
            init_db()
            yield db_path
        finally:
            # Restore old path or remove
            if old_db_path:
                os.environ["DEEP_THINK_DB"] = old_db_path
            elif "DEEP_THINK_DB" in os.environ:
                del os.environ["DEEP_THINK_DB"]


class TestRaceConditionFix:
    """Tests for Bug 1: Race condition in claim_next_job()."""
    
    def test_concurrent_claim_only_one_succeeds(self, test_db):
        """Test that only one worker can claim a job when claiming concurrently.
        
        This is a regression test for Bug 1 where the SELECT followed by UPDATE
        was not atomic, allowing two workers to both claim the same job.
        """
        # Create a single job
        job_id = create_job(
            question="test concurrent claim",
            passes=3,
            provider="test",
            model_summary="test model"
        )
        
        # Track which workers successfully claimed
        successful_claims = []
        claim_lock = threading.Lock()
        
        def attempt_claim(worker_id: str):
            """Attempt to claim the job."""
            result = claim_next_job(worker_id)
            with claim_lock:
                if result is not None:
                    successful_claims.append(worker_id)
        
        # Launch 10 workers simultaneously trying to claim the same job
        threads = []
        for i in range(10):
            worker_id = f"worker-{i}"
            thread = threading.Thread(target=attempt_claim, args=(worker_id,))
            threads.append(thread)
        
        # Start all threads at once
        for thread in threads:
            thread.start()
        
        # Wait for all to complete
        for thread in threads:
            thread.join()
        
        # CRITICAL: Only ONE worker should have successfully claimed the job
        assert len(successful_claims) == 1, \
            f"Expected exactly 1 successful claim, got {len(successful_claims)}: {successful_claims}"
        
        # Verify the job is marked as running by the one successful worker
        job = get_job(job_id)
        assert job["status"] == "running"
        assert job["claimed_by"] == successful_claims[0]
        assert job["claimed_at"] is not None
        assert job["started_at"] is not None
    
    def test_sequential_claims_after_requeue(self, test_db):
        """Test that after a job is requeued, a different worker can claim it."""
        # Create a job
        job_id = create_job(
            question="test sequential claim",
            passes=3,
            provider="test",
            model_summary="test model"
        )
        
        # Worker 1 claims the job
        job1 = claim_next_job("worker-1")
        assert job1 is not None
        assert job1["claimed_by"] == "worker-1"
        
        # Worker 2 tries to claim - should get None (no jobs available)
        job2 = claim_next_job("worker-2")
        assert job2 is None
        
        # Requeue the job (simulating orphan detection)
        success = requeue_orphaned_job(job_id)
        assert success is True
        
        # Now worker 2 should be able to claim it
        job3 = claim_next_job("worker-2")
        assert job3 is not None
        assert job3["job_id"] == job_id
        assert job3["claimed_by"] == "worker-2"
        assert job3["status"] == "running"


class TestStatusConsistencyFix:
    """Tests for Bug 2: Status mismatch between 'pending' and 'queued'."""
    
    def test_requeue_uses_queued_status(self, test_db):
        """Test that requeue_orphaned_job() sets status to 'queued', not 'pending'.
        
        This is a regression test for Bug 2 where requeue_orphaned_job() set
        status='pending' but claim_next_job() only looked for status='queued',
        causing requeued jobs to be stuck forever.
        """
        # Create and claim a job
        job_id = create_job(
            question="test requeue status",
            passes=3,
            provider="test",
            model_summary="test model"
        )
        
        job = claim_next_job("worker-1")
        assert job["status"] == "running"
        
        # Requeue the job
        success = requeue_orphaned_job(job_id)
        assert success is True
        
        # CRITICAL: Status must be 'queued', not 'pending'
        job = get_job(job_id)
        assert job["status"] == "queued", \
            f"Expected status='queued' after requeue, got '{job['status']}'"
    
    def test_requeued_job_can_be_claimed(self, test_db):
        """Test that a requeued job can actually be claimed by another worker.
        
        This is the end-to-end test for Bug 2 - verifying that the entire
        workflow works: claim -> requeue -> claim again.
        """
        # Create and claim a job
        job_id = create_job(
            question="test requeue and reclaim",
            passes=3,
            provider="test",
            model_summary="test model"
        )
        
        # Worker 1 claims it
        job1 = claim_next_job("worker-1")
        assert job1 is not None
        assert job1["job_id"] == job_id
        
        # Requeue the job (simulating timeout/crash)
        success = requeue_orphaned_job(job_id)
        assert success is True
        
        # CRITICAL: Worker 2 should be able to claim the requeued job
        job2 = claim_next_job("worker-2")
        assert job2 is not None, \
            "Requeued job could not be claimed - likely status='pending' instead of 'queued'"
        assert job2["job_id"] == job_id
        assert job2["claimed_by"] == "worker-2"
        assert job2["status"] == "running"
    
    def test_multiple_requeue_cycles(self, test_db):
        """Test that a job can be requeued and reclaimed multiple times."""
        job_id = create_job(
            question="test multiple requeues",
            passes=3,
            provider="test",
            model_summary="test model"
        )
        
        for i in range(3):
            worker_id = f"worker-{i}"
            
            # Claim the job
            job = claim_next_job(worker_id)
            assert job is not None
            assert job["job_id"] == job_id
            assert job["claimed_by"] == worker_id
            
            # Requeue it
            success = requeue_orphaned_job(job_id)
            assert success is True
            
            # Verify it's queued and can be claimed again
            job = get_job(job_id)
            assert job["status"] == "queued"


class TestStateTransitionValidation:
    """Tests for proper state transitions."""
    
    def test_claim_only_works_on_queued_jobs(self, test_db):
        """Test that claim_next_job() only claims jobs with status='queued'."""
        # Create a job
        job_id = create_job(
            question="test claim filter",
            passes=3,
            provider="test",
            model_summary="test model"
        )
        
        # Claim it
        job1 = claim_next_job("worker-1")
        assert job1 is not None
        
        # Try to claim again - should return None because job is 'running', not 'queued'
        job2 = claim_next_job("worker-2")
        assert job2 is None
        
        # Verify the first claim is still intact (wasn't overwritten)
        job = get_job(job_id)
        assert job["claimed_by"] == "worker-1"
        assert job["status"] == "running"
    
    def test_requeue_only_works_on_running_jobs(self, test_db):
        """Test that requeue_orphaned_job() only requeues jobs with status='running'."""
        # Create a job but don't claim it (status='queued')
        job_id = create_job(
            question="test requeue filter",
            passes=3,
            provider="test",
            model_summary="test model"
        )
        
        # Try to requeue a queued job - should return False
        success = requeue_orphaned_job(job_id)
        assert success is False
        
        # Job should still be queued
        job = get_job(job_id)
        assert job["status"] == "queued"
        
        # Now claim it
        claim_next_job("worker-1")
        
        # Now requeue should work
        success = requeue_orphaned_job(job_id)
        assert success is True
        
        job = get_job(job_id)
        assert job["status"] == "queued"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
