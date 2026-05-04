"""Health check endpoint and metrics for deep_think_mcp.

This module provides fast health metrics with minimal DB overhead:
- pending_count: quick count of queued jobs
- avg_latency: average duration of completed jobs (cached)
- last_success_timestamp: when the last job completed
- worker_count: number of active workers
- db_status: database connectivity check

Metrics are cached for fast (<100ms) responses.
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# Metrics cache with TTL
_CACHE_TTL = 10  # seconds
_CACHE: dict = {
    "timestamp": 0,
    "pending_count": 0,
    "avg_latency": 0,
    "last_success_timestamp": None,
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
            "avg_latency": 0,
            "last_success_timestamp": None,
            "worker_count": 0,
            "db_status": "unavailable",
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
            "avg_latency": 0,
            "last_success_timestamp": None,
            "worker_count": 0,
            "db_status": "error",
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
    
    # Count pending jobs (fast: index scan)
    row = conn.execute(
        "SELECT COUNT(*) as count FROM thinking_jobs WHERE status='queued'"
    ).fetchone()
    pending_count = row["count"] if row else 0
    
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
        "avg_latency": avg_latency,
        "last_success_timestamp": last_success_timestamp,
        "completed_count": completed_count,
        "db_status": "healthy",
    }


def _build_health_response(metrics: dict, max_pending_threshold: int) -> dict:
    """Build health response from metrics."""
    pending_count = metrics["pending_count"]
    
    # Determine health status
    is_healthy = pending_count < max_pending_threshold
    status = "healthy" if is_healthy else "degraded"
    http_status = 200 if is_healthy else 503
    
    response = {
        "status": status,
        "http_status": http_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pending_count": pending_count,
        "avg_latency": metrics["avg_latency"],
        "last_success_timestamp": metrics["last_success_timestamp"],
        "worker_count": 1,  # Simplified: assume 1 active worker (can be enhanced)
        "db_status": metrics["db_status"],
        "completed_count": metrics.get("completed_count", 0),
    }
    
    if not is_healthy:
        response["reason"] = f"Too many pending jobs ({pending_count} >= {max_pending_threshold})"
    
    return response


def reset_cache() -> None:
    """Reset health cache. Useful for testing."""
    global _CACHE
    _CACHE = {
        "timestamp": 0,
        "pending_count": 0,
        "avg_latency": 0,
        "last_success_timestamp": None,
        "completed_count": 0,
        "db_status": "unknown",
    }
