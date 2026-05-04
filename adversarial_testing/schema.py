"""Data models for the adversarial testing framework.

Defines the canonical types used across all framework components:
  - AdversarialInput   — a single test payload from the generator
  - Finding            — a confirmed vulnerability with severity metadata
  - TestJob            — lifecycle record for one adversarial test execution
  - TestBatchResult    — aggregate outcome for a parallel batch run
  - CoverageReport     — snapshot of endpoint / property coverage
  - MetricsSnapshot    — dashboard metrics at a point in time
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Category(str, Enum):
    HALLUCINATION = "hallucination"
    BYPASS = "bypass"
    EDGE_CASE = "edge-case"
    LOGIC_ERROR = "logic-error"
    ASSUMPTION_BREAK = "assumption-break"


class Reproducibility(str, Enum):
    ALWAYS = "always"
    SOMETIMES = "sometimes"
    FLAKY = "flaky"


class TestStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class AttackType(str, Enum):
    FALSE_PREMISE = "false_premise"
    CIRCULAR_REASONING = "circular_reasoning"
    BOUNDARY_CONDITION = "boundary_condition"
    ESCALATION_BYPASS = "escalation_bypass"
    POLICY_EVASION = "policy_evasion"
    CONTRADICTION = "contradiction"
    PROMPT_INJECTION = "prompt_injection"
    ADVERSARIAL_PARAPHRASE = "adversarial_paraphrase"


# ---------------------------------------------------------------------------
# Core data models
# ---------------------------------------------------------------------------


@dataclass
class AdversarialInput:
    """A single adversarial test case produced by the generator.

    Attributes:
        payload:           The adversarial text to submit to the system under test.
        expected_defense:  Human-readable description of how the system *should* respond.
        attack_type:       Classification of the attack vector.
        category:          Vulnerability category this tests.
        source:            "abliteration" | "local" | "regression"
        metadata:          Optional pass-through data from the generator.
    """

    payload: str
    expected_defense: str
    attack_type: AttackType
    category: Category = Category.HALLUCINATION
    source: str = "local"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Finding:
    """A confirmed vulnerability discovered during adversarial testing.

    Attributes:
        id:              UUID for this finding.
        severity:        Impact level of the vulnerability.
        category:        Type of vulnerability.
        reproducibility: How consistently the vulnerability triggers.
        impact:          What user-facing behavior breaks.
        mitigation:      Suggested fix or workaround.
        example_input:   Redacted reproducible example.
        test_job_id:     The job_id that discovered this finding.
        created_at:      UTC timestamp of discovery.
        confirmed_at:    Set when a human reviewer confirms the finding.
        fixed_at:        Set when a fix is deployed.
        false_positive:  True if a reviewer marks this as not reproducible.
        review_notes:    Human reviewer notes.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    severity: Severity = Severity.LOW
    category: Category = Category.HALLUCINATION
    reproducibility: Reproducibility = Reproducibility.FLAKY
    impact: str = ""
    mitigation: str = ""
    example_input: str = ""
    test_job_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    confirmed_at: Optional[datetime] = None
    fixed_at: Optional[datetime] = None
    false_positive: bool = False
    review_notes: str = ""


@dataclass
class TestJob:
    """Lifecycle record for one adversarial test execution.

    Attributes:
        job_id:             UUID for this test run.
        input:              The adversarial payload submitted.
        expected_behavior:  Description of the correct system response.
        status:             Current lifecycle status.
        category:           Test category that produced this job.
        attack_type:        Attack vector being tested.
        created_at:         UTC timestamp when queued.
        started_at:         UTC timestamp when execution began.
        completed_at:       UTC timestamp when execution finished.
        result:             Raw system-under-test response (dict).
        finding:            Associated Finding if the test exposed a vulnerability.
        error:              Error message if status == ERROR.
        submitter_token:    Hashed admin token used to submit (never stored raw).
        regression:         True if this is a regression re-test of a known finding.
        regression_finding_id: Finding ID being regression-tested.
    """

    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    input: str = ""
    expected_behavior: str = ""
    status: TestStatus = TestStatus.QUEUED
    category: Optional[str] = None
    attack_type: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[Dict[str, Any]] = None
    finding: Optional[Finding] = None
    error: Optional[str] = None
    submitter_token: Optional[str] = None
    regression: bool = False
    regression_finding_id: Optional[str] = None


@dataclass
class TestBatchResult:
    """Aggregate outcome for a parallel batch run.

    Attributes:
        batch_id:      UUID for this batch.
        job_ids:       Ordered list of job IDs in the batch.
        total:         Total number of tests in the batch.
        passed:        Number of tests that passed.
        failed:        Number of tests that exposed vulnerabilities.
        errored:       Number of tests that encountered execution errors.
        findings:      All findings produced by the batch.
        started_at:    UTC timestamp batch was launched.
        completed_at:  UTC timestamp all jobs finished.
    """

    batch_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    job_ids: List[str] = field(default_factory=list)
    total: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0
    findings: List[Finding] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None


@dataclass
class CoverageReport:
    """Snapshot of test coverage across endpoints and properties.

    Attributes:
        test_count:         Total tests executed.
        category_coverage:  Dict mapping category name → test count.
        attack_coverage:    Dict mapping attack type → test count.
        property_coverage:  Dict mapping property name → bool (tested/not).
        finding_trends:     List of (date, finding_count) tuples (last 30 days).
        endpoint_coverage:  Dict mapping endpoint name → test count.
    """

    test_count: int = 0
    category_coverage: Dict[str, int] = field(default_factory=dict)
    attack_coverage: Dict[str, int] = field(default_factory=dict)
    property_coverage: Dict[str, bool] = field(default_factory=dict)
    finding_trends: List[Dict[str, Any]] = field(default_factory=list)
    endpoint_coverage: Dict[str, int] = field(default_factory=dict)


@dataclass
class MetricsSnapshot:
    """Dashboard metrics at a point in time.

    Attributes:
        test_coverage_pct:      Percentage of defined properties/endpoints tested.
        finding_rate:           New findings per run (rolling average).
        pass_rate_pct:          Percentage of tests passing (should trend up).
        avg_time_to_fix_days:   Mean days from finding to fix deployment.
        false_positive_rate_pct: Percentage of findings marked as false positives.
        open_findings:          Count of unresolved findings.
        critical_findings:      Count of unresolved CRITICAL severity findings.
        total_tests_run:        Cumulative test count.
        total_findings:         Cumulative finding count.
        severity_distribution:  Dict mapping Severity → count.
        captured_at:            UTC timestamp of this snapshot.
    """

    test_coverage_pct: float = 0.0
    finding_rate: float = 0.0
    pass_rate_pct: float = 100.0
    avg_time_to_fix_days: float = 0.0
    false_positive_rate_pct: float = 0.0
    open_findings: int = 0
    critical_findings: int = 0
    total_tests_run: int = 0
    total_findings: int = 0
    severity_distribution: Dict[str, int] = field(
        default_factory=lambda: {s.value: 0 for s in Severity}
    )
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
