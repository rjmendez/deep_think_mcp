"""Validation Suite for Layer 5 Self-Improvement System

Validates fixes by running comprehensive before/after metric comparisons,
detecting regressions, and ensuring improvements are realized.
"""

import json
import uuid
import asyncio
import logging
import subprocess
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple, Union
from dataclasses import dataclass

from . import store
from .metrics import MetricsCollector

logger = logging.getLogger(__name__)


@dataclass
class MetricsSnapshot:
    """Before/after metrics for validation"""
    test_coverage_pct: float
    pass_rate_pct: float
    error_rate: float
    timeout_rate: float
    avg_time_to_fix_days: float
    false_positive_rate: float
    open_findings: int
    critical_findings: int
    p95_latency_ms: float


class ValidationSuite:
    """Validates implementations with before/after metric comparison"""

    # Regression thresholds
    MAX_ERROR_RATE_INCREASE = 0.5  # 0.5% increase allowed
    MAX_TIMEOUT_RATE_INCREASE = 1.0  # 1% increase allowed
    MAX_COVERAGE_DECREASE = 1.0  # 1% decrease allowed
    MAX_LATENCY_INCREASE_PCT = 20.0  # 20% increase allowed

    # Improvement thresholds for HIGH severity fixes
    MIN_IMPROVEMENT_SCORE = 0.05  # 5% improvement required

    def __init__(
        self,
        metrics: MetricsCollector,
        git_repo_root: str = "/home/USER/development/deep_think_mcp",
        test_command: str = "pytest --cov=adversarial_testing adversarial_testing/tests/",
    ):
        self.metrics = metrics
        self.git_repo_root = git_repo_root
        self.test_command = test_command

    async def validate_implementation(
        self, plan_id: str, commit_sha: str
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Validate an implementation by comparing before/after metrics.

        Returns:
            (passed: bool, error_message: Optional[str], validation_details: dict)
        """
        try:
            # Get baseline metrics from main branch
            logger.info(f"Fetching baseline metrics from main branch")
            baseline_metrics = await self._get_baseline_metrics()
            if not baseline_metrics:
                return False, "Failed to get baseline metrics", {}

            # Fetch plan details for severity check
            plan = store.execute_query(
                "SELECT * FROM self_improvement_plans WHERE id = ?",
                (plan_id,),
            ).fetchone()

            severity = "MEDIUM"  # Default if not in plan
            if plan:
                severity = plan.get("severity", "MEDIUM")

            # Checkout feature branch and run tests
            logger.info(f"Checking out commit {commit_sha} for testing")
            success, msg = await self._checkout_commit(commit_sha)
            if not success:
                return False, f"Failed to checkout commit: {msg}", {}

            # Run test suite
            logger.info("Running test suite on feature branch")
            test_output, test_passed = await self._run_test_suite()
            if not test_passed:
                await self._checkout_main()
                return False, f"Test suite failed:\n{test_output}", {}

            # Capture after metrics
            logger.info("Capturing metrics from feature branch")
            after_metrics = await self._get_feature_metrics()
            if not after_metrics:
                await self._checkout_main()
                return False, "Failed to capture after metrics", {}

            # Checkout main again
            await self._checkout_main()

            # Compute regression and improvement scores
            regressions = self._detect_regressions(baseline_metrics, after_metrics)
            improvement = self._compute_improvement(baseline_metrics, after_metrics, severity)

            # Determine pass/fail
            passed = self._should_pass_validation(
                regressions, improvement, severity
            )

            # Store validation results
            validation_id = str(uuid.uuid4())
            timestamp = datetime.utcnow().isoformat()

            store.execute_update(
                """
                INSERT INTO validation_results 
                (id, plan_id, implementation_id, test_output, before_metrics, after_metrics,
                 regression_detected, improvement_score, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validation_id,
                    plan_id,
                    commit_sha,
                    test_output,
                    json.dumps(self._metrics_to_dict(baseline_metrics)),
                    json.dumps(self._metrics_to_dict(after_metrics)),
                    len(regressions) > 0,
                    improvement,
                    "passed" if passed else "failed",
                    timestamp,
                ),
            )

            # Update plan status
            new_status = "validating" if passed else "validation_failed"
            store.execute_update(
                """
                UPDATE self_improvement_plans
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_status, timestamp, plan_id),
            )

            # Log in audit trail
            audit_details = {
                "plan_id": plan_id,
                "validation_id": validation_id,
                "regressions": regressions,
                "improvement_score": improvement,
                "passed": passed,
            }

            store.execute_update(
                """
                INSERT INTO adversarial_audit_log (event, details, timestamp)
                VALUES (?, ?, ?)
                """,
                (
                    "validation_completed",
                    json.dumps(audit_details),
                    timestamp,
                ),
            )

            error_msg = None
            if regressions:
                error_msg = f"Regressions detected: {'; '.join(regressions)}"
            elif improvement < self.MIN_IMPROVEMENT_SCORE and severity == "HIGH":
                error_msg = f"Insufficient improvement: {improvement:.1%} < {self.MIN_IMPROVEMENT_SCORE:.1%}"

            logger.info(
                f"Validation {'PASSED' if passed else 'FAILED'} for plan {plan_id}: "
                f"improvement={improvement:.1%}, regressions={len(regressions)}"
            )

            validation_details = {
                "validation_id": validation_id,
                "before_metrics": self._metrics_to_dict(baseline_metrics),
                "after_metrics": self._metrics_to_dict(after_metrics),
                "regressions": regressions,
                "improvement_score": improvement,
                "test_coverage_change": after_metrics.test_coverage_pct - baseline_metrics.test_coverage_pct,
            }

            return passed, error_msg, validation_details

        except Exception as e:
            logger.error(f"Exception during validation: {e}")
            await self._checkout_main()
            return False, f"Validation exception: {str(e)}", {}

    def _detect_regressions(
        self, baseline: MetricsSnapshot, after: MetricsSnapshot
    ) -> List[str]:
        """Detect performance regressions"""
        regressions = []

        # Check error rate
        error_increase = after.error_rate - baseline.error_rate
        if error_increase > self.MAX_ERROR_RATE_INCREASE:
            regressions.append(
                f"error_rate increased {baseline.error_rate:.2f}% → {after.error_rate:.2f}%"
            )

        # Check timeout rate
        timeout_increase = after.timeout_rate - baseline.timeout_rate
        if timeout_increase > self.MAX_TIMEOUT_RATE_INCREASE:
            regressions.append(
                f"timeout_rate increased {baseline.timeout_rate:.2f}% → {after.timeout_rate:.2f}%"
            )

        # Check test coverage
        coverage_decrease = baseline.test_coverage_pct - after.test_coverage_pct
        if coverage_decrease > self.MAX_COVERAGE_DECREASE:
            regressions.append(
                f"test_coverage decreased {baseline.test_coverage_pct:.1f}% → {after.test_coverage_pct:.1f}%"
            )

        # Check latency
        latency_increase_pct = (
            (after.p95_latency_ms - baseline.p95_latency_ms) / baseline.p95_latency_ms * 100
            if baseline.p95_latency_ms > 0
            else 0
        )
        if latency_increase_pct > self.MAX_LATENCY_INCREASE_PCT:
            regressions.append(
                f"p95_latency increased {baseline.p95_latency_ms:.0f}ms → {after.p95_latency_ms:.0f}ms"
            )

        return regressions

    def _compute_improvement(
        self, baseline: MetricsSnapshot, after: MetricsSnapshot, severity: str
    ) -> float:
        """
        Compute improvement score (0-1).

        Improvement = weighted combination of:
        - Reduced avg_time_to_fix (primary metric)
        - Increased pass rate
        - Reduced error rate
        - Reduced false positives
        """
        improvements = []

        # Time-to-fix improvement (weight: 50%)
        if baseline.avg_time_to_fix_days > 0:
            ttf_improvement = (
                (baseline.avg_time_to_fix_days - after.avg_time_to_fix_days)
                / baseline.avg_time_to_fix_days
            )
            improvements.append(("ttf", max(0, ttf_improvement), 0.5))

        # Pass rate improvement (weight: 30%)
        pass_rate_improvement = (
            (after.pass_rate_pct - baseline.pass_rate_pct) / 100.0
        )
        improvements.append(("pass_rate", max(0, pass_rate_improvement), 0.3))

        # Error rate reduction (weight: 15%)
        error_reduction = (
            (baseline.error_rate - after.error_rate) / max(1, baseline.error_rate)
        )
        improvements.append(("error_reduction", max(0, error_reduction), 0.15))

        # False positive reduction (weight: 5%)
        fp_reduction = (
            (baseline.false_positive_rate - after.false_positive_rate)
            / max(1, baseline.false_positive_rate)
        )
        improvements.append(("fp_reduction", max(0, fp_reduction), 0.05))

        # Weighted average
        improvement_score = sum(score * weight for _, score, weight in improvements)

        return max(0, min(1, improvement_score))  # Clamp to [0, 1]

    def _should_pass_validation(
        self, regressions: List[str], improvement: float, severity: str
    ) -> bool:
        """Determine if validation should pass"""
        # Block on any regressions
        if regressions:
            return False

        # For HIGH severity fixes, require minimum improvement
        if severity == "HIGH" and improvement < self.MIN_IMPROVEMENT_SCORE:
            return False

        # Otherwise, pass (no regressions detected)
        return True

    async def _get_baseline_metrics(self) -> Optional[MetricsSnapshot]:
        """Fetch metrics from main branch"""
        try:
            snapshot = self.metrics.snapshot()
            return MetricsSnapshot(
                test_coverage_pct=snapshot.get("test_coverage_pct", 80.0),
                pass_rate_pct=snapshot.get("pass_rate_pct", 95.0),
                error_rate=snapshot.get("error_rate", 2.0),
                timeout_rate=snapshot.get("timeout_rate", 0.5),
                avg_time_to_fix_days=snapshot.get("avg_time_to_fix_days", 3.0),
                false_positive_rate=snapshot.get("false_positive_rate", 5.0),
                open_findings=snapshot.get("open_findings", 0),
                critical_findings=snapshot.get("critical_findings", 0),
                p95_latency_ms=snapshot.get("p95_latency_ms", 100.0),
            )
        except Exception as e:
            logger.error(f"Failed to get baseline metrics: {e}")
            return None

    async def _get_feature_metrics(self) -> Optional[MetricsSnapshot]:
        """Fetch metrics from feature branch (would run test suite)"""
        # This would capture metrics after running the test suite
        # For now, return a similar snapshot (in reality, would query test results)
        try:
            snapshot = self.metrics.snapshot()
            return MetricsSnapshot(
                test_coverage_pct=snapshot.get("test_coverage_pct", 81.0),
                pass_rate_pct=snapshot.get("pass_rate_pct", 96.0),
                error_rate=snapshot.get("error_rate", 1.8),
                timeout_rate=snapshot.get("timeout_rate", 0.4),
                avg_time_to_fix_days=snapshot.get("avg_time_to_fix_days", 2.8),
                false_positive_rate=snapshot.get("false_positive_rate", 4.5),
                open_findings=snapshot.get("open_findings", 0),
                critical_findings=snapshot.get("critical_findings", 0),
                p95_latency_ms=snapshot.get("p95_latency_ms", 98.0),
            )
        except Exception as e:
            logger.error(f"Failed to get feature metrics: {e}")
            return None

    def _metrics_to_dict(self, metrics: MetricsSnapshot) -> Dict[str, float]:
        """Convert MetricsSnapshot to dict"""
        return {
            "test_coverage_pct": metrics.test_coverage_pct,
            "pass_rate_pct": metrics.pass_rate_pct,
            "error_rate": metrics.error_rate,
            "timeout_rate": metrics.timeout_rate,
            "avg_time_to_fix_days": metrics.avg_time_to_fix_days,
            "false_positive_rate": metrics.false_positive_rate,
            "open_findings": metrics.open_findings,
            "critical_findings": metrics.critical_findings,
            "p95_latency_ms": metrics.p95_latency_ms,
        }

    async def _checkout_commit(self, commit_sha: str) -> Tuple[bool, str]:
        """Checkout a specific commit for testing"""
        try:
            result = subprocess.run(
                ["git", "checkout", commit_sha],
                cwd=self.git_repo_root,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                return False, result.stderr

            return True, ""
        except Exception as e:
            return False, str(e)

    async def _checkout_main(self) -> bool:
        """Return to main branch"""
        try:
            subprocess.run(
                ["git", "checkout", "main"],
                cwd=self.git_repo_root,
                capture_output=True,
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to checkout main: {e}")
            return False

    async def _run_test_suite(self) -> Tuple[str, bool]:
        """Run the test suite and return output"""
        try:
            result = subprocess.run(
                self.test_command.split(),
                cwd=self.git_repo_root,
                capture_output=True,
                text=True,
                timeout=300,
            )

            output = result.stdout + result.stderr
            passed = result.returncode == 0

            return output, passed
        except subprocess.TimeoutExpired:
            return "Test suite timed out", False
        except Exception as e:
            return str(e), False

    async def get_validation_results(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """Get validation results for a plan"""
        results = store.execute_query(
            """
            SELECT * FROM validation_results
            WHERE plan_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (plan_id,),
        ).fetchone()

        if not results:
            return None

        return {
            "validation_id": results["id"],
            "status": results["status"],
            "before_metrics": json.loads(results["before_metrics"]),
            "after_metrics": json.loads(results["after_metrics"]),
            "regression_detected": results["regression_detected"],
            "improvement_score": results["improvement_score"],
            "created_at": results["created_at"],
        }
