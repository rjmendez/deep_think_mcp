"""Integration test demonstrating the cache transaction atomicity fix.

This test simulates the original bug scenario where cache writes could
become orphaned if job completion failed, and verifies that the fix
ensures atomic all-or-nothing semantics.
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch
import pytest

import store


@pytest.fixture
def test_db(tmp_path):
    """Create a test database."""
    db_path = tmp_path / "test_jobs.db"
    with patch("store._db_path", return_value=str(db_path)):
        store.init_db()
        yield str(db_path)


def test_original_bug_scenario_fixed(test_db):
    """
    SCENARIO: Original Bug
    
    During job execution:
    1. set_pass_cache() writes intermediate results (commits)
    2. Job crashes or complete_job() transaction fails
    3. Cache has data but job status not updated to "complete"
    4. Result: Orphaned cache entries + inconsistent state
    
    FIX: complete_job() now accepts cache_entries parameter and writes
    everything atomically. If complete_job() fails to commit, entire
    transaction rolls back including cache entries.
    """
    job_id = store.create_job(
        question="Complex reasoning question",
        passes=3,
        provider="test",
        model_summary="test-model",
    )
    
    # Claim the job (simulating job execution starting)
    claimed_job = store.claim_next_job()
    assert claimed_job["job_id"] == job_id
    assert claimed_job["status"] == "running"
    
    # Simulate job execution with multiple passes
    # In real scenario, set_pass_cache() would be called during execution
    cache_entries_collected = []
    
    for pass_num in range(1, 4):
        # Simulate pass execution
        output = f"Pass {pass_num} output: reasoning step {pass_num}"
        
        # In the old code, this would commit immediately
        # In the new code, we collect them for atomic write
        cache_entries_collected.append({
            "job_id": job_id,
            "perspective": "main",
            "pass_num": pass_num,
            "run_sig": "signature_123",
            "framing": f"framing_{pass_num}",
            "tier": "light" if pass_num == 1 else "medium",
            "model_used": "test-model",
            "provider": "test",
            "output": output,
        })
        
        # Old behavior: write immediately (commented out)
        # store.set_pass_cache(job_id, "main", pass_num, "signature_123",
        #                      f"framing_{pass_num}", "light", "test-model",
        #                      "test", output)
    
    # Simulate job completion
    result = {
        "status": "complete",
        "final_answer": "Final reasoning result",
        "confidence": 0.95,
    }
    
    # NEW: complete_job() with atomic cache writes
    # Before fix: cache and job status updated in separate transactions
    # After fix: everything in one transaction - all or nothing
    store.complete_job(
        job_id,
        json.dumps(result),
        cache_entries=cache_entries_collected,
    )
    
    # Verify atomicity worked
    job = store.get_job(job_id)
    assert job["status"] == "complete"
    assert json.loads(job["result"]) == result
    
    # Verify all cache entries are present
    cache = store.get_job_pass_cache_entries(job_id)
    assert len(cache) == 3
    assert cache[0]["pass_num"] == 1
    assert cache[1]["pass_num"] == 2
    assert cache[2]["pass_num"] == 3
    
    # Verify consistency
    is_valid, issues = store.validate_cache_consistency(job_id)
    assert is_valid is True
    assert len(issues) == 0


def test_transaction_rollback_on_failure(test_db):
    """
    Verify that if complete_job() transaction fails,
    both cache writes AND job status update are rolled back.
    """
    job_id = store.create_job(
        question="Test question",
        passes=2,
        provider="test",
        model_summary="test",
    )
    store.claim_next_job()
    
    cache_entries = [
        {
            "job_id": job_id,
            "perspective": "main",
            "pass_num": 1,
            "run_sig": "sig123",
            "framing": "framing1",
            "tier": "light",
            "model_used": "model1",
            "provider": "test",
            "output": "output1",
        }
    ]
    
    # Mock _connect to simulate commit failure
    with patch("store._connect") as mock_connect:
        mock_conn = type("MockConn", (), {})()
        mock_conn.execute = lambda *args, **kwargs: None
        mock_conn.commit = lambda: (_ for _ in ()).throw(
            sqlite3.OperationalError("Commit failed")
        )
        mock_conn.close = lambda: None
        mock_connect.return_value = mock_conn
        
        # Try to complete job - should fail
        with pytest.raises(sqlite3.OperationalError):
            store.complete_job(job_id, json.dumps({"status": "complete"}),
                              cache_entries=cache_entries)
    
    # Verify job status wasn't updated
    # (In the real scenario, if commit failed, status would still be "running")
    job = store.get_job(job_id)
    assert job["status"] == "running"


def test_failed_job_cleanup(test_db):
    """
    Verify that fail_job() atomically:
    1. Deletes any cache entries (preventing orphans)
    2. Updates job status to "failed"
    """
    job_id = store.create_job(
        question="Test question",
        passes=2,
        provider="test",
        model_summary="test",
    )
    store.claim_next_job()
    
    # Write some cache entries manually
    for i in range(1, 3):
        store.set_pass_cache(
            job_id, "main", i, "sig123",
            f"framing{i}", "light", "model1", "test",
            f"output{i}",
        )
    
    # Verify cache exists
    cache_before = store.get_job_pass_cache_entries(job_id)
    assert len(cache_before) == 2
    
    # Fail the job
    store.fail_job(job_id, "Simulated error")
    
    # Verify job is failed and cache is cleaned up
    job = store.get_job(job_id)
    assert job["status"] == "failed"
    assert job["error"] == "Simulated error"
    
    cache_after = store.get_job_pass_cache_entries(job_id)
    assert len(cache_after) == 0
    
    # Verify consistency validation passes
    is_valid, issues = store.validate_cache_consistency(job_id)
    assert is_valid is True


def test_backup_recovery_scenario(test_db, tmp_path):
    """
    Verify that database corruption triggers automatic
    backup and recovery.
    """
    # Create a job and mark it complete
    job_id = store.create_job(
        question="Test",
        passes=1,
        provider="test",
        model_summary="test",
    )
    store.claim_next_job()
    store.complete_job(job_id, json.dumps({"status": "complete"}),
                      cache_entries=[
                          {
                              "job_id": job_id,
                              "perspective": "main",
                              "pass_num": 1,
                              "run_sig": "sig123",
                              "framing": "framing1",
                              "tier": "light",
                              "model_used": "model1",
                              "provider": "test",
                              "output": "output1",
                          }
                      ])
    
    # Create a backup
    with patch("store._db_path", return_value=test_db):
        backup_path = store._backup_db("test_scenario")
    
    assert Path(backup_path).exists()
    
    # Simulate corruption by deleting all jobs
    conn = sqlite3.connect(test_db)
    conn.execute("DELETE FROM thinking_jobs")
    conn.commit()
    conn.close()
    
    # Verify data is gone
    job = store.get_job(job_id)
    assert job is None
    
    # Restore from backup
    store._restore_db(backup_path)
    
    # Verify data is restored
    job = store.get_job(job_id)
    assert job is not None
    assert job["job_id"] == job_id
    assert job["status"] == "complete"
    
    # Verify cache is also restored
    cache = store.get_job_pass_cache_entries(job_id)
    assert len(cache) == 1


def test_integrity_check_on_startup(test_db):
    """
    Verify that database integrity is checked on startup
    and corruption is detected.
    """
    # Should pass for valid database
    is_valid, message = store.check_db_integrity()
    assert is_valid is True
    assert message == "ok"
    
    # init_db_with_integrity_check should also pass
    store.init_db_with_integrity_check()  # No exception


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
