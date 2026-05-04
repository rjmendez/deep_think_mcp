"""adversarial_testing — adversarial robustness testing framework for Nova/deep-think.

Public API::

    from deep_think_mcp.adversarial_testing import (
        submit_test_job,
        get_job_status,
        get_job_results,
        get_coverage,
        get_metrics,
        submit_batch,
        submit_regression_suite,
    )

Schema types::

    from deep_think_mcp.adversarial_testing.schema import (
        Finding, TestJob, AdversarialInput, Severity, Category,
        Reproducibility, TestStatus, MetricsSnapshot, CoverageReport,
    )
"""

from .api import (
    get_coverage,
    get_job_results,
    get_job_status,
    get_metrics,
    submit_batch,
    submit_regression_suite,
    submit_test_job,
)
from .schema import (
    AdversarialInput,
    AttackType,
    Category,
    CoverageReport,
    Finding,
    MetricsSnapshot,
    Reproducibility,
    Severity,
    TestBatchResult,
    TestJob,
    TestStatus,
)
from . import store

def init() -> None:
    """Initialise the adversarial testing database (call once at startup)."""
    store.init_db()


__all__ = [
    # API handlers
    "submit_test_job",
    "get_job_status",
    "get_job_results",
    "get_coverage",
    "get_metrics",
    "submit_batch",
    "submit_regression_suite",
    # Lifecycle
    "init",
    # Schema
    "AdversarialInput",
    "AttackType",
    "Category",
    "CoverageReport",
    "Finding",
    "MetricsSnapshot",
    "Reproducibility",
    "Severity",
    "TestBatchResult",
    "TestJob",
    "TestStatus",
]
