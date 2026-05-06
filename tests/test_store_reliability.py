#!/usr/bin/env python3
"""Reliability tests for SQLite job persistence."""

import os
import tempfile
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from store import _connect, init_db, create_job, complete_job, get_job


@pytest.fixture
def test_db():
    """Create a temporary database for persistence tests."""
    old_db = os.environ.get("DEEP_THINK_DB")
    old_limit = os.environ.get("DEEP_THINK_MAX_RESULT_BYTES")

    with tempfile.TemporaryDirectory() as tmpdir:
        test_db_path = os.path.join(tmpdir, "test_jobs.db")
        os.environ["DEEP_THINK_DB"] = test_db_path
        init_db()
        yield test_db_path

    if old_db is None:
        os.environ.pop("DEEP_THINK_DB", None)
    else:
        os.environ["DEEP_THINK_DB"] = old_db

    if old_limit is None:
        os.environ.pop("DEEP_THINK_MAX_RESULT_BYTES", None)
    else:
        os.environ["DEEP_THINK_MAX_RESULT_BYTES"] = old_limit


def test_complete_job_rejects_oversize_result(test_db):
    """Oversize results should fail before they are written."""
    os.environ["DEEP_THINK_MAX_RESULT_BYTES"] = "32"
    job_id = create_job("question", passes=1, provider="ollama", model_summary="test-model")

    with pytest.raises(ValueError, match="Result payload too large"):
        complete_job(job_id, "x" * 64)

    job = get_job(job_id)
    assert job["status"] == "queued"
    assert job["result"] is None


def test_queue_indexes_created(test_db):
    """Queue-oriented composite indexes should exist after init."""
    conn = _connect()
    try:
        rows = conn.execute("PRAGMA index_list('thinking_jobs')").fetchall()
    finally:
        conn.close()

    index_names = {row[1] for row in rows}
    assert "idx_thinking_jobs_status_created" in index_names
    assert "idx_thinking_jobs_status_claimed" in index_names
    assert "idx_thinking_jobs_status_completed" in index_names
