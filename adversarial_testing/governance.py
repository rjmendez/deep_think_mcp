"""Governance: authorization, rate limiting, output redaction, and audit logging.

Responsibilities
----------------
verify_admin_token      Constant-time token verification against env var.
RateLimiter             Enforces 20 concurrent / 100-per-day limits.
OutputRedactor          Strips internal details before returning to callers.
requires_human_review   Gate: returns True for findings that need human sign-off.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict

from . import store
from .schema import Finding, Severity

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MAX_CONCURRENT = int(os.getenv("ADVERSARIAL_MAX_CONCURRENT", "20"))
_MAX_PER_DAY = int(os.getenv("ADVERSARIAL_MAX_PER_DAY", "100"))

# Patterns to redact from output (avoids leaking internal state)
_REDACT_PATTERNS = [
    "ADVERSARIAL_ADMIN_TOKEN",
    "ABLITERATION_API_KEY",
    "GITHUB_COPILOT_OAUTH_TOKEN",
    "system_prompt",
    "internal_error",
    "traceback",
    "Traceback",
]


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def verify_admin_token(token: str) -> bool:
    """Constant-time comparison of provided token against configured admin token.

    The expected token is read from ADVERSARIAL_ADMIN_TOKEN env var.
    Returns False if the env var is unset (deny-by-default).
    """
    expected = os.getenv("ADVERSARIAL_ADMIN_TOKEN", "")
    if not expected:
        log.warning(
            "ADVERSARIAL_ADMIN_TOKEN not set — all adversarial testing requests will be denied"
        )
        return False
    return hmac.compare_digest(
        hashlib.sha256(token.encode()).hexdigest(),
        hashlib.sha256(expected.encode()).hexdigest(),
    )


def hash_token(token: str) -> str:
    """One-way hash of a token for audit log storage (never store raw tokens)."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Thread-safe rate limiter with in-process concurrent tracking + SQLite daily limit.

    Limits:
        max_concurrent  — number of simultaneously RUNNING tests (default 20)
        max_per_day     — maximum submissions in a 24-hour calendar day (default 100)
    """

    def __init__(
        self,
        max_concurrent: int = _MAX_CONCURRENT,
        max_per_day: int = _MAX_PER_DAY,
    ) -> None:
        self._max_concurrent = max_concurrent
        self._max_per_day = max_per_day
        self._lock = threading.Lock()
        self._active: int = 0  # in-process concurrent counter

    def check_and_acquire(self) -> None:
        """Check limits and increment the concurrent counter.

        Raises:
            RateLimitError: If either limit would be exceeded.
        """
        # Daily limit (SQLite-backed — survives restarts)
        daily = store.get_and_increment_rate_window("daily_submissions", 86400)
        if daily > self._max_per_day:
            # Roll back the increment
            store.get_and_increment_rate_window("daily_submissions_rollback", 1)
            raise RateLimitError(
                f"Daily test limit reached ({self._max_per_day}/day). "
                "Try again tomorrow."
            )

        # Concurrent limit (in-process)
        with self._lock:
            running = store.count_running_jobs()
            if running >= self._max_concurrent:
                raise RateLimitError(
                    f"Too many concurrent tests ({running}/{self._max_concurrent}). "
                    "Wait for existing tests to complete."
                )
            self._active += 1

    def release(self) -> None:
        """Decrement the concurrent counter when a test finishes."""
        with self._lock:
            self._active = max(0, self._active - 1)

    def current_active(self) -> int:
        with self._lock:
            return self._active


# ---------------------------------------------------------------------------
# Output redactor
# ---------------------------------------------------------------------------


class OutputRedactor:
    """Strips sensitive internal details from outbound API responses."""

    def redact(self, data: Any) -> Any:
        """Recursively redact sensitive fields from a dict, list, or string."""
        if isinstance(data, str):
            return self._redact_string(data)
        if isinstance(data, dict):
            return {k: self.redact(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self.redact(item) for item in data]
        return data

    def _redact_string(self, text: str) -> str:
        for pattern in _REDACT_PATTERNS:
            if pattern in text:
                # Replace everything after the pattern key up to next whitespace/quote
                import re
                text = re.sub(
                    rf"{re.escape(pattern)}[=:\s][\S]+",
                    f"{pattern}=[REDACTED]",
                    text,
                )
        return text

    def redact_finding(self, finding: Finding) -> Dict[str, Any]:
        """Convert a finding to a safe dict with sensitive fields redacted."""
        return self.redact(
            {
                "id": finding.id,
                "severity": finding.severity.value,
                "category": finding.category.value,
                "reproducibility": finding.reproducibility.value,
                "impact": finding.impact,
                "mitigation": finding.mitigation,
                "example_input": finding.example_input[:200] + "…"
                if len(finding.example_input) > 200
                else finding.example_input,
                "test_job_id": finding.test_job_id,
                "created_at": finding.created_at.isoformat(),
                "confirmed_at": finding.confirmed_at.isoformat()
                if finding.confirmed_at
                else None,
                "fixed_at": finding.fixed_at.isoformat() if finding.fixed_at else None,
                "false_positive": finding.false_positive,
            }
        )


# ---------------------------------------------------------------------------
# Human review gate
# ---------------------------------------------------------------------------


def requires_human_review(finding: Finding) -> bool:
    """Return True when a finding must pass human review before auto-remediation.

    Policy:
        CRITICAL severity always requires human review.
        HIGH severity requires review if reproducibility is ALWAYS.
    """
    if finding.severity == Severity.CRITICAL:
        return True
    if finding.severity == Severity.HIGH and finding.reproducibility.value == "always":
        return True
    return False


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AuthorizationError(Exception):
    """Raised when a request lacks a valid admin token."""


class RateLimitError(Exception):
    """Raised when a rate limit is exceeded."""
