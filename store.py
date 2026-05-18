"""SQLite job store for deep_think_mcp.

Design:
- Per-operation connections (not a shared long-lived connection)
- WAL journal mode + busy_timeout for concurrent read/write safety
- BEGIN IMMEDIATE for atomic job claiming (prevents double-claim)
- threading.Lock removed — SQLite WAL + BEGIN IMMEDIATE already serializes the
  only operation that needs it (claim_next_job). Reads are naturally concurrent.
- On startup: stale 'running' jobs are reset to 'queued' using a time-based
  cutoff (DEEP_THINK_STALE_JOB_MINUTES, default 120) so multiple concurrent
  worker processes don't race to requeue each other's live jobs.

TRANSACTION SEMANTICS:
- Cache writes (pass_cache) are batched with job status updates (thinking_jobs)
  in a single transaction to ensure atomicity.
- If job completion commits successfully, all cache entries are persisted.
- If commit fails, entire transaction rolls back — no orphaned cache entries.
- Database integrity checks run on startup to detect corruption.
- Automatic backup/restore pattern for corruption recovery.

Tables:
- thinking_jobs      — reasoning job queue and results
- model_cache        — discovered model info + benchmarks (from discover.py)
- discovery_meta     — tracks last discovery run and ollama model set hash
- perspective_cache  — cached per-perspective outputs for fan-out jobs
                       keyed by content hash (question + mandate + height + model),
                       enabling resume-on-failure, repeatability, and debugging.
                       Entries expire after DEEP_THINK_CACHE_TTL_HOURS (default 24h).
                       Analogy: DAMA pheromone evaporation — stale signals fade.
- pass_cache         — individual pass outputs stored after each pass completes,
                       indexed by (job_id, perspective, pass_num) + run_sig.
                       Enables mid-job resume: if a job crashes on pass 3 of 6,
                       the next run reloads passes 1–2 and continues from pass 3.
                       run_sig locks in execution inputs (question, directives,
                       model, etc.) so cached passes are only replayed when the
                       resumed run is semantically identical.
"""

import json
import hashlib
import logging
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_FAILED_TERMINAL_STATUSES = {
    "failed",
    "failure",
    "error",
    "timeout",
    "timed_out",
    "cancelled",
    "canceled",
    "validation_error",
}


def _db_path() -> str:
    import os

    path = os.getenv("DEEP_THINK_DB", str(Path.home() / ".deep_think" / "jobs.db"))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return path


def _max_result_bytes() -> int:
    import os

    return int(os.getenv("DEEP_THINK_MAX_RESULT_BYTES", str(2 * 1024 * 1024)))


def _job_retention_days() -> int:
    import os
    return max(int(os.getenv("DEEP_THINK_JOB_RETENTION_DAYS", "30")), 0)


def _job_retention_max_rows() -> int:
    import os
    return max(int(os.getenv("DEEP_THINK_JOB_RETENTION_MAX_ROWS", "10000")), 0)


def _validate_result_size(result: str) -> None:
    size_bytes = len(result.encode("utf-8"))
    max_bytes = _max_result_bytes()
    if size_bytes > max_bytes:
        raise ValueError(
            f"Result payload too large to persist safely ({size_bytes} bytes > {max_bytes} bytes). "
            f"Increase DEEP_THINK_MAX_RESULT_BYTES or reduce output size."
        )


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    db_path = Path(_db_path())

    # Daily backup: copy .db to .db.backup if backup is missing or >24 h old.
    # Only when the database already exists (skip on first-time creation).
    if db_path.exists():
        _ensure_daily_backup(db_path)

    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thinking_jobs (
                job_id               TEXT PRIMARY KEY,
                status               TEXT NOT NULL DEFAULT 'queued',
                question             TEXT NOT NULL,
                passes               INTEGER NOT NULL DEFAULT 3,
                timeout_secs         INTEGER NOT NULL DEFAULT 300,
                provider             TEXT,
                model_summary        TEXT,
                provider_config_json TEXT DEFAULT '{}',
                created_at           TEXT NOT NULL,
                started_at           TEXT,
                claimed_by           TEXT,
                claimed_at           TEXT,
                cancel_requested_at  TEXT,
                cancel_reason        TEXT,
                completed_at         TEXT,
                result               TEXT,
                error                TEXT,
                CHECK (status IN ('queued', 'running', 'complete', 'failed'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_thinking_jobs_status "
            "ON thinking_jobs(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_thinking_jobs_created "
            "ON thinking_jobs(created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_thinking_jobs_status_created "
            "ON thinking_jobs(status, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_thinking_jobs_status_claimed "
            "ON thinking_jobs(status, claimed_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_thinking_jobs_status_completed "
            "ON thinking_jobs(status, completed_at)"
        )
        existing_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(thinking_jobs)").fetchall()
        }
        if "timeout_secs" not in existing_columns:
            conn.execute(
                "ALTER TABLE thinking_jobs ADD COLUMN timeout_secs INTEGER NOT NULL DEFAULT 300"
            )
            existing_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(thinking_jobs)").fetchall()
            }
        if "cancel_requested_at" not in existing_columns:
            conn.execute("ALTER TABLE thinking_jobs ADD COLUMN cancel_requested_at TEXT")
            existing_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(thinking_jobs)").fetchall()
            }
        if "cancel_reason" not in existing_columns:
            conn.execute("ALTER TABLE thinking_jobs ADD COLUMN cancel_reason TEXT")
            existing_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(thinking_jobs)").fetchall()
            }
        required_columns = {
            "job_id",
            "status",
            "question",
            "passes",
            "timeout_secs",
            "provider",
            "model_summary",
            "provider_config_json",
            "created_at",
            "cancel_requested_at",
            "cancel_reason",
            "result",
            "error",
        }
        missing_required = sorted(required_columns - existing_columns)
        if missing_required:
            raise RuntimeError(
                "thinking_jobs schema missing required columns: "
                + ", ".join(missing_required)
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_cache (
                model_id        TEXT NOT NULL,
                provider        TEXT NOT NULL,
                size_b          REAL DEFAULT 0,
                suggested_tier  TEXT DEFAULT 'medium',
                capabilities    TEXT DEFAULT '["general"]',
                benchmark_ms    INTEGER DEFAULT 0,
                timeout_secs    INTEGER DEFAULT 300,
                is_available    INTEGER DEFAULT 1,
                last_checked    TEXT,
                PRIMARY KEY (model_id, provider)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discovery_meta (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS perspective_cache (
                cache_key       TEXT PRIMARY KEY,
                perspective_name TEXT NOT NULL,
                model_summary   TEXT,
                passes_run      INTEGER NOT NULL DEFAULT 1,
                final_answer    TEXT NOT NULL,
                job_id          TEXT,
                created_at      TEXT NOT NULL,
                expires_at      TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_perspective_cache_expires "
            "ON perspective_cache(expires_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pass_cache (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id       TEXT NOT NULL,
                perspective  TEXT NOT NULL DEFAULT '',
                pass_num     INTEGER NOT NULL,
                run_sig      TEXT NOT NULL,
                framing      TEXT,
                tier         TEXT,
                model_used   TEXT,
                provider     TEXT,
                output       TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                expires_at   TEXT NOT NULL,
                UNIQUE(job_id, perspective, pass_num)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pass_cache_expires "
            "ON pass_cache(expires_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pass_cache_lookup "
            "ON pass_cache(job_id, perspective, run_sig)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_idempotency_keys (
                idempotency_key  TEXT PRIMARY KEY,
                request_hash     TEXT NOT NULL,
                endpoint         TEXT NOT NULL,
                job_id           TEXT NOT NULL,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES thinking_jobs(job_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_idempotency_job_id "
            "ON job_idempotency_keys(job_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS self_improvement_plans (
                id                  TEXT PRIMARY KEY,
                finding_ids         TEXT NOT NULL,
                plan_json           TEXT NOT NULL,
                priority            REAL NOT NULL DEFAULT 0.0,
                effort_estimate     INTEGER DEFAULT 0,
                risk_level          TEXT DEFAULT 'MEDIUM',
                status              TEXT NOT NULL DEFAULT 'pending',
                deep_think_job_id   TEXT,
                approval_notes      TEXT,
                approved_by         TEXT,
                approved_at         TEXT,
                deployment_sha      TEXT,
                validation_score    REAL,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_plans_status "
            "ON self_improvement_plans(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_plans_priority "
            "ON self_improvement_plans(priority DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plan_audit_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id         TEXT NOT NULL,
                event_type      TEXT NOT NULL,
                details_json    TEXT,
                created_at      TEXT NOT NULL,
                FOREIGN KEY(plan_id) REFERENCES self_improvement_plans(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_plan_id "
            "ON plan_audit_log(plan_id)"
        )
        conn.commit()
    finally:
        conn.close()

    # Purge expired cache rows on every startup so stale entries don't
    # accumulate between runs (DAMA pheromone evaporation).
    evict_expired_cache()
    prune_thinking_jobs()

    # Integrity check: warn and attempt VACUUM on failure.
    _startup_integrity_check()


def init_db_with_integrity_check() -> None:
    """Initialize database and verify integrity on startup.
    
    Runs PRAGMA integrity_check and restores from backup if corruption detected.
    """
    init_db()
    
    is_valid, message = check_db_integrity()
    if not is_valid:
        log.error(f"Database integrity check failed on startup: {message}")
        
        # Try to restore from latest backup
        backup_dir = Path(_db_path()).parent / "backups"
        if backup_dir.exists():
            backups = sorted(backup_dir.glob("jobs_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
            if backups:
                try:
                    latest_backup = str(backups[0])
                    log.info(f"Restoring from backup: {latest_backup}")
                    _restore_db(latest_backup)
                    is_valid, message = check_db_integrity()
                    if is_valid:
                        log.info("Database restored and integrity check passed")
                        return
                except Exception as e:
                    log.error(f"Failed to restore from backup: {e}")
        
        # Create backup before raising error
        try:
            _backup_db("corruption_detected")
        except Exception as e:
            log.error(f"Failed to backup corrupted database: {e}")
        
        raise RuntimeError(f"Database integrity check failed: {message}")
    
    # Validate cache consistency
    all_valid, issues = validate_all_cache_consistency()
    if not all_valid:
        log.warning(f"Cache consistency issues detected: {issues}")
        for job_id, job_issues in issues.items():
            log.warning(f"  Job {job_id}: {job_issues}")



def _ensure_daily_backup(db_path: Path) -> None:
    """Copy the database to a .db.backup sidecar if it is missing or older than 24 h.

    Uses the SQLite online backup API (sqlite3.Connection.backup) instead of
    shutil.copy2 so that WAL-mode writes accumulated since the last checkpoint
    are captured atomically even with concurrent connections open.
    """
    backup_path = db_path.parent / (db_path.name + ".backup")
    now = datetime.now(timezone.utc).timestamp()
    needs_backup = (
        not backup_path.exists()
        or (now - backup_path.stat().st_mtime) > 86400
    )
    if needs_backup:
        try:
            src = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
            dst = sqlite3.connect(str(backup_path), check_same_thread=False, timeout=10)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            log.info("Daily DB backup created: %s", backup_path)
        except Exception as exc:
            log.warning("Failed to create daily DB backup: %s", exc)


def _startup_integrity_check() -> None:
    """Run PRAGMA integrity_check; log WARNING and attempt VACUUM on failure."""
    is_ok, message = check_db_integrity()
    if is_ok:
        return

    log.warning("Database integrity check failed on startup: %s", message)

    # Attempt VACUUM before giving up
    try:
        conn = _connect()
        try:
            conn.execute("VACUUM")
            conn.commit()
        finally:
            conn.close()
        log.info("VACUUM completed; re-checking integrity…")
        is_ok, message = check_db_integrity()
        if is_ok:
            log.info("Database integrity check passed after VACUUM")
            return
    except Exception as exc:
        log.warning("VACUUM attempt failed: %s", exc)

    log.error("Database integrity check still failing after VACUUM: %s", message)


def _backup_db(suffix: str = "auto") -> str:
    """Create a backup of the database using sqlite3 online backup API. Returns backup path."""
    db_path = Path(_db_path())
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"jobs_{timestamp}_{suffix}.db"
    src = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
    dst = sqlite3.connect(str(backup_path), check_same_thread=False, timeout=10)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    log.info(f"Database backup created: {backup_path}")
    return str(backup_path)


def _restore_db(backup_path: str) -> None:
    """Restore database from backup."""
    db_path = Path(_db_path())
    if not Path(backup_path).exists():
        raise FileNotFoundError(f"Backup not found: {backup_path}")
    src = sqlite3.connect(str(backup_path), check_same_thread=False, timeout=10)
    dst = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    log.info(f"Database restored from: {backup_path}")


def check_db_integrity() -> tuple[bool, str]:
    """Run PRAGMA integrity_check on the database.
    
    Returns: (is_valid, message)
    """
    conn = _connect()
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        is_valid = result[0] == "ok"
        message = result[0]
        if not is_valid:
            log.error(f"Database integrity check failed: {message}")
        return is_valid, message
    finally:
        conn.close()


def validate_cache_consistency(job_id: str) -> tuple[bool, list[str]]:
    """Validate that no orphaned cache entries exist for failed jobs.
    
    Note: Complete jobs may or may not have cache entries depending on whether
    caching was used during execution. Only orphaned entries (failed job with cache)
    are considered an issue.
    
    Returns: (is_consistent, list of issues)
    """
    conn = _connect()
    issues = []
    try:
        job = conn.execute(
            "SELECT status FROM thinking_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        
        if not job:
            issues.append(f"Job {job_id} not found")
            return False, issues
        
        status = job["status"]
        cache_exists = conn.execute(
            "SELECT COUNT(*) as cnt FROM pass_cache WHERE job_id=?", (job_id,)
        ).fetchone()["cnt"] > 0
        
        # Only flag orphaned entries (failed job with cache)
        if status == "failed" and cache_exists:
            issues.append(f"Failed job {job_id} has orphaned cache entries")
        
        return len(issues) == 0, issues
    finally:
        conn.close()


def validate_all_cache_consistency() -> tuple[bool, dict[str, list[str]]]:
    """Validate cache consistency for all jobs.
    
    Returns: (all_valid, dict of job_id -> list of issues)
    """
    conn = _connect()
    all_valid = True
    issues_by_job = {}
    
    try:
        jobs = conn.execute(
            "SELECT job_id, status FROM thinking_jobs WHERE status IN ('complete', 'failed')"
        ).fetchall()
        
        for job in jobs:
            is_valid, issues = validate_cache_consistency(job["job_id"])
            if not is_valid:
                all_valid = False
                issues_by_job[job["job_id"]] = issues
        
        return all_valid, issues_by_job
    finally:
        conn.close()


def create_job(
    question: str,
    passes: int,
    provider: str,
    model_summary: str,
    provider_config_json: str = "{}",
    timeout_secs: int = 300,
) -> str:
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    timeout_secs = max(int(timeout_secs or 300), 60)
    conn = _connect()
    try:
        try:
            conn.execute(
                """
                INSERT INTO thinking_jobs
                    (job_id, status, question, passes, provider,
                     model_summary, provider_config_json, timeout_secs, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, "queued", question, passes, provider,
                 model_summary, provider_config_json, timeout_secs, now),
            )
        except sqlite3.OperationalError as exc:
            if "timeout_secs" not in str(exc):
                raise
            conn.execute(
                "ALTER TABLE thinking_jobs ADD COLUMN timeout_secs INTEGER NOT NULL DEFAULT 300"
            )
            conn.execute(
                """
                INSERT INTO thinking_jobs
                    (job_id, status, question, passes, provider,
                     model_summary, provider_config_json, timeout_secs, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, "queued", question, passes, provider,
                 model_summary, provider_config_json, timeout_secs, now),
            )
        conn.commit()
    finally:
        conn.close()
    # Bounded retention for completed/failed job history only.
    try:
        prune_thinking_jobs()
    except Exception as exc:
        log.warning("Job retention prune failed (non-fatal): %s", exc)
    return job_id


def build_idempotency_request_hash(payload: dict) -> str:
    """Create a stable hash for an idempotency request payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def lookup_idempotent_job(idempotency_key: str, request_hash: str, endpoint: str) -> Optional[dict]:
    """Return existing job bound to key/hash/endpoint, or None if unbound.

    Raises ValueError if the key is already bound to a different request payload.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM job_idempotency_keys WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        existing_hash = row["request_hash"]
        existing_endpoint = row["endpoint"] or ""
        if existing_hash != request_hash or existing_endpoint != endpoint:
            raise ValueError("Idempotency key already used with a different request payload")
        job_row = conn.execute(
            "SELECT * FROM thinking_jobs WHERE job_id=?",
            (row["job_id"],),
        ).fetchone()
        if job_row is None:
            conn.execute("DELETE FROM job_idempotency_keys WHERE idempotency_key=?", (idempotency_key,))
            conn.commit()
            return None
        return dict(job_row)
    finally:
        conn.close()


def bind_idempotency_key(
    idempotency_key: str,
    request_hash: str,
    endpoint: str,
    job_id: str,
) -> str:
    """Bind idempotency key to a job.

    Returns the existing job_id when key is already bound, otherwise returns the
    newly bound job_id.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM job_idempotency_keys WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO job_idempotency_keys
                    (idempotency_key, request_hash, endpoint, job_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (idempotency_key, request_hash, endpoint, job_id, now, now),
            )
            conn.commit()
            return job_id
        existing_hash = row["request_hash"]
        existing_endpoint = row["endpoint"] or ""
        existing_job_id = row["job_id"]
        if existing_hash != request_hash or existing_endpoint != endpoint:
            conn.execute("ROLLBACK")
            raise ValueError("Idempotency key already used with a different request payload")
        conn.execute(
            "UPDATE job_idempotency_keys SET updated_at=? WHERE idempotency_key=?",
            (now, idempotency_key),
        )
        conn.commit()
        return existing_job_id
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def prune_thinking_jobs(
    conn_or_path: Optional[str] = None,
    max_rows: Optional[int] = None,
    max_age_days: Optional[int] = None,
) -> int:
    """Prune terminal thinking_jobs rows using safe retention policy.

    Only rows in terminal states ('complete', 'failed') are eligible.
    Running/queued jobs are never pruned.
    """
    resolved_max_rows = _job_retention_max_rows() if max_rows is None else max(int(max_rows), 0)
    resolved_max_age_days = _job_retention_days() if max_age_days is None else max(int(max_age_days), 0)
    if resolved_max_rows == 0 and resolved_max_age_days == 0:
        return 0

    if isinstance(conn_or_path, str):
        conn = sqlite3.connect(conn_or_path, check_same_thread=False, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
    else:
        conn = _connect()

    pruned = 0
    now = datetime.now(timezone.utc)
    try:
        conn.execute("BEGIN IMMEDIATE")

        if resolved_max_age_days > 0:
            cutoff = (now - timedelta(days=resolved_max_age_days)).isoformat()
            cur_age = conn.execute(
                "DELETE FROM thinking_jobs "
                "WHERE status IN ('complete', 'failed') "
                "AND COALESCE(completed_at, created_at) <= ?",
                (cutoff,),
            )
            pruned += cur_age.rowcount

        if resolved_max_rows > 0:
            total_terminal = conn.execute(
                "SELECT COUNT(*) FROM thinking_jobs WHERE status IN ('complete', 'failed')"
            ).fetchone()[0]
            overflow = max(total_terminal - resolved_max_rows, 0)
            if overflow > 0:
                cur_rows = conn.execute(
                    "DELETE FROM thinking_jobs WHERE job_id IN ("
                    "  SELECT job_id FROM thinking_jobs "
                    "  WHERE status IN ('complete', 'failed') "
                    "  ORDER BY COALESCE(completed_at, created_at) ASC "
                    "  LIMIT ?"
                    ")",
                    (overflow,),
                )
                pruned += cur_rows.rowcount

        conn.commit()
        if pruned:
            log.info(
                "Pruned %d terminal thinking_jobs rows (max_rows=%d, max_age_days=%d)",
                pruned,
                resolved_max_rows,
                resolved_max_age_days,
            )
        return pruned
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def claim_next_job(worker_id: str = "default") -> Optional[dict]:
    """Atomically claim the oldest queued job. Returns the job dict or None.
    
    BUG FIX #3: Uses BEGIN IMMEDIATE + WHERE status='queued' condition in UPDATE
    to prevent double-claiming by concurrent workers. This makes the SELECT-UPDATE
    sequence atomic: if another worker claims the job first, the WHERE condition
    fails and we rollback with no harm done.
    """
    conn = _connect()
    try:
        # BUG FIX #3: BEGIN IMMEDIATE provides exclusive transaction lock
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM thinking_jobs "
            "WHERE status='queued' AND cancel_requested_at IS NULL "
            "ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        now = datetime.now(timezone.utc).isoformat()
        # BUG FIX #3: WHERE status='queued' ensures only queued jobs can transition to running.
        # If another worker updated this job first, the WHERE fails and rowcount is 0.
        cur = conn.execute(
            "UPDATE thinking_jobs SET status='running', started_at=?, claimed_by=?, claimed_at=? "
            "WHERE job_id=? AND status='queued' AND cancel_requested_at IS NULL",
            (now, worker_id, now, row["job_id"]),
        )
        # If no rows were updated, another worker claimed this job first - rollback and return None
        if cur.rowcount == 0:
            conn.execute("ROLLBACK")
            return None
        conn.execute("COMMIT")
        # Update the dict with new values
        result = dict(row)
        result["status"] = "running"
        result["started_at"] = now
        result["claimed_by"] = worker_id
        result["claimed_at"] = now
        return result
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def complete_job(
    job_id: str,
    result: str,
    cache_entries: Optional[list[dict]] = None,
    status: Optional[str] = None,
) -> None:
    """Mark job as complete with optional atomic cache writes.
    
    All cache entries and job status update are written in a single transaction.
    If commit fails, entire transaction rolls back — no orphaned cache entries.
    
    Args:
        job_id: Job ID
        result: Job result JSON string
        status: Optional explicit terminal status override ("complete" | "failed")
        cache_entries: Optional list of cache entry dicts with keys:
                      job_id, perspective, pass_num, run_sig, framing, tier,
                      model_used, provider, output
    """
    now = datetime.now(timezone.utc).isoformat()
    _validate_result_size(result)
    payload_status: Optional[str] = None
    payload_error: Optional[str] = None
    try:
        parsed = json.loads(result)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        raw_payload_status = parsed.get("status")
        payload_status = str(raw_payload_status) if raw_payload_status is not None else None
        raw_payload_error = parsed.get("error")
        payload_error = str(raw_payload_error) if raw_payload_error is not None else None

    effective_status = str(status or payload_status or "complete").strip().lower()
    if effective_status in {"complete", "completed", "partial"}:
        terminal_status = "complete"
    elif effective_status in _FAILED_TERMINAL_STATUSES:
        terminal_status = "failed"
    else:
        terminal_status = "failed"
    terminal_error = payload_error if terminal_status == "failed" else None

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        
        # Write cache entries atomically with job status
        if cache_entries:
            from datetime import timedelta
            expires = (datetime.now(timezone.utc) + timedelta(hours=_cache_ttl_hours())).isoformat()
            for entry in cache_entries:
                conn.execute(
                    """
                    INSERT INTO pass_cache
                        (job_id, perspective, pass_num, run_sig, framing, tier,
                         model_used, provider, output, created_at, expires_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(job_id, perspective, pass_num) DO UPDATE SET
                        run_sig=excluded.run_sig,
                        framing=excluded.framing,
                        tier=excluded.tier,
                        model_used=excluded.model_used,
                        provider=excluded.provider,
                        output=excluded.output,
                        created_at=excluded.created_at,
                        expires_at=excluded.expires_at
                    """,
                    (
                        entry["job_id"],
                        entry.get("perspective", ""),
                        entry["pass_num"],
                        entry["run_sig"],
                        entry.get("framing"),
                        entry.get("tier"),
                        entry.get("model_used"),
                        entry.get("provider"),
                        entry["output"],
                        now,
                        expires,
                    ),
                )
        
        # Update job status
        conn.execute(
            "UPDATE thinking_jobs SET status=?, result=?, error=?, completed_at=? WHERE job_id=?",
            (terminal_status, result, terminal_error, now, job_id),
        )
        
        # All-or-nothing commit
        conn.commit()
        log.info(f"Job {job_id} completed with {len(cache_entries) if cache_entries else 0} cache entries")
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        log.error(f"Failed to complete job {job_id}: {e}")
        # Attempt backup before re-raising
        try:
            _backup_db("fail_complete")
        except Exception as backup_err:
            log.error(f"Failed to backup database: {backup_err}")
        raise
    finally:
        conn.close()


def fail_job(job_id: str, error: str) -> None:
    """Mark job as failed and clean up any partial cache entries.
    
    Ensures no orphaned cache entries are left behind on job failure.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        
        # Delete any cache entries for failed job to avoid orphans
        conn.execute("DELETE FROM pass_cache WHERE job_id=?", (job_id,))
        
        # Update job status
        conn.execute(
            "UPDATE thinking_jobs SET status='failed', error=?, completed_at=? "
            "WHERE job_id=? AND status IN ('queued','running')",
            (error, now, job_id),
        )
        
        # All-or-nothing commit
        conn.commit()
        log.info(f"Job {job_id} marked as failed")
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        log.error(f"Failed to mark job {job_id} as failed: {e}")
        raise
    finally:
        conn.close()


def requeue_stale(stale_after_minutes: int = 0) -> int:
    """Reset timed-out 'running' jobs to 'queued'. Call on startup for crash recovery.

    Uses DEEP_THINK_STALE_JOB_MINUTES (default 120 min) — intentionally conservative
    so concurrent worker processes don't race to requeue each other's live jobs.
    The live watchdog uses the much shorter DEEP_THINK_ORPHAN_TIMEOUT_MINUTES.
    """
    import os
    from datetime import timedelta
    if stale_after_minutes <= 0:
        stale_after_minutes = int(os.getenv("DEEP_THINK_STALE_JOB_MINUTES", "120"))
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)
    ).isoformat()
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE thinking_jobs SET status='queued', started_at=NULL, claimed_by=NULL, claimed_at=NULL "
            "WHERE status='running' AND started_at < ? AND cancel_requested_at IS NULL",
            (cutoff,),
        )
        cancelled_cur = conn.execute(
            "UPDATE thinking_jobs SET status='failed', completed_at=?, error=COALESCE(error, ?) "
            "WHERE status='running' AND started_at < ? AND cancel_requested_at IS NOT NULL",
            (datetime.now(timezone.utc).isoformat(), "cancelled", cutoff),
        )
        count = cur.rowcount + cancelled_cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def evict_expired_cache(conn_or_path: Optional[str] = None) -> int:
    """Delete expired rows from pass_cache and perspective_cache.

    Rows whose expires_at timestamp (ISO-8601 TEXT) is earlier than or equal to
    the current UTC time are considered stale and purged.

    Args:
        conn_or_path: Path to the SQLite database file, or None to use the
                      default path resolved by ``_db_path()``.

    Returns:
        Total number of rows deleted across both tables.
    """
    now = datetime.now(timezone.utc).isoformat()
    if isinstance(conn_or_path, str):
        conn = sqlite3.connect(conn_or_path, check_same_thread=False, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
    else:
        conn = _connect()
    try:
        cur_pass = conn.execute(
            "DELETE FROM pass_cache WHERE expires_at <= ?", (now,)
        )
        cur_persp = conn.execute(
            "DELETE FROM perspective_cache WHERE expires_at <= ?", (now,)
        )
        evicted = cur_pass.rowcount + cur_persp.rowcount
        conn.commit()
        if evicted:
            log.info(
                "Evicted %d expired cache rows (%d pass_cache, %d perspective_cache)",
                evicted, cur_pass.rowcount, cur_persp.rowcount,
            )
        return evicted
    finally:
        conn.close()


def detect_orphaned_jobs(stale_after_minutes: int = 0) -> list[dict]:
    """Detect jobs stuck in 'running' state for longer than threshold.
    
    Returns list of orphaned job dicts that should be requeued.
    Uses DEEP_THINK_ORPHAN_TIMEOUT_MINUTES env var (default 10 min) for background
    watchdog detection. Intentionally shorter than the startup requeue threshold
    (DEEP_THINK_STALE_JOB_MINUTES) — safe to use from a live running process where
    there are no peer workers that could be mid-execution.
    """
    import os
    from datetime import timedelta
    
    if stale_after_minutes <= 0:
        stale_after_minutes = int(os.getenv("DEEP_THINK_ORPHAN_TIMEOUT_MINUTES", "10"))
    
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)
    ).isoformat()
    
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM thinking_jobs WHERE status='running' AND claimed_at < ?",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def requeue_orphaned_job(job_id: str, reason: str = "orphan_timeout") -> bool:
    """Requeue an orphaned job by resetting its status to 'queued'.
    
    BUG FIX #2: Explicitly sets status='queued' (not invalid 'pending').
    This ensures requeued jobs can be claimed by claim_next_job() which
    only looks for status='queued'. The UPDATE also includes WHERE status='running'
    to ensure we only requeue actually abandoned jobs, not ones claimed by active workers.
    
    Args:
        job_id: The job ID to requeue
        reason: The reason for requeue (for logging purposes)
        
    Returns:
        True if the job was requeued, False if not found
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, cancel_requested_at, cancel_reason FROM thinking_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None or row["status"] != "running":
            conn.execute("ROLLBACK")
            return False
        now = datetime.now(timezone.utc).isoformat()
        if row["cancel_requested_at"]:
            cancel_reason = row["cancel_reason"] or "cancelled"
            cur = conn.execute(
                "UPDATE thinking_jobs SET status='failed', completed_at=?, error=?, "
                "started_at=NULL, claimed_by=NULL, claimed_at=NULL WHERE job_id=? AND status='running'",
                (now, f"cancelled: {cancel_reason}", job_id),
            )
        else:
            # BUG FIX #2: status='queued' is the correct status for jobs awaiting processing
            cur = conn.execute(
                "UPDATE thinking_jobs SET status='queued', started_at=NULL, claimed_by=NULL, claimed_at=NULL "
                "WHERE job_id=? AND status='running'",
                (job_id,),
            )
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def request_job_cancellation(job_id: str, reason: str = "api_cancel") -> Optional[dict]:
    """Request cancellation for queued/running jobs with safe state transitions."""
    reason = (reason or "api_cancel").strip() or "api_cancel"
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, cancel_requested_at, cancel_reason FROM thinking_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        status = row["status"]
        if status in ("complete", "failed"):
            conn.execute("ROLLBACK")
            return {
                "job_id": job_id,
                "status": status,
                "cancel_requested": bool(row["cancel_requested_at"]),
                "terminal": True,
            }
        if status == "queued":
            error = f"cancelled: {reason}"
            conn.execute(
                "UPDATE thinking_jobs SET status='failed', error=?, completed_at=?, "
                "cancel_requested_at=COALESCE(cancel_requested_at, ?), "
                "cancel_reason=COALESCE(cancel_reason, ?) "
                "WHERE job_id=? AND status='queued'",
                (error, now, now, reason, job_id),
            )
            conn.commit()
            return {
                "job_id": job_id,
                "status": "failed",
                "cancel_requested": True,
                "terminal": True,
                "transition": "queued_to_failed",
            }
        conn.execute(
            "UPDATE thinking_jobs SET cancel_requested_at=COALESCE(cancel_requested_at, ?), "
            "cancel_reason=COALESCE(cancel_reason, ?) WHERE job_id=? AND status='running'",
            (now, reason, job_id),
        )
        conn.commit()
        return {
            "job_id": job_id,
            "status": "running",
            "cancel_requested": True,
            "terminal": False,
            "transition": "running_cancel_requested",
        }
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def get_cancellation_error(job_id: str) -> Optional[str]:
    """Return canonical cancellation error text for a job if cancellation was requested."""
    conn = _connect()
    try:
        try:
            row = conn.execute(
                "SELECT cancel_requested_at, cancel_reason FROM thinking_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "cancel_requested_at" in str(exc):
                return None
            raise
        if row is None or not row["cancel_requested_at"]:
            return None
        reason = row["cancel_reason"] or "cancelled"
        return f"cancelled: {reason}"
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


# ---------------------------------------------------------------------------
# Discovery persistence
# ---------------------------------------------------------------------------


def save_discovery(result: "DiscoveryResult", ollama_hash: str) -> None:
    """Persist a DiscoveryResult to model_cache and discovery_meta."""
    from .discover import DiscoveryResult  # noqa: F401 — satisfies type checker for the annotation
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        # Upsert each model
        for m in result.models:
            conn.execute(
                """
                INSERT INTO model_cache
                    (model_id, provider, size_b, suggested_tier, capabilities,
                     benchmark_ms, timeout_secs, is_available, last_checked)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(model_id, provider) DO UPDATE SET
                    size_b=excluded.size_b,
                    suggested_tier=excluded.suggested_tier,
                    capabilities=excluded.capabilities,
                    benchmark_ms=excluded.benchmark_ms,
                    timeout_secs=excluded.timeout_secs,
                    is_available=excluded.is_available,
                    last_checked=excluded.last_checked
                """,
                (
                    m.model_id, m.provider, m.size_b, m.suggested_tier,
                    json.dumps(m.capabilities), m.benchmark_ms,
                    m.timeout_secs, int(m.is_available), m.last_checked or now,
                ),
            )
        # Save tier assignments as JSON
        tier_json = json.dumps(
            {p: {"light": ta.light, "medium": ta.medium, "heavy": ta.heavy}
             for p, ta in result.tier_assignments.items()}
        )
        for key, value in [
            ("ollama_hash", ollama_hash),
            ("tier_assignments", tier_json),
            ("completed_at", result.completed_at or now),
            ("discovery_secs", str(result.discovery_secs)),
        ]:
            conn.execute(
                "INSERT INTO discovery_meta (key,value,updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, now),
            )
        conn.commit()
    finally:
        conn.close()


def load_discovery(
    current_ollama_hash: str,
    max_age_hours: int = 24,
) -> "DiscoveryResult | None":  # type: ignore[name-defined]
    """Load cached DiscoveryResult if fresh and hash matches. Returns None on miss."""
    from .discover import DiscoveryResult, ModelInfo, TierAssignment  # local import
    conn = _connect()
    try:
        meta_rows = conn.execute(
            "SELECT key, value, updated_at FROM discovery_meta"
        ).fetchall()
        meta = {r["key"]: r["value"] for r in meta_rows}
        # Use the most recent updated_at across all meta rows — this is the actual
        # DB write time from save_discovery, regardless of which key row we land on.
        # Filtering for key=="completed_at" silently skips expiry if that row is absent.
        updated_at_str = max(
            (r["updated_at"] for r in meta_rows if r["updated_at"]),
            default=None,
        )
        if not meta or "ollama_hash" not in meta:
            return None

        # Check hash match (model set unchanged)
        if meta["ollama_hash"] != current_ollama_hash:
            return None

        # Check age
        if updated_at_str:
            try:
                from datetime import timedelta
                updated = datetime.fromisoformat(updated_at_str)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - updated
                if age.total_seconds() > max_age_hours * 3600:
                    return None
            except Exception:
                return None

        # Load models
        rows = conn.execute(
            "SELECT * FROM model_cache WHERE is_available=1"
        ).fetchall()
        models = []
        for r in rows:
            models.append(ModelInfo(
                model_id=r["model_id"],
                provider=r["provider"],
                size_b=r["size_b"] or 0.0,
                suggested_tier=r["suggested_tier"] or "medium",
                capabilities=json.loads(r["capabilities"] or '["general"]'),
                benchmark_ms=r["benchmark_ms"] or 0,
                timeout_secs=r["timeout_secs"] or 300,
                is_available=bool(r["is_available"]),
                last_checked=r["last_checked"] or "",
            ))

        # Load tier assignments
        tier_assignments: dict[str, TierAssignment] = {}
        if "tier_assignments" in meta:
            try:
                raw = json.loads(meta["tier_assignments"])
                for provider, tiers in raw.items():
                    tier_assignments[provider] = TierAssignment(
                        light=tiers.get("light", ""),
                        medium=tiers.get("medium", ""),
                        heavy=tiers.get("heavy", ""),
                    )
            except Exception:
                pass

        result = DiscoveryResult(
            models=models,
            tier_assignments=tier_assignments,
            from_cache=True,
            completed_at=meta.get("completed_at", ""),
        )
        return result
    except Exception:
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Perspective cache — resume-on-failure + repeatability for fan-out jobs
# ---------------------------------------------------------------------------


def _cache_ttl_hours() -> int:
    import os
    return int(os.getenv("DEEP_THINK_CACHE_TTL_HOURS", "24"))


def get_perspective_cache(cache_key: str) -> Optional[dict]:
    """Return a cached perspective result if it exists and hasn't expired."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM perspective_cache WHERE cache_key=? AND expires_at > ?",
            (cache_key, now),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_perspective_cache(
    cache_key: str,
    perspective_name: str,
    final_answer: str,
    model_summary: str = "",
    passes_run: int = 1,
    job_id: str = "",
) -> None:
    """Store a perspective result. Overwrites any existing entry for this key."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(hours=_cache_ttl_hours())).isoformat()
    now_str = now.isoformat()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO perspective_cache
                (cache_key, perspective_name, model_summary, passes_run,
                 final_answer, job_id, created_at, expires_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET
                final_answer=excluded.final_answer,
                model_summary=excluded.model_summary,
                passes_run=excluded.passes_run,
                job_id=excluded.job_id,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at
            """,
            (cache_key, perspective_name, model_summary, passes_run,
             final_answer, job_id, now_str, expires),
        )
        conn.commit()
    finally:
        conn.close()




def list_perspective_cache(job_id: str = "") -> list[dict]:
    """List cached perspectives, optionally filtered by job_id."""
    conn = _connect()
    try:
        if job_id:
            rows = conn.execute(
                "SELECT cache_key, perspective_name, model_summary, passes_run, "
                "job_id, created_at, expires_at FROM perspective_cache WHERE job_id=? "
                "ORDER BY created_at",
                (job_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT cache_key, perspective_name, model_summary, passes_run, "
                "job_id, created_at, expires_at FROM perspective_cache "
                "ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pass cache — per-pass intermediate caching for mid-job resume
# ---------------------------------------------------------------------------


def get_pass_history(job_id: str, perspective: str, run_sig: str) -> list[dict]:
    """Return cached passes for this job/perspective with matching run_sig.

    Only returns the longest contiguous prefix starting at pass 1 — if pass 3
    is missing but 4 exists, returns [1, 2] to avoid skipping a required pass.
    Expired rows are excluded.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT pass_num, framing, tier, model_used, provider, output "
            "FROM pass_cache "
            "WHERE job_id=? AND perspective=? AND run_sig=? AND expires_at > ? "
            "ORDER BY pass_num ASC",
            (job_id, perspective, run_sig, now),
        ).fetchall()
    finally:
        conn.close()

    # Take the longest contiguous prefix 1, 2, 3, ... N
    result = []
    for i, row in enumerate(rows, start=1):
        if row["pass_num"] != i:
            break
        result.append(dict(row))
    return result


def get_job_pass_cache_entries(job_id: str) -> list[dict]:
    """Get all pass cache entries for a job (for atomic completion).
    
    Returns list of cache entry dicts ready to be passed to complete_job.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT job_id, perspective, pass_num, run_sig, framing, tier,
                   model_used, provider, output
            FROM pass_cache WHERE job_id=?
            ORDER BY perspective ASC, pass_num ASC
            """,
            (job_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def set_pass_cache(
    job_id: str,
    perspective: str,
    pass_num: int,
    run_sig: str,
    framing: str,
    tier: str,
    model_used: str,
    provider: str,
    output: str,
) -> None:
    """Store a single pass output. Overwrites any existing row for this (job, perspective, pass_num)."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(hours=_cache_ttl_hours())).isoformat()
    conn = _connect()
    try:
        # Use transaction to ensure atomic writes
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO pass_cache
                (job_id, perspective, pass_num, run_sig, framing, tier,
                 model_used, provider, output, created_at, expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_id, perspective, pass_num) DO UPDATE SET
                run_sig=excluded.run_sig,
                framing=excluded.framing,
                tier=excluded.tier,
                model_used=excluded.model_used,
                provider=excluded.provider,
                output=excluded.output,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at
            """,
            (job_id, perspective, pass_num, run_sig, framing, tier,
             model_used, provider, output, now.isoformat(), expires),
        )
        conn.commit()
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def get_full_reasoning_chain(job_id: str) -> list[dict]:
    """Return all pass_cache rows for a job, ordered by perspective then pass_num.

    Groups into a list of perspective dicts:
        [{"perspective": str, "passes": [{"pass_num", "framing", "tier",
                                           "model_used", "provider", "output"}, ...]}, ...]

    Perspective "" (empty string) is renamed to "main" for clarity.
    Expired rows are included — this is a forensic/reporting query, not a resume query.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT perspective, pass_num, framing, tier, model_used, provider, output "
            "FROM pass_cache WHERE job_id=? ORDER BY perspective ASC, pass_num ASC",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        key = row["perspective"] or "main"
        grouped.setdefault(key, []).append({
            "pass_num":   row["pass_num"],
            "framing":    row["framing"],
            "tier":       row["tier"],
            "model_used": row["model_used"],
            "provider":   row["provider"],
            "output":     row["output"],
        })
    return [{"perspective": k, "passes": v} for k, v in grouped.items()]


def evict_expired_pass_cache() -> int:
    """Remove expired pass cache entries. Returns count removed."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM pass_cache WHERE expires_at <= ?", (now,)
        )
        count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()



# ============================================================================
# Self-Improvement Plan Management
# ============================================================================

def create_plan(
    plan_id: str,
    finding_ids: list[str],
    plan_json: str,
    priority: float,
    effort_estimate: int,
    risk_level: str,
    deep_think_job_id: str = "",
) -> str:
    """Create a new self-improvement plan. Returns plan_id."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO self_improvement_plans
            (id, finding_ids, plan_json, priority, effort_estimate, risk_level,
             status, deep_think_job_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                plan_id,
                json.dumps(finding_ids),
                plan_json,
                priority,
                effort_estimate,
                risk_level,
                deep_think_job_id,
                now,
                now,
            ),
        )
        conn.commit()
        
        # Audit log
        audit_log("plan_created", plan_id, json.dumps({
            "finding_ids": finding_ids,
            "priority": priority,
            "effort_estimate": effort_estimate,
        }))
        
        return plan_id
    finally:
        conn.close()


def get_plan(plan_id: str) -> Optional[dict]:
    """Fetch a single plan by ID."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM self_improvement_plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_plans(
    status: str = "all",
    limit: int = 100,
    order_by: str = "priority DESC",
) -> list[dict]:
    """List plans optionally filtered by status."""
    conn = _connect()
    try:
        query = "SELECT * FROM self_improvement_plans"
        params: list = []
        
        if status != "all":
            query += " WHERE status = ?"
            params.append(status)
        
        query += f" ORDER BY {order_by} LIMIT ?"
        params.append(limit)
        
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_plan_status(plan_id: str, status: str, approved_by: str = "") -> bool:
    """Update plan status and record audit log."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        updates = f"status = ?, updated_at = ?"
        params: list = [status, now]
        
        if approved_by and status == "approved":
            updates += ", approved_by = ?, approved_at = ?"
            params.extend([approved_by, now])
        
        params.append(plan_id)
        
        conn.execute(
            f"UPDATE self_improvement_plans SET {updates} WHERE id = ?",
            params,
        )
        conn.commit()
        
        audit_log(f"plan_{status}", plan_id, json.dumps({
            "approved_by": approved_by,
        }))
        
        return True
    finally:
        conn.close()


def update_plan_validation(
    plan_id: str,
    validation_score: float,
    deployment_sha: str = "",
) -> bool:
    """Update plan with validation results."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE self_improvement_plans
            SET validation_score = ?, deployment_sha = ?, updated_at = ?
            WHERE id = ?
            """,
            (validation_score, deployment_sha, now, plan_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def audit_log(
    event_type: str,
    plan_id: str,
    details_json: str = "{}",
) -> None:
    """Log a plan audit event."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO plan_audit_log (plan_id, event_type, details_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (plan_id, event_type, details_json, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_plan_audit_trail(plan_id: str) -> list[dict]:
    """Fetch complete audit trail for a plan."""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT event_type, details_json, created_at
            FROM plan_audit_log
            WHERE plan_id = ?
            ORDER BY created_at ASC
            """,
            (plan_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
