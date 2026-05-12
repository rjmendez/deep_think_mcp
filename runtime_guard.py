"""Runtime/build drift guardrails.

Detects when a running process is older than critical code changes and provides
runtime fingerprint + invariant checks for health surfaces.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROCESS_STARTED_AT_UTC = datetime.now(timezone.utc)
PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent

CRITICAL_FILES = (
    "server.py",
    "worker.py",
    "store.py",
    "api/reasoning.py",
    "engine/orchestrator.py",
)


@dataclass(frozen=True)
class RuntimeFingerprint:
    process_started_at: str
    process_started_epoch: float
    newest_code_mtime: str
    newest_code_epoch: float
    newest_code_path: str
    runtime_stale: bool
    git_sha: str
    git_dirty: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "process_started_at": self.process_started_at,
            "process_started_epoch": self.process_started_epoch,
            "newest_code_mtime": self.newest_code_mtime,
            "newest_code_epoch": self.newest_code_epoch,
            "newest_code_path": self.newest_code_path,
            "runtime_stale": self.runtime_stale,
            "git_sha": self.git_sha,
            "git_dirty": self.git_dirty,
        }


def _git_info() -> tuple[str, bool]:
    try:
        sha = subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        sha = "unknown"
    try:
        dirty = bool(
            subprocess.check_output(
                ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        )
    except Exception:
        dirty = False
    return sha, dirty


def _critical_file_mtimes() -> list[tuple[Path, float]]:
    mtimes: list[tuple[Path, float]] = []
    for rel in CRITICAL_FILES:
        p = PACKAGE_ROOT / rel
        if p.exists():
            mtimes.append((p, p.stat().st_mtime))
    return mtimes


def get_runtime_fingerprint() -> RuntimeFingerprint:
    mtimes = _critical_file_mtimes()
    if mtimes:
        newest_path, newest_epoch = max(mtimes, key=lambda t: t[1])
    else:
        newest_path, newest_epoch = PACKAGE_ROOT, PROCESS_STARTED_AT_UTC.timestamp()
    git_sha, git_dirty = _git_info()
    started_epoch = PROCESS_STARTED_AT_UTC.timestamp()
    runtime_stale = newest_epoch > started_epoch
    newest_dt = datetime.fromtimestamp(newest_epoch, tz=timezone.utc).isoformat()
    return RuntimeFingerprint(
        process_started_at=PROCESS_STARTED_AT_UTC.isoformat(),
        process_started_epoch=started_epoch,
        newest_code_mtime=newest_dt,
        newest_code_epoch=newest_epoch,
        newest_code_path=str(newest_path),
        runtime_stale=runtime_stale,
        git_sha=git_sha,
        git_dirty=git_dirty,
    )


def stale_runtime_error() -> dict[str, Any] | None:
    fp = get_runtime_fingerprint()
    if not fp.runtime_stale:
        return None
    return {
        "status": "failed",
        "error": (
            "RUNTIME_STALE: running process predates critical code changes. "
            "Refusing new jobs until service restart."
        ),
        "restart_required": True,
        "runtime_fingerprint": fp.as_dict(),
    }


def check_recent_fanout_invariants(db_connect_fn, limit: int = 20) -> dict[str, Any]:
    """Check recently completed fan-out jobs for contract invariants."""
    required_fanout_keys = {
        "tools_invoked_total",
        "tool_successes_total",
        "inference_only",
        "perspective_outputs",
        "perspectives",
    }
    violations: list[dict[str, Any]] = []
    checked = 0
    conn: sqlite3.Connection = db_connect_fn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT job_id, status, result
            FROM thinking_jobs
            WHERE status IN ('complete', 'failed')
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        for row in rows:
            result_raw = row["result"]
            if not result_raw:
                continue
            try:
                result = json.loads(result_raw)
            except Exception:
                continue
            if not isinstance(result, dict) or result.get("type") != "fan_out":
                continue
            checked += 1
            expected = str(result.get("status")) if result.get("status") is not None else None
            persisted = str(row["status"])
            missing = sorted(k for k in required_fanout_keys if k not in result)
            mismatch = expected is not None and expected != persisted
            if mismatch or missing:
                violations.append(
                    {
                        "job_id": row["job_id"],
                        "db_status": persisted,
                        "result_status": expected,
                        "status_mismatch": mismatch,
                        "missing_keys": missing,
                    }
                )
    finally:
        conn.close()
    return {
        "checked": checked,
        "violations": violations,
        "violations_count": len(violations),
    }
