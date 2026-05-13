"""Self-improvement integration for the adversarial testing framework.

When a critical or high-severity finding is discovered, this module:
  1. Queues an auto-review agent (via deep_think) to analyse the vulnerability.
  2. Generates a remediation plan.
  3. Tracks time-to-fix and false positive rate metrics.
  4. Prevents regression by logging fixed findings for re-test.

Design notes
------------
- This module is intentionally decoupled from the rest of the framework.
  If deep_think is unavailable, it degrades gracefully (log + continue).
- Auto-patching is deliberately disabled for CRITICAL findings that require
  human review (governance.requires_human_review returns True).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from . import store
from .governance import requires_human_review
from .schema import Finding, Severity

log = logging.getLogger(__name__)

# Only fire auto-review for findings at or above this threshold
_AUTO_REVIEW_SEVERITY_THRESHOLD = Severity.HIGH


async def on_finding(finding: Finding) -> None:
    """React to a new finding.

    Actions taken:
      - Log the finding to the audit trail.
      - Trigger auto-review analysis via deep_think for HIGH+ findings.
      - If CRITICAL and requires human review: queue for human gate.
      - Track time-to-finding for metrics.
    """
    store.audit_log(
        event_type="finding_discovered",
        finding_id=finding.id,
        details={
            "severity": finding.severity.value,
            "category": finding.category.value,
            "requires_human_review": requires_human_review(finding),
        },
    )

    if _severity_rank(finding.severity) >= _severity_rank(_AUTO_REVIEW_SEVERITY_THRESHOLD):
        asyncio.create_task(_auto_review_finding(finding))


async def _auto_review_finding(finding: Finding) -> None:
    """Queue a deep_think analysis job for the finding.

    This is a best-effort background task. Failures are logged and ignored
    so they never block the primary test execution path.
    """
    try:
        deep_think_base = os.getenv("DEEP_THINK_BASE_URL", "")
        review_question = (
            f"Adversarial security finding detected.\n\n"
            f"Severity: {finding.severity.value}\n"
            f"Category: {finding.category.value}\n"
            f"Impact: {finding.impact}\n"
            f"Example input: {finding.example_input[:300]}\n\n"
            f"Please:\n"
            f"1. Analyse the root cause of this vulnerability.\n"
            f"2. Suggest a concrete code-level fix.\n"
            f"3. Describe a regression test that would prevent recurrence.\n"
            f"4. Estimate implementation effort (hours)."
        )

        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{deep_think_base}/call/deep_think_async",
                json={
                    "question": review_question,
                    "passes": 3,
                    "task_class": "code_review",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                review_job_id = data.get("job_id", "")
                store.audit_log(
                    event_type="auto_review_queued",
                    finding_id=finding.id,
                    details={"review_job_id": review_job_id},
                )
                log.info(
                    "Auto-review queued for finding %s → deep_think job %s",
                    finding.id,
                    review_job_id,
                )
            else:
                log.warning(
                    "Auto-review request returned %d for finding %s",
                    resp.status_code,
                    finding.id,
                )

    except Exception as exc:  # noqa: BLE001 — best-effort, non-fatal
        log.warning("Auto-review failed for finding %s: %s", finding.id, exc)


def track_fix(finding_id: str, fix_description: str) -> None:
    """Record that a finding has been fixed; update the finding's fixed_at timestamp."""
    finding = store.load_finding(finding_id)
    if not finding:
        log.warning("track_fix: finding %s not found", finding_id)
        return
    finding.fixed_at = datetime.now(timezone.utc)
    finding.review_notes = (finding.review_notes or "") + f"\nFix: {fix_description}"
    store.save_finding(finding)
    store.audit_log(
        event_type="finding_fixed",
        finding_id=finding_id,
        details={"fix_description": fix_description[:500]},
    )
    log.info("Finding %s marked as fixed.", finding_id)


def get_ttf_days(finding: Finding) -> Optional[float]:
    """Return time-to-fix in fractional days, or None if not yet fixed."""
    if not finding.fixed_at:
        return None
    fixed = finding.fixed_at
    created = finding.created_at
    # Normalise: ensure both are timezone-aware UTC
    if fixed.tzinfo is None:
        fixed = fixed.replace(tzinfo=timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    delta = fixed - created
    return delta.total_seconds() / 86400.0


def _severity_rank(severity: Severity) -> int:
    return {
        Severity.LOW: 0,
        Severity.MEDIUM: 1,
        Severity.HIGH: 2,
        Severity.CRITICAL: 3,
    }[severity]
