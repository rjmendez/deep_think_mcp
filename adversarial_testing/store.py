"""SQLite persistence for the adversarial testing framework.

Tables
------
adversarial_jobs        — One row per TestJob; full lifecycle tracking.
adversarial_findings    — One row per confirmed Finding.
adversarial_audit_log   — Immutable append-only log of all test submissions.
adversarial_coverage    — Counters by category / attack type / endpoint.
adversarial_budget      — Daily abliteration API budget tracking.
adversarial_rate_window — Per-minute/day API rate limit counters.

Design follows the same WAL + per-operation connection pattern as store.py.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .schema import (
    Category,
    Finding,
    Reproducibility,
    Severity,
    TestJob,
    TestStatus,
)


def _db_path() -> str:
    import os

    path = os.getenv(
        "ADVERSARIAL_DB",
        str(Path.home() / ".deep_think" / "adversarial.db"),
    )
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS adversarial_jobs (
                job_id              TEXT PRIMARY KEY,
                status              TEXT NOT NULL DEFAULT 'queued',
                input               TEXT NOT NULL,
                expected_behavior   TEXT NOT NULL,
                category            TEXT,
                attack_type         TEXT,
                created_at          TEXT NOT NULL,
                started_at          TEXT,
                completed_at        TEXT,
                result_json         TEXT,
                finding_id          TEXT,
                error               TEXT,
                submitter_token     TEXT,
                regression          INTEGER NOT NULL DEFAULT 0,
                regression_finding_id TEXT
            );

            CREATE TABLE IF NOT EXISTS adversarial_findings (
                id                  TEXT PRIMARY KEY,
                severity            TEXT NOT NULL,
                category            TEXT NOT NULL,
                reproducibility     TEXT NOT NULL,
                impact              TEXT NOT NULL,
                mitigation          TEXT NOT NULL,
                example_input       TEXT NOT NULL,
                test_job_id         TEXT NOT NULL,
                created_at          TEXT NOT NULL,
                confirmed_at        TEXT,
                fixed_at            TEXT,
                false_positive      INTEGER NOT NULL DEFAULT 0,
                review_notes        TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS adversarial_audit_log (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type          TEXT NOT NULL,
                job_id              TEXT,
                finding_id          TEXT,
                submitter_token     TEXT,
                payload_hash        TEXT,
                timestamp           TEXT NOT NULL,
                details_json        TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS adversarial_coverage (
                dimension           TEXT NOT NULL,
                key                 TEXT NOT NULL,
                test_count          INTEGER NOT NULL DEFAULT 0,
                last_tested_at      TEXT,
                PRIMARY KEY (dimension, key)
            );

            CREATE TABLE IF NOT EXISTS adversarial_budget (
                date                TEXT PRIMARY KEY,
                calls_made          INTEGER NOT NULL DEFAULT 0,
                tokens_used         INTEGER NOT NULL DEFAULT 0,
                budget_usd          REAL NOT NULL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS adversarial_rate_window (
                window_key          TEXT PRIMARY KEY,
                count               INTEGER NOT NULL DEFAULT 0,
                reset_at            TEXT NOT NULL
            );

            -- Layer 5 Self-Improvement System Tables
            CREATE TABLE IF NOT EXISTS self_improvement_plans (
                id                  TEXT PRIMARY KEY,
                finding_ids         TEXT NOT NULL,  -- JSON array of finding IDs
                plan_json           TEXT NOT NULL,  -- Full deep_think response
                priority            REAL NOT NULL,
                effort_estimate     INTEGER NOT NULL,
                risk_level          TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'pending',
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                approved_by         TEXT,
                deployment_sha      TEXT
            );

            CREATE TABLE IF NOT EXISTS implementation_tasks (
                id                  TEXT PRIMARY KEY,
                plan_id             TEXT NOT NULL,
                task_description    TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'pending',
                implementation_notes TEXT NOT NULL DEFAULT '',
                commit_sha          TEXT,
                created_at          TEXT NOT NULL,
                completed_at        TEXT,
                FOREIGN KEY (plan_id) REFERENCES self_improvement_plans(id)
            );

            CREATE TABLE IF NOT EXISTS validation_results (
                id                  TEXT PRIMARY KEY,
                plan_id             TEXT NOT NULL,
                implementation_id   TEXT NOT NULL,
                test_output         TEXT NOT NULL,
                before_metrics      TEXT NOT NULL,  -- JSON metrics snapshot
                after_metrics       TEXT NOT NULL,  -- JSON metrics snapshot
                regression_detected INTEGER NOT NULL DEFAULT 0,
                improvement_score   REAL NOT NULL DEFAULT 0.0,
                status              TEXT NOT NULL DEFAULT 'pending',
                created_at          TEXT NOT NULL,
                FOREIGN KEY (plan_id) REFERENCES self_improvement_plans(id)
            );

            CREATE TABLE IF NOT EXISTS deployment_events (
                id                  TEXT PRIMARY KEY,
                plan_id             TEXT NOT NULL,
                commit_sha          TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'pending',
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                FOREIGN KEY (plan_id) REFERENCES self_improvement_plans(id)
            );

            CREATE TABLE IF NOT EXISTS layer5_audit_log (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                event               TEXT NOT NULL,
                plan_id             TEXT,
                finding_id          TEXT,
                details             TEXT NOT NULL DEFAULT '{}',
                timestamp           TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_self_improvement_status ON self_improvement_plans(status);
            CREATE INDEX IF NOT EXISTS idx_implementation_tasks_plan ON implementation_tasks(plan_id);
            CREATE INDEX IF NOT EXISTS idx_validation_results_plan ON validation_results(plan_id);
            CREATE INDEX IF NOT EXISTS idx_deployment_events_plan ON deployment_events(plan_id);
            """
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Job CRUD
# ---------------------------------------------------------------------------


def save_job(job: TestJob) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO adversarial_jobs
              (job_id, status, input, expected_behavior, category, attack_type,
               created_at, started_at, completed_at, result_json, finding_id,
               error, submitter_token, regression, regression_finding_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job.job_id,
                job.status.value,
                job.input,
                job.expected_behavior,
                job.category,
                job.attack_type,
                job.created_at.isoformat() if job.created_at else _now_iso(),
                job.started_at.isoformat() if job.started_at else None,
                job.completed_at.isoformat() if job.completed_at else None,
                json.dumps(job.result) if job.result else None,
                job.finding.id if job.finding else None,
                job.error,
                job.submitter_token,
                1 if job.regression else 0,
                job.regression_finding_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_job(job_id: str) -> Optional[TestJob]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM adversarial_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if not row:
            return None
        return _row_to_job(row)
    finally:
        conn.close()


def load_jobs_by_status(status: TestStatus) -> List[TestJob]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM adversarial_jobs WHERE status = ? ORDER BY created_at",
            (status.value,),
        ).fetchall()
        return [_row_to_job(r) for r in rows]
    finally:
        conn.close()


def list_jobs(limit: int = 100) -> List[TestJob]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM adversarial_jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_job(r) for r in rows]
    finally:
        conn.close()


def count_running_jobs() -> int:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM adversarial_jobs WHERE status = 'running'"
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _row_to_job(row: sqlite3.Row) -> TestJob:
    finding = None
    if row["finding_id"]:
        finding = load_finding(row["finding_id"])
    return TestJob(
        job_id=row["job_id"],
        status=TestStatus(row["status"]),
        input=row["input"],
        expected_behavior=row["expected_behavior"],
        category=row["category"],
        attack_type=row["attack_type"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        finding=finding,
        error=row["error"],
        submitter_token=row["submitter_token"],
        regression=bool(row["regression"]),
        regression_finding_id=row["regression_finding_id"],
    )


# ---------------------------------------------------------------------------
# Finding CRUD
# ---------------------------------------------------------------------------


def save_finding(finding: Finding) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO adversarial_findings
              (id, severity, category, reproducibility, impact, mitigation,
               example_input, test_job_id, created_at, confirmed_at, fixed_at,
               false_positive, review_notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                finding.id,
                finding.severity.value,
                finding.category.value,
                finding.reproducibility.value,
                finding.impact,
                finding.mitigation,
                finding.example_input,
                finding.test_job_id,
                finding.created_at.isoformat(),
                finding.confirmed_at.isoformat() if finding.confirmed_at else None,
                finding.fixed_at.isoformat() if finding.fixed_at else None,
                1 if finding.false_positive else 0,
                finding.review_notes,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_finding(finding_id: str) -> Optional[Finding]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM adversarial_findings WHERE id = ?", (finding_id,)
        ).fetchone()
        if not row:
            return None
        return _row_to_finding(row)
    finally:
        conn.close()


def list_findings(
    severity: Optional[Severity] = None,
    resolved: Optional[bool] = None,
    limit: int = 100,
) -> List[Finding]:
    conn = _connect()
    try:
        clauses: List[str] = []
        params: List = []
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity.value)
        if resolved is False:
            clauses.append("fixed_at IS NULL AND false_positive = 0")
        elif resolved is True:
            clauses.append("(fixed_at IS NOT NULL OR false_positive = 1)")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM adversarial_findings {where} ORDER BY created_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        return [_row_to_finding(r) for r in rows]
    finally:
        conn.close()


def count_findings_by_severity() -> dict:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT severity, COUNT(*) as cnt
            FROM adversarial_findings
            WHERE false_positive = 0
            GROUP BY severity
            """
        ).fetchall()
        result = {s.value: 0 for s in Severity}
        for r in rows:
            result[r["severity"]] = r["cnt"]
        return result
    finally:
        conn.close()


def _row_to_finding(row: sqlite3.Row) -> Finding:
    return Finding(
        id=row["id"],
        severity=Severity(row["severity"]),
        category=Category(row["category"]),
        reproducibility=Reproducibility(row["reproducibility"]),
        impact=row["impact"],
        mitigation=row["mitigation"],
        example_input=row["example_input"],
        test_job_id=row["test_job_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        confirmed_at=datetime.fromisoformat(row["confirmed_at"]) if row["confirmed_at"] else None,
        fixed_at=datetime.fromisoformat(row["fixed_at"]) if row["fixed_at"] else None,
        false_positive=bool(row["false_positive"]),
        review_notes=row["review_notes"] or "",
    )


# ---------------------------------------------------------------------------
# Audit log (append-only)
# ---------------------------------------------------------------------------


def audit_log(
    event_type: str,
    job_id: Optional[str] = None,
    finding_id: Optional[str] = None,
    submitter_token: Optional[str] = None,
    payload_hash: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO adversarial_audit_log
              (event_type, job_id, finding_id, submitter_token,
               payload_hash, timestamp, details_json)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                event_type,
                job_id,
                finding_id,
                submitter_token,
                payload_hash,
                _now_iso(),
                json.dumps(details or {}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Coverage tracking
# ---------------------------------------------------------------------------


def increment_coverage(dimension: str, key: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO adversarial_coverage (dimension, key, test_count, last_tested_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(dimension, key) DO UPDATE
              SET test_count = test_count + 1,
                  last_tested_at = excluded.last_tested_at
            """,
            (dimension, key, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def get_coverage() -> dict:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT dimension, key, test_count, last_tested_at FROM adversarial_coverage"
        ).fetchall()
        result: dict = {}
        for r in rows:
            result.setdefault(r["dimension"], {})[r["key"]] = {
                "test_count": r["test_count"],
                "last_tested_at": r["last_tested_at"],
            }
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Budget tracking
# ---------------------------------------------------------------------------


def record_abliteration_call(tokens_used: int = 0, cost_usd: float = 0.0) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO adversarial_budget (date, calls_made, tokens_used, budget_usd)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(date) DO UPDATE
              SET calls_made = calls_made + 1,
                  tokens_used = tokens_used + excluded.tokens_used,
                  budget_usd = budget_usd + excluded.budget_usd
            """,
            (today, tokens_used, cost_usd),
        )
        conn.commit()
    finally:
        conn.close()


def get_daily_budget(date: Optional[str] = None) -> dict:
    today = date or datetime.now(timezone.utc).date().isoformat()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM adversarial_budget WHERE date = ?", (today,)
        ).fetchone()
        if not row:
            return {"date": today, "calls_made": 0, "tokens_used": 0, "budget_usd": 0.0}
        return dict(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rate window helpers
# ---------------------------------------------------------------------------


def get_and_increment_rate_window(window_key: str, window_duration_seconds: int) -> int:
    """Return the current count (after incrementing) for a rate window.

    Automatically resets the counter when the window expires.
    """
    conn = _connect()
    try:
        now = datetime.now(timezone.utc)
        row = conn.execute(
            "SELECT count, reset_at FROM adversarial_rate_window WHERE window_key = ?",
            (window_key,),
        ).fetchone()

        if row:
            reset_at = datetime.fromisoformat(row["reset_at"])
            if now >= reset_at:
                # Window expired — reset
                new_reset = datetime.fromtimestamp(
                    now.timestamp() + window_duration_seconds, tz=timezone.utc
                )
                conn.execute(
                    "UPDATE adversarial_rate_window SET count = 1, reset_at = ? WHERE window_key = ?",
                    (new_reset.isoformat(), window_key),
                )
                conn.commit()
                return 1
            else:
                new_count = row["count"] + 1
                conn.execute(
                    "UPDATE adversarial_rate_window SET count = ? WHERE window_key = ?",
                    (new_count, window_key),
                )
                conn.commit()
                return new_count
        else:
            reset_at = datetime.fromtimestamp(
                now.timestamp() + window_duration_seconds, tz=timezone.utc
            )
            conn.execute(
                "INSERT INTO adversarial_rate_window (window_key, count, reset_at) VALUES (?,1,?)",
                (window_key, reset_at.isoformat()),
            )
            conn.commit()
            return 1
    finally:
        conn.close()
