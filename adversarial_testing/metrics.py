"""Metrics collection and snapshot for the adversarial testing framework.

Provides MetricsCollector which computes the dashboard snapshot from
live SQLite data. All reads are non-blocking (no locking required — WAL mode).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from . import store
from .schema import CoverageReport, MetricsSnapshot, Severity, TestStatus
from .self_improvement import get_ttf_days

log = logging.getLogger(__name__)

# Properties / categories the framework is expected to cover
_EXPECTED_PROPERTIES = {
    "category:hallucination",
    "category:bypass",
    "category:edge-case",
    "category:logic-error",
    "category:assumption-break",
    "endpoint:deep_think_async",
    "endpoint:get_thinking_result",
    "endpoint:list_thinking_jobs",
    "endpoint:nova_verify",
    "endpoint:nova_synthesize",
}


class MetricsCollector:
    """Computes dashboard metrics from live store data."""

    def snapshot(self) -> MetricsSnapshot:
        """Return a MetricsSnapshot computed from current SQLite state."""
        all_jobs = store.list_jobs(limit=10_000)
        all_findings = store.list_findings(limit=10_000)

        total_tests = len(all_jobs)
        passed = sum(1 for j in all_jobs if j.status == TestStatus.PASSED)
        pass_rate = (passed / total_tests * 100.0) if total_tests else 100.0

        open_findings = [f for f in all_findings if not f.fixed_at and not f.false_positive]
        critical = [f for f in open_findings if f.severity == Severity.CRITICAL]
        total_findings = len([f for f in all_findings if not f.false_positive])

        # False positive rate
        false_positives = sum(1 for f in all_findings if f.false_positive)
        fp_rate = (false_positives / len(all_findings) * 100.0) if all_findings else 0.0

        # Average time-to-fix
        ttf_values = [get_ttf_days(f) for f in all_findings if f.fixed_at]
        avg_ttf = sum(v for v in ttf_values if v is not None) / len(ttf_values) if ttf_values else 0.0

        # Finding rate (new findings per run, rolling 7 days)
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        recent_findings = [
            f for f in all_findings
            if f.created_at.replace(tzinfo=timezone.utc) >= cutoff
        ]
        recent_runs = [
            j for j in all_jobs
            if j.created_at.replace(tzinfo=timezone.utc) >= cutoff
        ]
        run_count = max(len(recent_runs), 1)
        finding_rate = len(recent_findings) / run_count

        # Coverage
        coverage_data = store.get_coverage()
        tested_properties = set()
        for dim, keys in coverage_data.items():
            for key in keys:
                tested_properties.add(f"{dim}:{key}")
        coverage_pct = (
            len(tested_properties & _EXPECTED_PROPERTIES)
            / len(_EXPECTED_PROPERTIES)
            * 100.0
        )

        return MetricsSnapshot(
            test_coverage_pct=round(coverage_pct, 1),
            finding_rate=round(finding_rate, 3),
            pass_rate_pct=round(pass_rate, 1),
            avg_time_to_fix_days=round(avg_ttf, 2),
            false_positive_rate_pct=round(fp_rate, 1),
            open_findings=len(open_findings),
            critical_findings=len(critical),
            total_tests_run=total_tests,
            total_findings=total_findings,
            severity_distribution=store.count_findings_by_severity(),
        )

    def coverage_report(self) -> CoverageReport:
        """Return a structured coverage report."""
        all_jobs = store.list_jobs(limit=10_000)
        coverage_data = store.get_coverage()

        category_coverage: Dict[str, int] = {}
        attack_coverage: Dict[str, int] = {}
        endpoint_coverage: Dict[str, int] = {}

        for dim, keys in coverage_data.items():
            for key, info in keys.items():
                count = info["test_count"]
                if dim == "category":
                    category_coverage[key] = count
                elif dim == "attack_type":
                    attack_coverage[key] = count
                elif dim == "endpoint":
                    endpoint_coverage[key] = count

        # Property coverage: which expected properties have been tested at all?
        tested_props = set()
        for dim, keys in coverage_data.items():
            for key in keys:
                tested_props.add(f"{dim}:{key}")

        property_coverage = {
            prop: (prop in tested_props) for prop in sorted(_EXPECTED_PROPERTIES)
        }

        # Finding trends: daily finding counts for the last 30 days
        finding_trends = _compute_finding_trends()

        return CoverageReport(
            test_count=len(all_jobs),
            category_coverage=category_coverage,
            attack_coverage=attack_coverage,
            property_coverage=property_coverage,
            finding_trends=finding_trends,
            endpoint_coverage=endpoint_coverage,
        )


def _compute_finding_trends() -> List[Dict]:
    """Return daily finding counts for the last 30 days."""
    findings = store.list_findings(limit=10_000)
    now = datetime.now(timezone.utc)
    trends = []
    for day_offset in range(30):
        day = (now - timedelta(days=29 - day_offset)).date()
        count = sum(
            1
            for f in findings
            if f.created_at.date() == day and not f.false_positive
        )
        trends.append({"date": day.isoformat(), "finding_count": count})
    return trends
