#!/usr/bin/env python3
"""Tests for orphaned job detection and requeue functionality.

Tests cover:
- Unit tests for detecting stale jobs
- Integration tests for orphaned job requeue
- Concurrency tests with multiple workers
- Edge cases for legitimately slow jobs
"""

import asyncio
import logging
import os
import sqlite3
import tempfile
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add parent directory to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from store import (
    _connect, init_db, create_job, claim_next_job, 
    detect_orphaned_jobs, requeue_orphaned_job, requeue_stale,
    complete_job, get_job, _db_path
)
from metrics import get_metrics, reset_metrics

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(name)s: %(message)s",
)

log = logging.getLogger(__name__)


@pytest.fixture
def test_db():
    """Create a temporary test database."""
    # Use temp database for testing
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db_path = os.path.join(tmpdir, "test_jobs.db")
        os.environ["DEEP_THINK_DB"] = test_db_path
        
        # Initialize the test database
        init_db()
        
        yield test_db_path
        
        # Cleanup
        if os.path.exists(test_db_path):
            os.remove(test_db_path)


@pytest.fixture
def reset_metrics_fixture():
    """Reset metrics before each test."""
    reset_metrics()
    yield
    reset_metrics()


class TestOrphanDetection:
    """Unit tests for orphan job detection."""

    def test_detect_fresh_jobs_are_not_orphaned(self, test_db, reset_metrics_fixture):
        """Fresh jobs should not be detected as orphaned."""
        # Create and claim a job
        job_id = create_job(
            question="test question",
            passes=3,
            provider="test",
            model_summary="test model"
        )
        
        job = claim_next_job("worker-1")
        assert job is not None
        assert job["job_id"] == job_id
        assert job["status"] == "running"
        
        # Should not be detected as orphaned (just claimed)
        orphans = detect_orphaned_jobs(stale_after_minutes=5)
        assert len(orphans) == 0

    def test_detect_stale_jobs(self, test_db, reset_metrics_fixture):
        """Jobs stuck > threshold should be detected as orphaned."""
        # Create a job
        job_id = create_job(
            question="test question",
            passes=3,
            provider="test",
            model_summary="test model"
        )
        
        # Manually set it to running with old timestamp
        conn = _connect()
        try:
            stale_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            conn.execute(
                "UPDATE thinking_jobs SET status='running', started_at=?, claimed_at=?, claimed_by=? WHERE job_id=?",
                (stale_time, stale_time, "dead-worker", job_id)
            )
            conn.commit()
        finally:
            conn.close()
        
        # Should be detected as orphaned (>5 min)
        orphans = detect_orphaned_jobs(stale_after_minutes=5)
        assert len(orphans) == 1
        assert orphans[0]["job_id"] == job_id
        assert orphans[0]["claimed_by"] == "dead-worker"

    def test_detect_respects_timeout_threshold(self, test_db, reset_metrics_fixture):
        """Jobs within threshold should not be detected."""
        # Create a job
        job_id = create_job(
            question="test question",
            passes=3,
            provider="test",
            model_summary="test model"
        )
        
        # Set to running with recent timestamp (3 minutes old)
        conn = _connect()
        try:
            recent_time = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
            conn.execute(
                "UPDATE thinking_jobs SET status='running', started_at=?, claimed_at=?, claimed_by=? WHERE job_id=?",
                (recent_time, recent_time, "active-worker", job_id)
            )
            conn.commit()
        finally:
            conn.close()
        
        # Should NOT be detected as orphaned (3 min < 5 min threshold)
        orphans = detect_orphaned_jobs(stale_after_minutes=5)
        assert len(orphans) == 0

    def test_requeue_orphaned_job(self, test_db, reset_metrics_fixture):
        """Requeue should reset job status to pending."""
        # Create and claim a job
        job_id = create_job(
            question="test question",
            passes=3,
            provider="test",
            model_summary="test model"
        )
        
        job = claim_next_job("worker-1")
        assert job["status"] == "running"
        assert job["claimed_by"] == "worker-1"
        
        # Requeue the job
        success = requeue_orphaned_job(job_id, "test_timeout")
        assert success is True
        
        # Verify job state
        job = get_job(job_id)
        assert job["status"] == "pending"
        assert job["started_at"] is None
        assert job["claimed_by"] is None
        assert job["claimed_at"] is None

    def test_requeue_nonexistent_job(self, test_db, reset_metrics_fixture):
        """Requeue non-existent job should return False."""
        success = requeue_orphaned_job("nonexistent-job-id", "test")
        assert success is False

    def test_requeue_non_running_job(self, test_db, reset_metrics_fixture):
        """Requeue non-running job should return False."""
        # Create a job that stays queued
        job_id = create_job(
            question="test question",
            passes=3,
            provider="test",
            model_summary="test model"
        )
        
        # Try to requeue queued job
        success = requeue_orphaned_job(job_id, "test")
        assert success is False
        
        # Verify job still queued
        job = get_job(job_id)
        assert job["status"] == "queued"

    def test_multiple_orphaned_jobs(self, test_db, reset_metrics_fixture):
        """Should detect multiple orphaned jobs."""
        # Create and orphan 3 jobs
        job_ids = []
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        
        for i in range(3):
            job_id = create_job(
                question=f"test question {i}",
                passes=3,
                provider="test",
                model_summary="test model"
            )
            job_ids.append(job_id)
            
            conn = _connect()
            try:
                conn.execute(
                    "UPDATE thinking_jobs SET status='running', started_at=?, claimed_at=?, claimed_by=? WHERE job_id=?",
                    (stale_time, stale_time, f"dead-worker-{i}", job_id)
                )
                conn.commit()
            finally:
                conn.close()
        
        # Detect orphans
        orphans = detect_orphaned_jobs(stale_after_minutes=5)
        assert len(orphans) == 3
        detected_ids = {o["job_id"] for o in orphans}
        assert detected_ids == set(job_ids)


class TestOrphanRequeue:
    """Integration tests for orphaned job requeue workflow."""

    def test_orphan_requeue_workflow(self, test_db, reset_metrics_fixture):
        """Test complete orphan detection and requeue workflow."""
        m = get_metrics()
        
        # Create multiple jobs
        job_ids = [
            create_job(
                question=f"question {i}",
                passes=3,
                provider="test",
                model_summary="test model"
            )
            for i in range(3)
        ]
        
        # Claim 2 jobs (simulate active workers)
        active_job = claim_next_job("worker-1")
        inactive_job = claim_next_job("worker-2")
        queued_job_id = job_ids[2]
        
        # Make one job stale
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        conn = _connect()
        try:
            conn.execute(
                "UPDATE thinking_jobs SET started_at=?, claimed_at=? WHERE job_id=?",
                (stale_time, stale_time, inactive_job["job_id"])
            )
            conn.commit()
        finally:
            conn.close()
        
        # Detect orphans
        orphans = detect_orphaned_jobs(stale_after_minutes=5)
        assert len(orphans) == 1
        assert orphans[0]["job_id"] == inactive_job["job_id"]
        
        # Requeue orphans
        for orphan in orphans:
            requeue_orphaned_job(orphan["job_id"], "timeout")
        
        # Verify states
        active = get_job(active_job["job_id"])
        inactive = get_job(inactive_job["job_id"])
        queued = get_job(queued_job_id)
        
        assert active["status"] == "running"  # Still running
        assert inactive["status"] == "pending"  # Requeued
        assert queued["status"] == "queued"  # Still queued

    def test_requeue_stale_at_startup(self, test_db, reset_metrics_fixture):
        """Test requeue_stale on startup (long timeout)."""
        # Create and orphan a job with very old timestamp
        job_id = create_job(
            question="test question",
            passes=3,
            provider="test",
            model_summary="test model"
        )
        
        # Set to running with very old timestamp (older than 120 min default)
        very_old = (datetime.now(timezone.utc) - timedelta(minutes=150)).isoformat()
        conn = _connect()
        try:
            conn.execute(
                "UPDATE thinking_jobs SET status='running', started_at=? WHERE job_id=?",
                (very_old, job_id)
            )
            conn.commit()
        finally:
            conn.close()
        
        # Startup recovery (120 min threshold)
        count = requeue_stale(stale_after_minutes=120)
        assert count == 1
        
        job = get_job(job_id)
        assert job["status"] == "queued"
        assert job["started_at"] is None


class TestConcurrency:
    """Concurrency tests for multiple workers."""

    def test_multiple_workers_claim_different_jobs(self, test_db, reset_metrics_fixture):
        """Multiple workers should claim different jobs."""
        # Create jobs
        job_ids = [
            create_job(
                question=f"question {i}",
                passes=3,
                provider="test",
                model_summary="test model"
            )
            for i in range(3)
        ]
        
        # Multiple workers claim jobs
        jobs = []
        for i in range(3):
            job = claim_next_job(f"worker-{i}")
            assert job is not None
            jobs.append(job)
        
        # All should have different job IDs
        claimed_ids = {j["job_id"] for j in jobs}
        assert len(claimed_ids) == 3
        assert claimed_ids == set(job_ids)
        
        # All should have claimed_by set correctly
        worker_claims = {j["claimed_by"] for j in jobs}
        assert worker_claims == {"worker-0", "worker-1", "worker-2"}

    def test_orphan_detection_with_mixed_states(self, test_db, reset_metrics_fixture):
        """Detect orphans only among running jobs with different ages."""
        # Create jobs with different states
        job_ids = []
        
        # Running but recent (should not be orphaned)
        jid = create_job("q1", 3, "test", "model")
        job_ids.append(jid)
        recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        conn = _connect()
        try:
            conn.execute(
                "UPDATE thinking_jobs SET status='running', claimed_at=? WHERE job_id=?",
                (recent, jid)
            )
            conn.commit()
        finally:
            conn.close()
        
        # Running but stale (should be orphaned)
        jid = create_job("q2", 3, "test", "model")
        job_ids.append(jid)
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        conn = _connect()
        try:
            conn.execute(
                "UPDATE thinking_jobs SET status='running', claimed_at=? WHERE job_id=?",
                (stale, jid)
            )
            conn.commit()
        finally:
            conn.close()
        
        # Completed (should not be orphaned)
        jid = create_job("q3", 3, "test", "model")
        job_ids.append(jid)
        conn = _connect()
        try:
            conn.execute(
                "UPDATE thinking_jobs SET status='complete', result=? WHERE job_id=?",
                ("result", jid)
            )
            conn.commit()
        finally:
            conn.close()
        
        # Queued (should not be orphaned)
        jid = create_job("q4", 3, "test", "model")
        job_ids.append(jid)
        
        # Only stale running job should be detected
        orphans = detect_orphaned_jobs(stale_after_minutes=5)
        assert len(orphans) == 1
        assert orphans[0]["job_id"] == job_ids[1]


class TestEdgeCases:
    """Edge case tests."""

    def test_legitimately_slow_job_not_requeued(self, test_db, reset_metrics_fixture):
        """A legitimately running job should not be requeued."""
        # Create and claim a job
        job_id = create_job(
            question="slow job",
            passes=10,
            provider="test",
            model_summary="test model"
        )
        
        job = claim_next_job("worker-1")
        assert job["status"] == "running"
        
        # Set to running 3 minutes ago (within 5-minute threshold)
        recent = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        conn = _connect()
        try:
            conn.execute(
                "UPDATE thinking_jobs SET claimed_at=? WHERE job_id=?",
                (recent, job_id)
            )
            conn.commit()
        finally:
            conn.close()
        
        # Should not be detected as orphaned
        orphans = detect_orphaned_jobs(stale_after_minutes=5)
        assert len(orphans) == 0

    def test_configurable_timeout(self, test_db, reset_metrics_fixture):
        """Timeout threshold should be configurable."""
        job_id = create_job(
            question="test",
            passes=3,
            provider="test",
            model_summary="model"
        )
        
        # Set to running 7 minutes ago
        old = (datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat()
        conn = _connect()
        try:
            conn.execute(
                "UPDATE thinking_jobs SET status='running', claimed_at=? WHERE job_id=?",
                (old, job_id)
            )
            conn.commit()
        finally:
            conn.close()
        
        # With 5-min threshold: orphaned
        orphans = detect_orphaned_jobs(stale_after_minutes=5)
        assert len(orphans) == 1
        
        # With 10-min threshold: not orphaned
        orphans = detect_orphaned_jobs(stale_after_minutes=10)
        assert len(orphans) == 0

    def test_null_claimed_fields_on_requeue(self, test_db, reset_metrics_fixture):
        """Requeue should clear all claimed fields."""
        job_id = create_job("test", 3, "test", "model")
        
        job = claim_next_job("worker-1")
        assert job["claimed_by"] == "worker-1"
        assert job["claimed_at"] is not None
        
        requeue_orphaned_job(job_id)
        
        job = get_job(job_id)
        assert job["claimed_by"] is None
        assert job["claimed_at"] is None
        assert job["status"] == "pending"




if __name__ == "__main__":
    pytest.main([__file__, "-v"])
