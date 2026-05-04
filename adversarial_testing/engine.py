"""Test execution engine for the adversarial testing framework.

Responsibilities
----------------
submit_test          Queue a single adversarial test job (returns job_id).
get_test_status      Poll a job for current status.
get_test_results     Return full results including any finding.
run_batch            Submit 5-10 tests in parallel; return aggregate results.
run_regression_suite Re-test all known findings to check for regressions.
_execute_test        Internal: run one test synchronously; update the job record.

System-Under-Test
-----------------
The engine submits each adversarial input to the deep_think /testing/submit
endpoint (configurable via DEEP_THINK_BASE_URL).  When that endpoint is
unavailable (offline development, unit tests) it falls back to the
LocalTestBackend which returns canned responses for evaluation.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from . import store
from .categories import TestCategoryRegistry
from .governance import (
    AuthorizationError,
    OutputRedactor,
    RateLimiter,
    RateLimitError,
    hash_token,
    requires_human_review,
    verify_admin_token,
)
from .schema import (
    AdversarialInput,
    Category,
    Finding,
    Severity,
    TestBatchResult,
    TestJob,
    TestStatus,
)
from .self_improvement import on_finding

log = logging.getLogger(__name__)

_DEEP_THINK_BASE = os.getenv("DEEP_THINK_BASE_URL", "http://localhost:8080")
_HTTP_TIMEOUT = float(os.getenv("ADVERSARIAL_TIMEOUT", "60.0"))

_registry = TestCategoryRegistry()
_rate_limiter = RateLimiter()
_redactor = OutputRedactor()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def submit_test(
    input_payload: str,
    expected_behavior: str,
    auth_token: str,
    category: Optional[str] = None,
    attack_type: Optional[str] = None,
    regression: bool = False,
    regression_finding_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Queue one adversarial test job.

    Returns:
        {"job_id": str, "status_url": str}

    Raises:
        AuthorizationError: Invalid admin token.
        RateLimitError: Rate limit exceeded.
    """
    if not verify_admin_token(auth_token):
        raise AuthorizationError("Invalid or missing admin token.")

    _rate_limiter.check_and_acquire()

    job = TestJob(
        input=input_payload,
        expected_behavior=expected_behavior,
        status=TestStatus.QUEUED,
        category=category,
        attack_type=attack_type,
        submitter_token=hash_token(auth_token),
        regression=regression,
        regression_finding_id=regression_finding_id,
    )
    store.save_job(job)

    payload_hash = hashlib.sha256(input_payload.encode()).hexdigest()[:16]
    store.audit_log(
        event_type="test_submitted",
        job_id=job.job_id,
        submitter_token=hash_token(auth_token),
        payload_hash=payload_hash,
        details={"category": category, "attack_type": attack_type, "regression": regression},
    )

    # Launch execution in background
    asyncio.create_task(_execute_test(job))

    return {
        "job_id": job.job_id,
        "status_url": f"/testing/status/{job.job_id}",
    }


def get_test_status(job_id: str) -> Dict[str, Any]:
    """Poll a job for current status.

    Returns:
        {"job_id": str, "status": str, "progress": str}
    """
    job = store.load_job(job_id)
    if not job:
        return {"error": f"Job {job_id} not found"}
    progress = _compute_progress(job)
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "progress": progress,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def get_test_results(job_id: str) -> Dict[str, Any]:
    """Return full results for a completed job.

    Returns:
        {"findings": list, "severity_distribution": dict, "metrics": dict}
    """
    job = store.load_job(job_id)
    if not job:
        return {"error": f"Job {job_id} not found"}

    findings_out = []
    if job.finding:
        findings_out.append(_redactor.redact_finding(job.finding))

    severity_dist = {s.value: 0 for s in Severity}
    for f in findings_out:
        sev = f.get("severity", Severity.LOW.value)
        severity_dist[sev] = severity_dist.get(sev, 0) + 1

    return _redactor.redact(
        {
            "job_id": job_id,
            "status": job.status.value,
            "findings": findings_out,
            "severity_distribution": severity_dist,
            "metrics": {
                "category": job.category,
                "attack_type": job.attack_type,
                "passed": job.status == TestStatus.PASSED,
                "regression": job.regression,
            },
            "error": job.error,
        }
    )


async def run_batch(
    adversarial_inputs: List[AdversarialInput],
    auth_token: str,
) -> TestBatchResult:
    """Submit up to 10 tests in parallel and return aggregate results.

    Args:
        adversarial_inputs: List of AdversarialInput (capped at 10).
        auth_token:         Admin token for authorization.

    Returns:
        TestBatchResult with all job_ids and aggregated findings.

    Raises:
        AuthorizationError: Invalid admin token.
    """
    if not verify_admin_token(auth_token):
        raise AuthorizationError("Invalid or missing admin token.")

    batch = TestBatchResult()
    inputs = adversarial_inputs[:10]  # hard cap
    batch.total = len(inputs)

    submit_tasks = [
        submit_test(
            input_payload=inp.payload,
            expected_behavior=inp.expected_defense,
            auth_token=auth_token,
            category=inp.category.value,
            attack_type=inp.attack_type.value,
        )
        for inp in inputs
    ]
    results = await asyncio.gather(*submit_tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, Exception):
            batch.errored += 1
            log.warning("Batch submit error: %s", r)
        else:
            batch.job_ids.append(r["job_id"])

    # Wait for all submitted jobs to complete (poll with timeout)
    await _wait_for_jobs(batch.job_ids, timeout=300.0)

    # Aggregate results
    for job_id in batch.job_ids:
        job = store.load_job(job_id)
        if not job:
            batch.errored += 1
            continue
        if job.status == TestStatus.PASSED:
            batch.passed += 1
        elif job.status == TestStatus.FAILED:
            batch.failed += 1
            if job.finding:
                batch.findings.append(job.finding)
        else:
            batch.errored += 1

    batch.completed_at = datetime.now(timezone.utc)
    return batch


async def run_regression_suite(auth_token: str) -> TestBatchResult:
    """Re-test all known findings to check they remain fixed.

    Returns:
        TestBatchResult — regressions appear as FAILED findings.
    """
    if not verify_admin_token(auth_token):
        raise AuthorizationError("Invalid or missing admin token.")

    known_findings = store.list_findings(resolved=True, limit=200)
    if not known_findings:
        return TestBatchResult(total=0)

    batch = TestBatchResult()
    batch.total = len(known_findings)

    submit_tasks = [
        submit_test(
            input_payload=finding.example_input,
            expected_behavior=f"Finding {finding.id} should be fixed (no vulnerability).",
            auth_token=auth_token,
            category=finding.category.value,
            regression=True,
            regression_finding_id=finding.id,
        )
        for finding in known_findings
    ]
    results = await asyncio.gather(*submit_tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            batch.errored += 1
        else:
            batch.job_ids.append(r["job_id"])

    await _wait_for_jobs(batch.job_ids, timeout=600.0)

    for job_id in batch.job_ids:
        job = store.load_job(job_id)
        if not job:
            batch.errored += 1
            continue
        if job.status == TestStatus.PASSED:
            batch.passed += 1
        elif job.status == TestStatus.FAILED:
            batch.failed += 1
            if job.finding:
                batch.findings.append(job.finding)
        else:
            batch.errored += 1

    batch.completed_at = datetime.now(timezone.utc)
    return batch


# ---------------------------------------------------------------------------
# Internal execution
# ---------------------------------------------------------------------------


async def _execute_test(job: TestJob) -> None:
    """Run a single test: call system-under-test, evaluate, update job record."""
    job.status = TestStatus.RUNNING
    job.started_at = datetime.now(timezone.utc)
    store.save_job(job)

    try:
        raw_response = await _call_system_under_test(job.input)

        # Determine which category handler to use
        category_enum = _parse_category(job.category)
        handler = _registry.get(category_enum) if category_enum else None

        if handler:
            adv_input = AdversarialInput(
                payload=job.input,
                expected_defense=job.expected_behavior,
                attack_type=_parse_attack_type_enum(job.attack_type),
                category=category_enum,
            )
            passed, finding = handler.evaluate(adv_input, raw_response)
        else:
            # Generic evaluation: any non-error response is a pass
            passed = bool(raw_response) and "error" not in str(raw_response).lower()
            finding = None

        if finding:
            finding.test_job_id = job.job_id
            store.save_finding(finding)
            job.finding = finding
            if requires_human_review(finding):
                store.audit_log(
                    event_type="human_review_required",
                    finding_id=finding.id,
                    job_id=job.job_id,
                    details={"severity": finding.severity.value},
                )
            asyncio.create_task(on_finding(finding))

        job.status = TestStatus.PASSED if passed else TestStatus.FAILED
        job.result = raw_response
        job.completed_at = datetime.now(timezone.utc)

        # Update coverage tracking
        if job.category:
            store.increment_coverage("category", job.category)
        if job.attack_type:
            store.increment_coverage("attack_type", job.attack_type)

    except Exception as exc:  # noqa: BLE001
        log.exception("Test execution failed for job %s", job.job_id)
        job.status = TestStatus.ERROR
        job.error = str(exc)[:500]
        job.completed_at = datetime.now(timezone.utc)

    finally:
        store.save_job(job)
        _rate_limiter.release()


async def _call_system_under_test(payload: str) -> Dict[str, Any]:
    """POST the adversarial payload to deep_think and return the response dict.

    Falls back to LocalTestBackend when deep_think is unreachable.
    """
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                f"{_DEEP_THINK_BASE}/call/deep_think_async",
                json={"question": payload, "passes": 2, "task_class": "general"},
            )
            if resp.status_code == 200:
                data = resp.json()
                job_id = data.get("job_id", "")
                if job_id:
                    return await _poll_deep_think_result(client, job_id)
                return data
            return {"error": f"deep_think returned {resp.status_code}", "status_code": resp.status_code}
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        log.debug("deep_think unreachable (%s) — using local backend", exc)
        return LocalTestBackend().respond(payload)


async def _poll_deep_think_result(
    client: httpx.AsyncClient,
    job_id: str,
    max_wait: float = 120.0,
    poll_interval: float = 2.0,
) -> Dict[str, Any]:
    """Poll deep_think until the job is complete or timeout."""
    elapsed = 0.0
    while elapsed < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        try:
            resp = await client.post(
                f"{_DEEP_THINK_BASE}/call/get_thinking_result",
                json={"job_id": job_id},
            )
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status", "")
                if status in ("complete", "failed"):
                    return data
        except (httpx.HTTPError, httpx.RequestError):
            pass
    return {"error": "timeout waiting for deep_think result", "job_id": job_id}


def _compute_progress(job: TestJob) -> str:
    if job.status == TestStatus.QUEUED:
        return "Waiting in queue"
    if job.status == TestStatus.RUNNING:
        elapsed = (datetime.now(timezone.utc) - job.started_at.replace(tzinfo=timezone.utc)).seconds
        return f"Running ({elapsed}s elapsed)"
    if job.status in (TestStatus.PASSED, TestStatus.FAILED, TestStatus.ERROR):
        return "Complete"
    return job.status.value


async def _wait_for_jobs(job_ids: List[str], timeout: float = 300.0) -> None:
    """Wait until all jobs reach a terminal status or timeout expires."""
    terminal = {TestStatus.PASSED, TestStatus.FAILED, TestStatus.ERROR}
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        jobs = [store.load_job(jid) for jid in job_ids]
        if all(j and j.status in terminal for j in jobs if j):
            return
        await asyncio.sleep(1.0)


def _parse_category(category_str: Optional[str]) -> Optional[Category]:
    if not category_str:
        return None
    try:
        return Category(category_str)
    except ValueError:
        return None


def _parse_attack_type_enum(attack_type_str: Optional[str]):
    from .schema import AttackType
    if not attack_type_str:
        return AttackType.FALSE_PREMISE
    try:
        return AttackType(attack_type_str)
    except ValueError:
        return AttackType.FALSE_PREMISE


# ---------------------------------------------------------------------------
# Local test backend (offline / unit test fallback)
# ---------------------------------------------------------------------------


class LocalTestBackend:
    """Returns deterministic canned responses for offline testing.

    Simulates how deep_think would respond to adversarial inputs
    without requiring a live deep_think instance.
    """

    _REJECTION_TRIGGERS = {
        "1922": "incorrect — Einstein published special relativity in 1905",
        "384 km": "incorrect — the Moon is approximately 384,400 km from Earth",
        "80°c": "incorrect — water boils at 100°C at sea level",
        "circular": "This appears to be circular reasoning",
        "ignore.*instruction": "I cannot comply with that request",
        "base64": "I detected an encoded bypass attempt",
        "DAN": "I cannot adopt an unrestricted persona",
    }

    def respond(self, payload: str) -> Dict[str, Any]:
        import re

        for pattern, response in self._REJECTION_TRIGGERS.items():
            if re.search(pattern, payload, re.IGNORECASE):
                return {
                    "final_answer": response,
                    "status": "complete",
                    "source": "local_backend",
                }

        if not payload or not payload.strip():
            return {
                "error": "Input cannot be empty",
                "status": "error",
                "source": "local_backend",
            }

        return {
            "final_answer": f"Processed: {payload[:100]}",
            "status": "complete",
            "source": "local_backend",
        }
