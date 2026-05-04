"""
Integration tests for validation_suite endpoint in production system.

Tests:
1. Unit test: Regression detection algorithms (thresholds)
2. Unit test: Improvement scoring calculation
3. Integration test: Metrics before/after comparison
4. E2E test: Implementation → validation → decision
5. Edge case: All metrics improve (excellent)
6. Edge case: Some regressions (fail validation)
"""

import pytest
import json
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from adversarial_testing.validation_suite import ValidationSuite, MetricsSnapshot
# AdversarialStore not needed
from adversarial_testing.metrics import MetricsCollector


class TestRegressionDetection:
    """Unit tests: Regression detection algorithms and thresholds"""

    def test_error_rate_regression_threshold(self):
        """Test error rate increase detection (0.5% threshold)"""
        metrics = MagicMock(spec=MetricsCollector)
        suite = ValidationSuite(metrics=metrics)

        baseline = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=2.0,
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        # Regression: error rate increased by 0.6% (above threshold)
        after_regression = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=2.6,  # +0.6% increase
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        regressions = suite._detect_regressions(baseline, after_regression)
        assert len(regressions) > 0
        assert any("error_rate" in r for r in regressions)

        # No regression: error rate increased by 0.4% (below threshold)
        after_ok = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=2.4,  # +0.4% increase
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        regressions = suite._detect_regressions(baseline, after_ok)
        assert not any("error_rate" in r for r in regressions)

    def test_timeout_rate_regression_threshold(self):
        """Test timeout rate increase detection (2% threshold)"""
        # store not needed
        metrics = MagicMock(spec=MetricsCollector)
        suite = ValidationSuite(metrics=metrics)

        baseline = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=2.0,
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        # Regression: timeout rate increased by 2.5% (above threshold)
        after_regression = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=2.0,
            timeout_rate=3.0,  # +2.5% increase
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        regressions = suite._detect_regressions(baseline, after_regression)
        assert len(regressions) > 0
        assert any("timeout_rate" in r for r in regressions)

        # No regression: timeout rate increased by 0.8% (below threshold)
        after_ok = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=2.0,
            timeout_rate=1.3,  # +0.8% increase
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        regressions = suite._detect_regressions(baseline, after_ok)
        assert not any("timeout_rate" in r for r in regressions)

    def test_coverage_decrease_regression_threshold(self):
        """Test coverage decrease detection (1% threshold)"""
        # store not needed
        metrics = MagicMock(spec=MetricsCollector)
        suite = ValidationSuite(metrics=metrics)

        baseline = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=2.0,
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        # Regression: coverage decreased by 1.5% (above threshold)
        after_regression = MetricsSnapshot(
            test_coverage_pct=83.5,  # -1.5% decrease
            pass_rate_pct=95.0,
            error_rate=2.0,
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        regressions = suite._detect_regressions(baseline, after_regression)
        assert len(regressions) > 0
        assert any("test_coverage" in r for r in regressions)

        # No regression: coverage decreased by 0.8% (below threshold)
        after_ok = MetricsSnapshot(
            test_coverage_pct=84.2,  # -0.8% decrease
            pass_rate_pct=95.0,
            error_rate=2.0,
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        regressions = suite._detect_regressions(baseline, after_ok)
        assert not any("test_coverage" in r for r in regressions)

    def test_latency_increase_regression_threshold(self):
        """Test latency increase detection (20% threshold)"""
        # store not needed
        metrics = MagicMock(spec=MetricsCollector)
        suite = ValidationSuite(metrics=metrics)

        baseline = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=2.0,
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        # Regression: latency increased by 25% (above 20% threshold)
        after_regression = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=2.0,
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=125.0,  # +25% increase
        )

        regressions = suite._detect_regressions(baseline, after_regression)
        assert len(regressions) > 0
        assert any("p95_latency" in r for r in regressions)

        # No regression: latency increased by 15% (below 20% threshold)
        after_ok = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=2.0,
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=115.0,  # +15% increase
        )

        regressions = suite._detect_regressions(baseline, after_ok)
        assert not any("p95_latency" in r for r in regressions)


class TestImprovementScoring:
    """Unit tests: Improvement score calculation (0-1 scale)"""

    def test_improvement_score_with_ttf_improvement(self):
        """Test improvement score when time-to-fix decreases (50% weight)"""
        # store not needed
        metrics = MagicMock(spec=MetricsCollector)
        suite = ValidationSuite(metrics=metrics)

        baseline = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=2.0,
            timeout_rate=0.5,
            avg_time_to_fix_days=4.0,  # Baseline TTF
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        # 50% reduction in TTF: (4.0 - 2.0) / 4.0 = 0.5
        after = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=2.0,
            timeout_rate=0.5,
            avg_time_to_fix_days=2.0,  # 50% improvement
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        score = suite._compute_improvement(baseline, after, severity="HIGH")
        # TTF improvement: 0.5 * 0.5 (weight) = 0.25
        # Other metrics are unchanged, so score should be ~0.25
        assert score > 0.2
        assert score < 0.3

    def test_improvement_score_with_pass_rate_improvement(self):
        """Test improvement score when pass rate increases (30% weight)"""
        # store not needed
        metrics = MagicMock(spec=MetricsCollector)
        suite = ValidationSuite(metrics=metrics)

        baseline = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=90.0,  # Baseline
            error_rate=2.0,
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        # 10% absolute increase in pass rate: (100 - 90) / 100 = 0.1
        after = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=100.0,  # +10% absolute
            error_rate=2.0,
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        score = suite._compute_improvement(baseline, after, severity="MEDIUM")
        # Pass rate improvement: 0.1 * 0.3 (weight) = 0.03
        assert score > 0.02
        assert score < 0.05

    def test_improvement_score_with_error_rate_reduction(self):
        """Test improvement score when error rate decreases (15% weight)"""
        # store not needed
        metrics = MagicMock(spec=MetricsCollector)
        suite = ValidationSuite(metrics=metrics)

        baseline = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=4.0,  # Baseline
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        # 50% reduction in error rate: (4.0 - 2.0) / 4.0 = 0.5
        after = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=2.0,  # 50% improvement
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        score = suite._compute_improvement(baseline, after, severity="MEDIUM")
        # Error reduction: 0.5 * 0.15 (weight) = 0.075
        assert score > 0.07
        assert score < 0.09

    def test_improvement_score_all_metrics_improve(self):
        """Test improvement score when ALL metrics improve (excellent case)"""
        # store not needed
        metrics = MagicMock(spec=MetricsCollector)
        suite = ValidationSuite(metrics=metrics)

        baseline = MetricsSnapshot(
            test_coverage_pct=80.0,
            pass_rate_pct=90.0,
            error_rate=4.0,
            timeout_rate=1.0,
            avg_time_to_fix_days=5.0,
            false_positive_rate=10.0,
            open_findings=20,
            critical_findings=5,
            p95_latency_ms=150.0,
        )

        # All metrics improve significantly
        after = MetricsSnapshot(
            test_coverage_pct=92.0,  # +12%
            pass_rate_pct=99.0,  # +9%
            error_rate=1.0,  # -75%
            timeout_rate=0.2,  # -80%
            avg_time_to_fix_days=2.0,  # -60%
            false_positive_rate=2.0,  # -80%
            open_findings=5,
            critical_findings=1,
            p95_latency_ms=120.0,  # -20%
        )

        score = suite._compute_improvement(baseline, after, severity="CRITICAL")
        # Score should be quite high due to multiple improvements
        assert score > 0.45
        assert score <= 1.0

    def test_improvement_score_no_improvement(self):
        """Test improvement score when nothing improves (0)"""
        # store not needed
        metrics = MagicMock(spec=MetricsCollector)
        suite = ValidationSuite(metrics=metrics)

        baseline = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=2.0,
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        # No changes
        after = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=2.0,
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        score = suite._compute_improvement(baseline, after, severity="MEDIUM")
        assert score == 0.0

    def test_improvement_score_clamped_to_1(self):
        """Test that improvement score is clamped to [0, 1]"""
        # store not needed
        metrics = MagicMock(spec=MetricsCollector)
        suite = ValidationSuite(metrics=metrics)

        baseline = MetricsSnapshot(
            test_coverage_pct=50.0,
            pass_rate_pct=50.0,
            error_rate=10.0,
            timeout_rate=5.0,
            avg_time_to_fix_days=10.0,
            false_positive_rate=20.0,
            open_findings=100,
            critical_findings=20,
            p95_latency_ms=500.0,
        )

        # Extreme improvements (should clamp to 1)
        after = MetricsSnapshot(
            test_coverage_pct=99.0,
            pass_rate_pct=99.5,
            error_rate=0.1,
            timeout_rate=0.01,
            avg_time_to_fix_days=0.5,
            false_positive_rate=0.5,
            open_findings=1,
            critical_findings=0,
            p95_latency_ms=100.0,
        )

        score = suite._compute_improvement(baseline, after, severity="CRITICAL")
        assert score <= 1.0


class TestValidationDecision:
    """Unit tests: Pass/fail decision logic"""

    def test_validation_fails_on_regression(self):
        """Test that validation fails if any regression is detected"""
        # store not needed
        metrics = MagicMock(spec=MetricsCollector)
        suite = ValidationSuite(metrics=metrics)

        regressions = ["error_rate increased 2.0% → 2.8%"]
        improvement = 0.5  # Good improvement score

        passed = suite._should_pass_validation(regressions, improvement, "MEDIUM")
        assert not passed

    def test_validation_fails_on_high_severity_low_improvement(self):
        """Test that HIGH severity fixes require minimum improvement (5%)"""
        # store not needed
        metrics = MagicMock(spec=MetricsCollector)
        suite = ValidationSuite(metrics=metrics)

        regressions = []
        improvement = 0.03  # Below MIN_IMPROVEMENT_SCORE (0.05)

        passed = suite._should_pass_validation(regressions, improvement, "HIGH")
        assert not passed

    def test_validation_passes_with_good_score_and_no_regressions(self):
        """Test that validation passes with good improvement and no regressions"""
        # store not needed
        metrics = MagicMock(spec=MetricsCollector)
        suite = ValidationSuite(metrics=metrics)

        regressions = []
        improvement = 0.7  # Good score

        passed = suite._should_pass_validation(regressions, improvement, "MEDIUM")
        assert passed


class TestMetricsComparison:
    """Integration tests: Before/after metrics comparison"""

    def test_metrics_snapshot_conversion(self):
        """Test conversion of MetricsSnapshot to dict"""
        # store not needed
        metrics = MagicMock(spec=MetricsCollector)
        suite = ValidationSuite(metrics=metrics)

        snapshot = MetricsSnapshot(
            test_coverage_pct=85.0,
            pass_rate_pct=95.0,
            error_rate=2.0,
            timeout_rate=0.5,
            avg_time_to_fix_days=3.0,
            false_positive_rate=5.0,
            open_findings=10,
            critical_findings=2,
            p95_latency_ms=100.0,
        )

        metrics_dict = suite._metrics_to_dict(snapshot)
        assert metrics_dict["test_coverage_pct"] == 85.0
        assert metrics_dict["pass_rate_pct"] == 95.0
        assert metrics_dict["error_rate"] == 2.0
        assert metrics_dict["p95_latency_ms"] == 100.0


class TestEndToEndValidation:
    """E2E tests: Full validation flow"""

    @pytest.mark.asyncio
    async def test_e2e_validation_passes(self):
        """Test E2E: Implementation with good metrics passes validation"""
        metrics = MagicMock(spec=MetricsCollector)
        suite = ValidationSuite(metrics=metrics)

        metrics.snapshot.side_effect = [
            {
                "test_coverage_pct": 85.0,
                "pass_rate_pct": 95.0,
                "error_rate": 2.0,
                "timeout_rate": 0.5,
                "avg_time_to_fix_days": 3.0,
                "false_positive_rate": 5.0,
                "open_findings": 10,
                "critical_findings": 2,
                "p95_latency_ms": 100.0,
            },
            {
                "test_coverage_pct": 90.0,  # Improvement
                "pass_rate_pct": 97.0,  # Improvement
                "error_rate": 1.5,  # Improvement
                "timeout_rate": 0.4,  # Improvement
                "avg_time_to_fix_days": 2.5,  # Improvement
                "false_positive_rate": 4.0,  # Improvement
                "open_findings": 10,
                "critical_findings": 2,
                "p95_latency_ms": 98.0,  # Improvement
            },
        ]

        # Mock plan lookup
        mock_result = MagicMock()
        mock_result.fetchone.return_value = {
            "id": "plan-1",
            "severity": "MEDIUM",
        }

        with patch(
            "adversarial_testing.store.execute_query",
            return_value=mock_result,
        ):
            with patch(
                "adversarial_testing.store.execute_update",
                return_value=1,
            ):
                with patch(
                    "adversarial_testing.validation_suite.ValidationSuite._checkout_commit",
                    new_callable=AsyncMock,
                ) as mock_checkout:
                    with patch(
                        "adversarial_testing.validation_suite.ValidationSuite._run_test_suite",
                        new_callable=AsyncMock,
                    ) as mock_test:
                        with patch(
                            "adversarial_testing.validation_suite.ValidationSuite._checkout_main",
                            new_callable=AsyncMock,
                        ):
                            mock_checkout.return_value = (True, "")
                            mock_test.return_value = ("Test output", True)

                            passed, error_msg, details = await suite.validate_implementation(
                                plan_id="plan-1",
                                commit_sha="abc123",
                            )

                            assert passed
                            assert error_msg is None
                            assert details["improvement_score"] > 0
                            assert len(details["regressions"]) == 0

    @pytest.mark.asyncio
    async def test_e2e_validation_fails_on_regression(self):
        """Test E2E: Implementation with regressions fails validation"""
        metrics = MagicMock(spec=MetricsCollector)
        suite = ValidationSuite(metrics=metrics)

        metrics.snapshot.side_effect = [
            {
                "test_coverage_pct": 85.0,
                "pass_rate_pct": 95.0,
                "error_rate": 2.0,
                "timeout_rate": 0.5,
                "avg_time_to_fix_days": 3.0,
                "false_positive_rate": 5.0,
                "open_findings": 10,
                "critical_findings": 2,
                "p95_latency_ms": 100.0,
            },
            {
                "test_coverage_pct": 85.0,
                "pass_rate_pct": 95.0,
                "error_rate": 3.2,  # REGRESSION: +1.2%
                "timeout_rate": 0.5,
                "avg_time_to_fix_days": 3.0,
                "false_positive_rate": 5.0,
                "open_findings": 10,
                "critical_findings": 2,
                "p95_latency_ms": 100.0,
            },
        ]

        # Mock plan lookup
        mock_result = MagicMock()
        mock_result.fetchone.return_value = {
            "id": "plan-1",
            "severity": "MEDIUM",
        }

        with patch(
            "adversarial_testing.store.execute_query",
            return_value=mock_result,
        ):
            with patch(
                "adversarial_testing.store.execute_update",
                return_value=1,
            ):
                with patch(
                    "adversarial_testing.validation_suite.ValidationSuite._checkout_commit",
                    new_callable=AsyncMock,
                ) as mock_checkout:
                    with patch(
                        "adversarial_testing.validation_suite.ValidationSuite._run_test_suite",
                        new_callable=AsyncMock,
                    ) as mock_test:
                        with patch(
                            "adversarial_testing.validation_suite.ValidationSuite._checkout_main",
                            new_callable=AsyncMock,
                        ):
                            mock_checkout.return_value = (True, "")
                            mock_test.return_value = ("Test output", True)

                            passed, error_msg, details = await suite.validate_implementation(
                                plan_id="plan-1",
                                commit_sha="abc123",
                            )

                            assert not passed
                            assert error_msg is not None
                            assert "Regressions detected" in error_msg
                            assert len(details["regressions"]) > 0
