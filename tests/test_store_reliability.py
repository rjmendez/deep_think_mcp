#!/usr/bin/env python3
"""Reliability tests for SQLite job persistence."""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from store import (
    _connect,
    init_db,
    create_job,
    complete_job,
    get_job,
    prune_thinking_jobs,
    request_job_cancellation,
    claim_next_job,
    build_idempotency_request_hash,
    bind_idempotency_key,
    lookup_idempotent_job,
)


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


def test_create_job_persists_explicit_timeout(test_db):
    """Job-level timeout should persist for worker-side enforcement."""
    job_id = create_job(
        "question",
        passes=2,
        provider="ollama",
        model_summary="test-model",
        timeout_secs=987,
    )
    job = get_job(job_id)
    assert job["timeout_secs"] == 987


def test_prune_thinking_jobs_respects_terminal_row_limit(test_db):
    queued_id = create_job("queued", passes=1, provider="ollama", model_summary="q")
    running_id = create_job("running", passes=1, provider="ollama", model_summary="r")
    terminal_ids = [
        create_job("done-1", passes=1, provider="ollama", model_summary="d1"),
        create_job("done-2", passes=1, provider="ollama", model_summary="d2"),
        create_job("done-3", passes=1, provider="ollama", model_summary="d3"),
    ]

    now = datetime.now(timezone.utc)
    conn = _connect()
    try:
        conn.execute("UPDATE thinking_jobs SET status='queued' WHERE job_id=?", (queued_id,))
        conn.execute(
            "UPDATE thinking_jobs SET status='running', started_at=?, claimed_at=? WHERE job_id=?",
            (now.isoformat(), now.isoformat(), running_id),
        )
        for idx, job_id in enumerate(terminal_ids):
            ts = (now + timedelta(seconds=idx)).isoformat()
            conn.execute(
                "UPDATE thinking_jobs SET status='complete', completed_at=? WHERE job_id=?",
                (ts, job_id),
            )
        conn.commit()
    finally:
        conn.close()

    pruned = prune_thinking_jobs(max_rows=2, max_age_days=0)
    assert pruned == 1

    conn = _connect()
    try:
        remaining_terminal = conn.execute(
            "SELECT job_id FROM thinking_jobs WHERE status='complete' ORDER BY completed_at ASC"
        ).fetchall()
        remaining_non_terminal = conn.execute(
            "SELECT job_id, status FROM thinking_jobs WHERE status IN ('queued', 'running') ORDER BY job_id ASC"
        ).fetchall()
    finally:
        conn.close()

    assert [row["job_id"] for row in remaining_terminal] == terminal_ids[1:]
    assert {(row["job_id"], row["status"]) for row in remaining_non_terminal} == {
        (queued_id, "queued"),
        (running_id, "running"),
    }


def test_prune_thinking_jobs_respects_terminal_age_limit(test_db):
    old_job = create_job("old", passes=1, provider="ollama", model_summary="old")
    fresh_job = create_job("fresh", passes=1, provider="ollama", model_summary="fresh")

    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=45)).isoformat()
    fresh_ts = (now - timedelta(days=2)).isoformat()

    conn = _connect()
    try:
        conn.execute(
            "UPDATE thinking_jobs SET status='complete', completed_at=? WHERE job_id=?",
            (old_ts, old_job),
        )
        conn.execute(
            "UPDATE thinking_jobs SET status='failed', completed_at=? WHERE job_id=?",
            (fresh_ts, fresh_job),
        )
        conn.commit()
    finally:
        conn.close()

    pruned = prune_thinking_jobs(max_rows=0, max_age_days=30)
    assert pruned == 1

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT job_id, status FROM thinking_jobs ORDER BY job_id ASC"
        ).fetchall()
    finally:
        conn.close()

    assert [(row["job_id"], row["status"]) for row in rows] == [(fresh_job, "failed")]


def test_request_job_cancellation_transitions_queued_to_failed(test_db):
    job_id = create_job("cancel me", passes=1, provider="ollama", model_summary="model")
    result = request_job_cancellation(job_id, reason="user-request")
    assert result["status"] == "failed"
    assert result["terminal"] is True
    job = get_job(job_id)
    assert job["status"] == "failed"
    assert "cancelled: user-request" == job["error"]


def test_request_job_cancellation_marks_running_job_for_worker_cancel(test_db):
    job_id = create_job("cancel running", passes=1, provider="ollama", model_summary="model")
    claimed = claim_next_job("worker-1")
    assert claimed is not None and claimed["job_id"] == job_id
    result = request_job_cancellation(job_id, reason="stop-now")
    assert result["status"] == "running"
    assert result["cancel_requested"] is True
    job = get_job(job_id)
    assert job["status"] == "running"
    assert job["cancel_reason"] == "stop-now"
    assert job["cancel_requested_at"] is not None


def test_idempotency_key_maps_to_existing_job(test_db):
    payload = {"endpoint": "deep_think_async", "question": "hello", "passes": 3}
    request_hash = build_idempotency_request_hash(payload)
    job_id = create_job("hello", passes=3, provider="ollama", model_summary="model")
    bound_job = bind_idempotency_key(
        idempotency_key="idem-1",
        request_hash=request_hash,
        endpoint="deep_think_async",
        job_id=job_id,
    )
    assert bound_job == job_id

    replay_job = lookup_idempotent_job("idem-1", request_hash, "deep_think_async")
    assert replay_job is not None
    assert replay_job["job_id"] == job_id


def test_idempotency_key_rejects_payload_mismatch(test_db):
    job_id = create_job("hello", passes=3, provider="ollama", model_summary="model")
    request_hash = build_idempotency_request_hash({"endpoint": "deep_think_async", "question": "hello"})
    bind_idempotency_key("idem-2", request_hash, "deep_think_async", job_id)
    mismatch_hash = build_idempotency_request_hash({"endpoint": "deep_think_async", "question": "different"})
    with pytest.raises(ValueError):
        lookup_idempotent_job("idem-2", mismatch_hash, "deep_think_async")
