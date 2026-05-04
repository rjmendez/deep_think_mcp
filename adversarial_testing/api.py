"""HTTP API handlers for the adversarial testing framework.

Maps the REST API contract to the engine and metrics layers:

  POST /testing/submit            → submit_test_job()
  GET  /testing/status/<job_id>   → get_job_status()
  GET  /testing/results/<job_id>  → get_job_results()
  GET  /testing/coverage          → get_coverage()

These functions are framework-agnostic (no FastAPI/Flask imports).
They can be wired into any ASGI/WSGI framework or called directly.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .engine import (
    get_test_results,
    get_test_status,
    run_batch,
    run_regression_suite,
    submit_test,
)
from .generator import AbliterationClient
from .governance import AuthorizationError, OutputRedactor, RateLimitError
from .metrics import MetricsCollector
from .schema import Category

log = logging.getLogger(__name__)

_redactor = OutputRedactor()
_metrics = MetricsCollector()


# ---------------------------------------------------------------------------
# POST /testing/submit
# ---------------------------------------------------------------------------


async def submit_test_job(
    input_payload: str,
    expected_behavior: str,
    auth_token: str,
    category: Optional[str] = None,
    attack_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Queue a single adversarial test job.

    Returns:
        {"job_id": str, "status_url": str}
    or on error:
        {"error": str, "code": str}
    """
    try:
        result = await submit_test(
            input_payload=input_payload,
            expected_behavior=expected_behavior,
            auth_token=auth_token,
            category=category,
            attack_type=attack_type,
        )
        return result
    except AuthorizationError as exc:
        return {"error": str(exc), "code": "UNAUTHORIZED"}
    except RateLimitError as exc:
        return {"error": str(exc), "code": "RATE_LIMITED"}
    except Exception as exc:  # noqa: BLE001
        log.exception("submit_test_job failed")
        return {"error": "Internal error", "code": "INTERNAL_ERROR"}


# ---------------------------------------------------------------------------
# GET /testing/status/<job_id>
# ---------------------------------------------------------------------------


def get_job_status(job_id: str) -> Dict[str, Any]:
    """Return current status of a test job.

    Returns:
        {"job_id": str, "status": str, "progress": str}
    """
    return _redactor.redact(get_test_status(job_id))


# ---------------------------------------------------------------------------
# GET /testing/results/<job_id>
# ---------------------------------------------------------------------------


def get_job_results(job_id: str) -> Dict[str, Any]:
    """Return full results for a completed test job.

    Returns:
        {"findings": list, "severity_distribution": dict, "metrics": dict}
    """
    return _redactor.redact(get_test_results(job_id))


# ---------------------------------------------------------------------------
# GET /testing/coverage
# ---------------------------------------------------------------------------


def get_coverage() -> Dict[str, Any]:
    """Return test coverage report.

    Returns:
        {"test_count": int, "property_coverage": dict, "finding_trends": list}
    """
    report = _metrics.coverage_report()
    return _redactor.redact(
        {
            "test_count": report.test_count,
            "category_coverage": report.category_coverage,
            "attack_coverage": report.attack_coverage,
            "property_coverage": report.property_coverage,
            "finding_trends": report.finding_trends,
            "endpoint_coverage": report.endpoint_coverage,
        }
    )


# ---------------------------------------------------------------------------
# GET /testing/metrics
# ---------------------------------------------------------------------------


def get_metrics() -> Dict[str, Any]:
    """Return dashboard metrics snapshot."""
    snap = _metrics.snapshot()
    return _redactor.redact(
        {
            "test_coverage_pct": snap.test_coverage_pct,
            "finding_rate": snap.finding_rate,
            "pass_rate_pct": snap.pass_rate_pct,
            "avg_time_to_fix_days": snap.avg_time_to_fix_days,
            "false_positive_rate_pct": snap.false_positive_rate_pct,
            "open_findings": snap.open_findings,
            "critical_findings": snap.critical_findings,
            "total_tests_run": snap.total_tests_run,
            "total_findings": snap.total_findings,
            "severity_distribution": snap.severity_distribution,
            "captured_at": snap.captured_at.isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# POST /testing/batch
# ---------------------------------------------------------------------------


async def submit_batch(
    category: str,
    count: int,
    auth_token: str,
) -> Dict[str, Any]:
    """Generate and submit a batch of adversarial inputs for a category.

    Returns:
        {"batch_id": str, "job_ids": list, "total": int}
    """
    try:
        try:
            cat = Category(category)
        except ValueError:
            return {"error": f"Unknown category: {category}", "code": "INVALID_CATEGORY"}

        generator = AbliterationClient()
        inputs = await generator.generate(cat, min(count, 10))

        batch = await run_batch(inputs, auth_token)
        return _redactor.redact(
            {
                "batch_id": batch.batch_id,
                "job_ids": batch.job_ids,
                "total": batch.total,
                "passed": batch.passed,
                "failed": batch.failed,
                "errored": batch.errored,
                "findings": [_redactor.redact_finding(f) for f in batch.findings],
            }
        )
    except AuthorizationError as exc:
        return {"error": str(exc), "code": "UNAUTHORIZED"}
    except RateLimitError as exc:
        return {"error": str(exc), "code": "RATE_LIMITED"}
    except Exception as exc:  # noqa: BLE001
        log.exception("submit_batch failed")
        return {"error": "Internal error", "code": "INTERNAL_ERROR"}


# ---------------------------------------------------------------------------
# POST /testing/regression
# ---------------------------------------------------------------------------


async def submit_regression_suite(auth_token: str) -> Dict[str, Any]:
    """Run the full regression suite against all known resolved findings.

    Returns:
        {"batch_id": str, "regressions": int, "passed": int}
    """
    try:
        batch = await run_regression_suite(auth_token)
        return _redactor.redact(
            {
                "batch_id": batch.batch_id,
                "total": batch.total,
                "passed": batch.passed,
                "regressions": batch.failed,
                "errored": batch.errored,
                "regression_findings": [_redactor.redact_finding(f) for f in batch.findings],
            }
        )
    except AuthorizationError as exc:
        return {"error": str(exc), "code": "UNAUTHORIZED"}
    except Exception as exc:  # noqa: BLE001
        log.exception("submit_regression_suite failed")
        return {"error": "Internal error", "code": "INTERNAL_ERROR"}
