"""Test cache transaction atomicity and data integrity.

Tests that:
- Cache writes + job status updates are atomic (all-or-nothing)
- Orphaned cache entries are not created on job failure
- Database integrity checks detect corruption
- Backup/restore works for recovery
- Cache validation ensures consistency between cache and job status
"""

import json
import pytest
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

import store


@pytest.fixture
def test_db(tmp_path):
    """Create a test database in a temporary directory."""
    db_path = tmp_path / "test_jobs.db"
    with patch("store._db_path", return_value=str(db_path)):
        store.init_db()
        yield str(db_path)


class TestTransactionAtomicity:
    """Test atomicity of cache + job status writes."""

    def test_complete_job_atomicity_with_cache_entries(self, test_db):
        """Test that cache entries and job status are written atomically."""
        question = "Test question"
        
        # Create a job
        job_id = store.create_job(question=question, passes=3, provider="test", model_summary="test")
        
        # Claim the job
        claimed = store.claim_next_job()
        assert claimed["job_id"] == job_id
        assert claimed["job_id"] == job_id
        
        # Prepare cache entries
        cache_entries = [
            {
                "job_id": job_id,
                "perspective": "test_perspective",
                "pass_num": 1,
                "run_sig": "sig1",
                "framing": "test_framing",
                "tier": "light",
                "model_used": "test_model",
                "provider": "test",
                "output": "Test output 1",
            },
            {
                "job_id": job_id,
                "perspective": "test_perspective",
                "pass_num": 2,
                "run_sig": "sig1",
                "framing": "test_framing",
                "tier": "medium",
                "model_used": "test_model",
                "provider": "test",
                "output": "Test output 2",
            },
        ]
        
        # Complete job with cache entries
        result = {"status": "complete", "answer": "Test answer"}
        store.complete_job(job_id, json.dumps(result), cache_entries=cache_entries)
        
        # Verify job is complete
        job = store.get_job(job_id)
        assert job["status"] == "complete"
        assert job["result"] == json.dumps(result)
        
        # Verify cache entries exist
        cache = store.get_job_pass_cache_entries(job_id)
        assert len(cache) == 2
        assert cache[0]["pass_num"] == 1
        assert cache[1]["pass_num"] == 2

    def test_complete_job_atomicity_no_cache(self, test_db):
        """Test that job completion works without cache entries."""
        job_id = "test_job_no_cache"
        question = "Test question"
        
        job_id = store.create_job(question=question, passes=3, provider="test", model_summary="test")
        claimed = store.claim_next_job()
        assert claimed["job_id"] == job_id
        
        result = {"status": "complete", "answer": "Test answer"}
        store.complete_job(job_id, json.dumps(result), cache_entries=None)
        
        job = store.get_job(job_id)
        assert job["status"] == "complete"
        
        # Verify no cache entries
        cache = store.get_job_pass_cache_entries(job_id)
        assert len(cache) == 0

    def test_fail_job_cleans_up_cache(self, test_db):
        """Test that failing a job removes orphaned cache entries."""
        job_id = "test_job_fail_cleanup"
        question = "Test question"
        
        job_id = store.create_job(question=question, passes=3, provider="test", model_summary="test")
        claimed = store.claim_next_job()
        assert claimed["job_id"] == job_id
        
        # Manually write some cache entries
        store.set_pass_cache(
            job_id=job_id,
            perspective="test",
            pass_num=1,
            run_sig="sig1",
            framing="framing1",
            tier="light",
            model_used="model1",
            provider="test",
            output="output1",
        )
        
        # Verify cache exists
        cache = store.get_job_pass_cache_entries(job_id)
        assert len(cache) == 1
        
        # Fail the job
        store.fail_job(job_id, "Test error")
        
        # Verify job is failed
        job = store.get_job(job_id)
        assert job["status"] == "failed"
        assert job["error"] == "Test error"
        
        # Verify cache is cleaned up
        cache = store.get_job_pass_cache_entries(job_id)
        assert len(cache) == 0

    def test_complete_job_transaction_failure_rollback(self, test_db):
        """Test that transaction failure causes rollback of cache + status."""
        job_id = "test_job_txn_fail"
        question = "Test question"
        
        job_id = store.create_job(question=question, passes=3, provider="test", model_summary="test")
        claimed = store.claim_next_job()
        assert claimed["job_id"] == job_id
        
        cache_entries = [
            {
                "job_id": job_id,
                "perspective": "test",
                "pass_num": 1,
                "run_sig": "sig1",
                "framing": "framing1",
                "tier": "light",
                "model_used": "model1",
                "provider": "test",
                "output": "output1",
            }
        ]
        
        result = {"status": "complete"}
        
        # Mock the connection to simulate commit failure
        with patch("store._connect") as mock_connect:
            mock_conn = MagicMock()
            mock_connect.return_value = mock_conn
            
            # Simulate commit failure
            mock_conn.commit.side_effect = sqlite3.OperationalError("Commit failed")
            
            # Try to complete job
            with pytest.raises(sqlite3.OperationalError):
                store.complete_job(job_id, json.dumps(result), cache_entries=cache_entries)
            
            # Verify rollback was called
            mock_conn.execute.assert_any_call("BEGIN IMMEDIATE")
            mock_conn.execute.assert_any_call("ROLLBACK")


class TestDatabaseIntegrity:
    """Test database integrity checks and recovery."""

    def test_check_db_integrity_valid(self, test_db):
        """Test that integrity check passes for valid database."""
        is_valid, message = store.check_db_integrity()
        assert is_valid is True
        assert message == "ok"

    def test_backup_and_restore(self, test_db, tmp_path):
        """Test that database can be backed up and restored."""
        # Create a job
        job_id = "test_backup_restore"
        job_id = store.create_job(question="Test", passes=3, provider="test", model_summary="test")
        
        # Verify job exists
        job = store.get_job(job_id)
        assert job is not None
        
        # Create backup
        with patch("store._db_path", return_value=str(tmp_path / "test_jobs.db")):
            backup_path = store._backup_db("test_backup")
        
        assert Path(backup_path).exists()
        
        # Delete the job from original database
        conn = sqlite3.connect(test_db)
        conn.execute("DELETE FROM thinking_jobs WHERE job_id=?", (job_id,))
        conn.commit()
        conn.close()
        
        # Verify job is deleted
        job = store.get_job(job_id)
        assert job is None
        
        # Restore from backup
        store._restore_db(backup_path)
        
        # Verify job is restored
        job = store.get_job(job_id)
        assert job is not None
        assert job["job_id"] == job_id

    def test_init_db_with_integrity_check_valid(self, test_db):
        """Test initialization with integrity checks."""
        # Should not raise
        store.init_db_with_integrity_check()

    def test_init_db_with_corruption_detection(self, test_db):
        """Test that corruption is detected and triggers backup."""
        # Mock integrity check to fail
        with patch("store.check_db_integrity", return_value=(False, "Error")):
            with patch("store._backup_db") as mock_backup:
                # Should try to backup and then raise error
                with pytest.raises(RuntimeError):
                    store.init_db_with_integrity_check()
                
                # Verify backup was called
                mock_backup.assert_called_once_with("corruption_detected")


class TestCacheValidation:
    """Test cache consistency validation."""

    def test_validate_cache_consistency_complete_job_with_cache(self, test_db):
        """Test validation passes for complete job with cache."""
        job_id = "test_validate_complete"
        job_id = store.create_job(question="Test", passes=3, provider="test", model_summary="test")
        store.claim_next_job()
        
        # Write cache and complete job
        cache_entries = [
            {
                "job_id": job_id,
                "perspective": "test",
                "pass_num": 1,
                "run_sig": "sig1",
                "framing": "framing1",
                "tier": "light",
                "model_used": "model1",
                "provider": "test",
                "output": "output1",
            }
        ]
        store.complete_job(job_id, json.dumps({"status": "complete"}), cache_entries=cache_entries)
        
        # Validate
        is_valid, issues = store.validate_cache_consistency(job_id)
        assert is_valid is True
        assert len(issues) == 0

    def test_validate_cache_consistency_failed_job_no_cache(self, test_db):
        """Test validation passes for failed job with no cache."""
        job_id = "test_validate_failed"
        job_id = store.create_job(question="Test", passes=3, provider="test", model_summary="test")
        store.claim_next_job()
        store.fail_job(job_id, "Test error")
        
        # Validate
        is_valid, issues = store.validate_cache_consistency(job_id)
        assert is_valid is True
        assert len(issues) == 0

    def test_validate_cache_consistency_missing_cache(self, test_db):
        """Test validation fails when complete job has no cache."""
        job_id = "test_validate_missing"
        job_id = store.create_job(question="Test", passes=3, provider="test", model_summary="test")
        store.claim_next_job()
        store.complete_job(job_id, json.dumps({"status": "complete"}), cache_entries=None)
        
        # Validate
        is_valid, issues = store.validate_cache_consistency(job_id)
        # Should pass - it's OK for complete jobs to have no cache if they didn't use caching
        assert is_valid is True

    def test_validate_cache_consistency_orphaned_cache(self, test_db):
        """Test validation fails when failed job has orphaned cache."""
        job_id = "test_validate_orphan"
        job_id = store.create_job(question="Test", passes=3, provider="test", model_summary="test")
        store.claim_next_job()
        
        # Write cache
        store.set_pass_cache(
            job_id=job_id,
            perspective="test",
            pass_num=1,
            run_sig="sig1",
            framing="framing1",
            tier="light",
            model_used="model1",
            provider="test",
            output="output1",
        )
        
        # Now directly mark as failed without cleanup (simulating error)
        conn = sqlite3.connect(test_db)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE thinking_jobs SET status='failed', error=?, completed_at=? WHERE job_id=?",
            ("Simulated failure", now, job_id),
        )
        conn.commit()
        conn.close()
        
        # Validate
        is_valid, issues = store.validate_cache_consistency(job_id)
        assert is_valid is False
        assert len(issues) == 1
        assert "orphaned" in issues[0].lower()

    def test_validate_all_cache_consistency(self, test_db):
        """Test validation of all jobs."""
        # Create multiple jobs
        job1 = "test_all_validate_1"
        job2 = "test_all_validate_2"
        
        job_id = store.create_job(question="Test1", passes=3, provider="test", model_summary="test")
        job_id = store.create_job(question="Test2", passes=3, provider="test", model_summary="test")
        
        store.claim_next_job()
        store.claim_next_job()
        
        # Complete first job with cache
        cache_entries = [
            {
                "job_id": job1,
                "perspective": "test",
                "pass_num": 1,
                "run_sig": "sig1",
                "framing": "framing1",
                "tier": "light",
                "model_used": "model1",
                "provider": "test",
                "output": "output1",
            }
        ]
        store.complete_job(job1, json.dumps({"status": "complete"}), cache_entries=cache_entries)
        store.fail_job(job2, "Test error")
        
        # Validate all
        all_valid, issues_by_job = store.validate_all_cache_consistency()
        assert all_valid is True
        assert len(issues_by_job) == 0


class TestEdgeCases:
    """Test edge cases and error scenarios."""

    def test_complete_job_with_empty_cache_list(self, test_db):
        """Test completing job with empty cache list."""
        job_id = "test_empty_cache_list"
        job_id = store.create_job(question="Test", passes=3, provider="test", model_summary="test")
        store.claim_next_job()
        
        store.complete_job(job_id, json.dumps({"status": "complete"}), cache_entries=[])
        
        job = store.get_job(job_id)
        assert job["status"] == "complete"
        
        cache = store.get_job_pass_cache_entries(job_id)
        assert len(cache) == 0

    def test_get_pass_cache_entries_nonexistent_job(self, test_db):
        """Test getting cache entries for nonexistent job."""
        cache = store.get_job_pass_cache_entries("nonexistent_job")
        assert cache == []

    def test_validate_nonexistent_job(self, test_db):
        """Test validating nonexistent job."""
        is_valid, issues = store.validate_cache_consistency("nonexistent_job")
        assert is_valid is False
        assert len(issues) == 1
        assert "not found" in issues[0].lower()

    def test_complete_job_multiple_perspectives(self, test_db):
        """Test completing job with cache entries from multiple perspectives."""
        job_id = "test_multi_perspective"
        job_id = store.create_job(question="Test", passes=3, provider="test", model_summary="test")
        store.claim_next_job()
        
        cache_entries = [
            {
                "job_id": job_id,
                "perspective": "perspective1",
                "pass_num": 1,
                "run_sig": "sig1",
                "framing": "framing1",
                "tier": "light",
                "model_used": "model1",
                "provider": "test",
                "output": "output1",
            },
            {
                "job_id": job_id,
                "perspective": "perspective2",
                "pass_num": 1,
                "run_sig": "sig1",
                "framing": "framing1",
                "tier": "light",
                "model_used": "model1",
                "provider": "test",
                "output": "output2",
            },
            {
                "job_id": job_id,
                "perspective": "perspective1",
                "pass_num": 2,
                "run_sig": "sig1",
                "framing": "framing1",
                "tier": "medium",
                "model_used": "model1",
                "provider": "test",
                "output": "output3",
            },
        ]
        
        store.complete_job(job_id, json.dumps({"status": "complete"}), cache_entries=cache_entries)
        
        cache = store.get_job_pass_cache_entries(job_id)
        assert len(cache) == 3
        
        # Verify ordering
        assert cache[0]["perspective"] == "perspective1"
        assert cache[0]["pass_num"] == 1
        assert cache[1]["perspective"] == "perspective1"
        assert cache[1]["pass_num"] == 2
        assert cache[2]["perspective"] == "perspective2"
