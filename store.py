"""SQLite job store for deep_think_mcp.

Design:
- Per-operation connections (not a shared long-lived connection)
- WAL journal mode + busy_timeout for concurrent read/write safety
- BEGIN IMMEDIATE for atomic job claiming (prevents double-claim)
- On startup: stale 'running' jobs are reset to 'queued' for recovery
"""

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_lock = threading.Lock()


def _db_path() -> str:
    import os

    path = os.getenv("DEEP_THINK_DB", str(Path.home() / ".deep_think" / "jobs.db"))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS thinking_jobs (
                    job_id               TEXT PRIMARY KEY,
                    status               TEXT NOT NULL DEFAULT 'queued',
                    question             TEXT NOT NULL,
                    passes               INTEGER NOT NULL DEFAULT 3,
                    provider             TEXT,
                    model_summary        TEXT,
                    provider_config_json TEXT DEFAULT '{}',
                    created_at           TEXT NOT NULL,
                    started_at           TEXT,
                    completed_at         TEXT,
                    result               TEXT,
                    error                TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def create_job(
    question: str,
    passes: int,
    provider: str,
    model_summary: str,
    provider_config_json: str = "{}",
) -> str:
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO thinking_jobs
                    (job_id, status, question, passes, provider,
                     model_summary, provider_config_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, "queued", question, passes, provider,
                 model_summary, provider_config_json, now),
            )
            conn.commit()
        finally:
            conn.close()
    return job_id


def claim_next_job() -> Optional[dict]:
    """Atomically claim the oldest queued job. Returns the job dict or None."""
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM thinking_jobs WHERE status='queued' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                return None
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE thinking_jobs SET status='running', started_at=? WHERE job_id=?",
                (now, row["job_id"]),
            )
            conn.execute("COMMIT")
            return dict(row)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()


def complete_job(job_id: str, result: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE thinking_jobs SET status='complete', result=?, completed_at=? WHERE job_id=?",
                (result, now, job_id),
            )
            conn.commit()
        finally:
            conn.close()


def fail_job(job_id: str, error: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE thinking_jobs SET status='failed', error=?, completed_at=? WHERE job_id=?",
                (error, now, job_id),
            )
            conn.commit()
        finally:
            conn.close()


def requeue_stale() -> int:
    """Reset any 'running' jobs to 'queued'. Call on startup to recover from crashes."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE thinking_jobs SET status='queued', started_at=NULL WHERE status='running'"
            )
            count = conn.execute("SELECT changes()").fetchone()[0]
            conn.commit()
            return count
        finally:
            conn.close()


def get_job(job_id: str) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM thinking_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_jobs(status: str = "all", limit: int = 10) -> list[dict]:
    conn = _connect()
    try:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM thinking_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM thinking_jobs WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
