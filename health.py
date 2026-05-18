"""Health check endpoint and metrics for deep_think_mcp.

This module provides fast health metrics with minimal DB overhead:
- pending_count: quick count of queued jobs
- avg_latency: average duration of completed jobs (cached)
- last_success_timestamp: when the last job completed
- worker_count: number of active workers
- db_status: database connectivity check

Metrics are cached for fast (<100ms) responses.
"""

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

from . import metrics as runtime_metrics
from . import runtime_guard

log = logging.getLogger(__name__)

# Metrics cache with TTL
_CACHE_TTL = 10  # seconds
_CACHE: dict = {
    "timestamp": 0,
    "pending_count": 0,
    "running_count": 0,
    "failed_count": 0,
    "avg_latency": 0,
    "last_success_timestamp": None,
    "oldest_queued_age_secs": None,
    "oldest_running_age_secs": None,
    "completed_count": 0,
    "db_status": "unknown",
}
_CACHE_LOCK = None  # Will be initialized when needed


def get_health_metrics(db_connection_fn, max_pending_threshold: int = 100) -> dict:
    """Get health metrics for the service.
    
    Args:
        db_connection_fn: Callable that returns a sqlite3 connection
        max_pending_threshold: Number of pending jobs that triggers degraded status
        
    Returns:
        dict with health status and metrics
    """
    now = time.time()
    
    # Use cached metrics if fresh (<10 seconds old)
    if _CACHE["timestamp"] > 0 and (now - _CACHE["timestamp"]) < _CACHE_TTL:
        return _build_health_response(_CACHE, max_pending_threshold)
    
    # Fetch fresh metrics
    try:
        conn = db_connection_fn()
        metrics = _fetch_metrics(conn)
        conn.close()
        
        # Update cache
        _CACHE.update(metrics)
        _CACHE["timestamp"] = now
        _CACHE["db_status"] = "healthy"
        
    except sqlite3.DatabaseError as e:
        log.error(f"Database error in health check: {e}")
        _CACHE["db_status"] = "unavailable"
        return {
            "status": "degraded",
            "http_status": 503,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pending_count": 0,
            "running_count": 0,
            "failed_count": 0,
            "avg_latency": 0,
            "last_success_timestamp": None,
            "oldest_queued_age_secs": None,
            "oldest_running_age_secs": None,
            "worker_count": 0,
            "db_status": "unavailable",
            "completed_count": 0,
            "timeout_count": 0,
            "timeout_by_component": {},
            "orphaned_jobs_detected": 0,
            "orphaned_jobs_requeued": 0,
            "reason": "Database connection failed",
        }
    except Exception as e:
        log.error(f"Unexpected error in health check: {e}")
        _CACHE["db_status"] = "error"
        return {
            "status": "degraded",
            "http_status": 503,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pending_count": 0,
            "running_count": 0,
            "failed_count": 0,
            "avg_latency": 0,
            "last_success_timestamp": None,
            "oldest_queued_age_secs": None,
            "oldest_running_age_secs": None,
            "worker_count": 0,
            "db_status": "error",
            "completed_count": 0,
            "timeout_count": 0,
            "timeout_by_component": {},
            "orphaned_jobs_detected": 0,
            "orphaned_jobs_requeued": 0,
            "reason": f"Health check failed: {str(e)}",
        }
    
    return _build_health_response(_CACHE, max_pending_threshold)


def _fetch_metrics(conn: sqlite3.Connection) -> dict:
    """Fetch metrics from database.
    
    Uses lightweight queries:
    - COUNT(*) for pending jobs (index scan only)
    - AVG on completed jobs (aggregate)
    - MAX on completion timestamp
    
    No full row fetches or complex joins.
    """
    conn.row_factory = sqlite3.Row
    
    # Queue state overview
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) as pending_count,
            SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) as running_count,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed_count,
            MIN(CASE WHEN status='queued' THEN created_at END) as oldest_queued_created,
            MIN(CASE WHEN status='running' THEN claimed_at END) as oldest_running_claimed
        FROM thinking_jobs
        """
    ).fetchone()
    pending_count = (row["pending_count"] if row else 0) or 0
    running_count = (row["running_count"] if row else 0) or 0
    failed_count = (row["failed_count"] if row else 0) or 0
    oldest_queued_age_secs = _age_seconds(row["oldest_queued_created"] if row else None)
    oldest_running_age_secs = _age_seconds(row["oldest_running_claimed"] if row else None)
    
    # Get average latency for completed jobs
    row = conn.execute(
        """
        SELECT 
            AVG(CAST((julianday(completed_at) - julianday(created_at)) * 86400 AS REAL)) as avg_secs,
            MAX(completed_at) as last_success,
            COUNT(*) as total_completed
        FROM thinking_jobs 
        WHERE status='complete' AND completed_at IS NOT NULL
        """
    ).fetchone()
    
    avg_latency = 0
    last_success_timestamp = None
    completed_count = 0
    
    if row:
        avg_latency = round(row["avg_secs"], 2) if row["avg_secs"] else 0
        last_success_timestamp = row["last_success"]
        completed_count = row["total_completed"]
    
    return {
        "pending_count": pending_count,
        "running_count": running_count,
        "failed_count": failed_count,
        "avg_latency": avg_latency,
        "last_success_timestamp": last_success_timestamp,
        "oldest_queued_age_secs": oldest_queued_age_secs,
        "oldest_running_age_secs": oldest_running_age_secs,
        "completed_count": completed_count,
        "db_status": "healthy",
    }


def _age_seconds(timestamp: Optional[str]) -> Optional[float]:
    if not timestamp:
        return None
    try:
        ts = datetime.fromisoformat(timestamp)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - ts).total_seconds(), 2)
    except (TypeError, ValueError):
        return None


def _build_health_response(metrics: dict, max_pending_threshold: int) -> dict:
    """Build health response from metrics."""
    pending_count = metrics["pending_count"]
    runtime = runtime_metrics.get_metrics()
    worker_runtime = _get_worker_runtime()
    fingerprint = runtime_guard.get_runtime_fingerprint().as_dict()
    runtime_stale = bool(fingerprint.get("runtime_stale"))
    
    # Determine health status
    is_healthy = pending_count < max_pending_threshold and not runtime_stale
    status = "healthy" if is_healthy else "degraded"
    http_status = 200 if is_healthy else 503
    
    response = {
        "status": status,
        "http_status": http_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pending_count": pending_count,
        "running_count": metrics.get("running_count", 0),
        "failed_count": metrics.get("failed_count", 0),
        "avg_latency": metrics["avg_latency"],
        "last_success_timestamp": metrics["last_success_timestamp"],
        "oldest_queued_age_secs": metrics.get("oldest_queued_age_secs"),
        "oldest_running_age_secs": metrics.get("oldest_running_age_secs"),
        "worker_count": worker_runtime.get("active_workers", 0),
        "db_status": metrics["db_status"],
        "completed_count": metrics.get("completed_count", 0),
        "timeout_count": runtime.timeout_count,
        "timeout_by_component": dict(runtime.timeout_by_component),
        "orphaned_jobs_detected": runtime.orphaned_jobs_detected,
        "orphaned_jobs_requeued": runtime.orphaned_jobs_requeued,
        "runtime_stale": runtime_stale,
        "runtime_fingerprint": fingerprint,
    }
    
    if runtime_stale:
        response["reason"] = "Runtime is stale: code changed since process start; restart required"
    elif not is_healthy:
        response["reason"] = f"Too many pending jobs ({pending_count} >= {max_pending_threshold})"
    
    return response


def _get_worker_runtime() -> dict:
    try:
        from . import worker as runtime_worker
        return runtime_worker.get_worker_runtime()
    except Exception:
        return {"active_workers": 0, "max_workers": 0, "running": False}


def reset_cache() -> None:
    """Reset health cache. Useful for testing."""
    global _CACHE
    _CACHE = {
        "timestamp": 0,
        "pending_count": 0,
        "running_count": 0,
        "failed_count": 0,
        "avg_latency": 0,
        "last_success_timestamp": None,
        "oldest_queued_age_secs": None,
        "oldest_running_age_secs": None,
        "completed_count": 0,
        "db_status": "unknown",
    }
