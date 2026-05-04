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
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


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
                claimed_by           TEXT,
                claimed_at           TEXT,
                completed_at         TEXT,
                result               TEXT,
                error                TEXT
            )
            """
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


def claim_next_job(worker_id: str = "default") -> Optional[dict]:
    """Atomically claim the oldest queued job. Returns the job dict or None."""
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
            "UPDATE thinking_jobs SET status='running', started_at=?, claimed_by=?, claimed_at=? WHERE job_id=?",
            (now, worker_id, now, row["job_id"]),
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
    conn = _connect()
    try:
        conn.execute(
            "UPDATE thinking_jobs SET status='failed', error=?, completed_at=? WHERE job_id=?",
            (error, now, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def requeue_stale(stale_after_minutes: int = 0) -> int:
    """Reset timed-out 'running' jobs to 'queued'. Call on startup for crash recovery.

    Uses a time-based cutoff (default DEEP_THINK_STALE_JOB_MINUTES, fallback 120 min)
    so concurrent worker processes only requeue genuinely abandoned jobs, not each
    other's actively-running work.
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
            "WHERE status='running' AND started_at < ?",
            (cutoff,),
        )
        count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def detect_orphaned_jobs(stale_after_minutes: int = 0) -> list[dict]:
    """Detect jobs stuck in 'running' state for longer than threshold.
    
    Returns list of orphaned job dicts that should be requeued.
    Uses DEEP_THINK_ORPHAN_TIMEOUT_MINUTES env var (default 5 min) for background
    watchdog detection. This is separate from DEEP_THINK_STALE_JOB_MINUTES (120 min)
    used only at startup for crash recovery.
    """
    import os
    from datetime import timedelta
    
    if stale_after_minutes <= 0:
        stale_after_minutes = int(os.getenv("DEEP_THINK_ORPHAN_TIMEOUT_MINUTES", "5"))
    
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
    """Requeue an orphaned job by resetting its status to 'pending'.
    
    Args:
        job_id: The job ID to requeue
        reason: The reason for requeue (for logging purposes)
        
    Returns:
        True if the job was requeued, False if not found
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE thinking_jobs SET status='pending', started_at=NULL, claimed_by=NULL, claimed_at=NULL "
            "WHERE job_id=? AND status='running'",
            (job_id,),
        )
        conn.commit()
        return cur.rowcount > 0
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


def evict_expired_cache() -> int:
    """Remove expired perspective cache entries. Returns count removed."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM perspective_cache WHERE expires_at <= ?", (now,)
        )
        count = cur.rowcount
        conn.commit()
        return count
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

