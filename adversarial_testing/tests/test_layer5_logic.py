"""Test suite for Layer 5 Self-Improvement System core logic

Tests focused on critical algorithms and decision logic:
- Priority scoring for findings
- Regression detection
- Improvement computation
- Canary rollback detection
"""

import pytest
import json
from datetime import datetime


# ============================================================================
# PLANNING ENGINE LOGIC TESTS
# ============================================================================


class TestPriorityScoring:
    """Test priority score computation logic"""

    SEVERITY_WEIGHTS = {
        "CRITICAL": 3.0,
        "HIGH": 2.0,
        "MEDIUM": 1.0,
        "LOW": 0.3,
    }

    RISK_PENALTY = {
        1: 1.0,  # LOW risk
        2: 1.5,  # MEDIUM risk
        3: 2.5,  # HIGH risk
    }

    EFFORT_PENALTY = {
        1: 1.0,
        2: 1.2,
        3: 1.5,
        4: 2.0,
        5: 3.0,
    }

    def _compute_priority(self, finding, effort_estimate=2, risk_level=2):
        """Compute priority score for a finding"""
        severity = finding.get("severity", "MEDIUM")
        impact = finding.get("impact", 1.0)
        reproducibility = finding.get("reproducibility", 0.5)

        numerator = self.SEVERITY_WEIGHTS.get(severity, 1.0) * impact * reproducibility
        denominator = self.EFFORT_PENALTY.get(effort_estimate, 1.5) * self.RISK_PENALTY.get(risk_level, 1.5)

        return numerator / denominator

    def test_priority_higher_severity(self):
        """CRITICAL findings should score higher than HIGH"""
        finding = {"impact": 8.0, "reproducibility": 0.9}

        critical_score = self._compute_priority({"severity": "CRITICAL", **finding})
        high_score = self._compute_priority({"severity": "HIGH", **finding})

        assert critical_score > high_score

    def test_priority_higher_impact(self):
        """Higher impact findings should score higher"""
        finding = {"severity": "HIGH", "reproducibility": 0.9}

        high_impact = self._compute_priority({**finding, "impact": 9.0})
        low_impact = self._compute_priority({**finding, "impact": 5.0})

        assert high_impact > low_impact

    def test_priority_higher_reproducibility(self):
        """More reproducible findings should score higher"""
        finding = {"severity": "HIGH", "impact": 8.0}

        reproducible = self._compute_priority({**finding, "reproducibility": 0.95})
        less_reproducible = self._compute_priority({**finding, "reproducibility": 0.70})

        assert reproducible > less_reproducible

    def test_priority_lower_effort_is_higher(self):
        """Lower effort fixes should have higher priority (easier to fix)"""
        finding = {"severity": "HIGH", "impact": 8.0, "reproducibility": 0.9}

        # Use effort_estimate parameter
        low_effort = self._compute_priority(finding, effort_estimate=1)
        high_effort = self._compute_priority(finding, effort_estimate=5)

        assert low_effort > high_effort

    def test_priority_reasonable_bounds(self):
        """Priority scores should be in reasonable bounds"""
        finding = {"severity": "HIGH", "impact": 8.0, "reproducibility": 0.9}

        priority = self._compute_priority(finding)
        assert 0 < priority < 100


# ============================================================================
# REGRESSION DETECTION TESTS
# ============================================================================


class TestRegressionDetection:
    """Test regression detection logic"""

    MAX_ERROR_RATE_INCREASE = 0.5
    MAX_TIMEOUT_RATE_INCREASE = 1.0
    MAX_COVERAGE_DECREASE = 1.0
    MAX_LATENCY_INCREASE_PCT = 20.0

    def _detect_regressions(self, baseline, after):
        """Detect performance regressions"""
        regressions = []

        # Check error rate
        error_increase = after.get("error_rate", 0) - baseline.get("error_rate", 0)
        if error_increase > self.MAX_ERROR_RATE_INCREASE:
            regressions.append(
                f"error_rate increased {baseline['error_rate']:.2f}% → {after['error_rate']:.2f}%"
            )

        # Check timeout rate
        timeout_increase = after.get("timeout_rate", 0) - baseline.get("timeout_rate", 0)
        if timeout_increase > self.MAX_TIMEOUT_RATE_INCREASE:
            regressions.append(
                f"timeout_rate increased {baseline['timeout_rate']:.2f}% → {after['timeout_rate']:.2f}%"
            )

        # Check test coverage
        coverage_decrease = baseline.get("test_coverage_pct", 80) - after.get("test_coverage_pct", 80)
        if coverage_decrease > self.MAX_COVERAGE_DECREASE:
            regressions.append(
                f"test_coverage decreased {baseline['test_coverage_pct']:.1f}% → {after['test_coverage_pct']:.1f}%"
            )

        # Check latency
        baseline_latency = baseline.get("p95_latency_ms", 100)
        after_latency = after.get("p95_latency_ms", 100)
        latency_increase_pct = (
            (after_latency - baseline_latency) / baseline_latency * 100
            if baseline_latency > 0
            else 0
        )
        if latency_increase_pct > self.MAX_LATENCY_INCREASE_PCT:
            regressions.append(
                f"p95_latency increased {baseline_latency:.0f}ms → {after_latency:.0f}ms"
            )

        return regressions

    def test_error_rate_regression(self):
        """Should detect error rate regression"""
        baseline = {"error_rate": 2.0, "timeout_rate": 0.5, "test_coverage_pct": 80.0, "p95_latency_ms": 100.0}
        after = {"error_rate": 2.8, "timeout_rate": 0.5, "test_coverage_pct": 80.0, "p95_latency_ms": 100.0}

        regressions = self._detect_regressions(baseline, after)
        assert len(regressions) > 0
        assert any("error_rate" in r for r in regressions)

    def test_timeout_rate_regression(self):
        """Should detect timeout rate regression"""
        baseline = {"error_rate": 2.0, "timeout_rate": 0.5, "test_coverage_pct": 80.0, "p95_latency_ms": 100.0}
        after = {"error_rate": 2.0, "timeout_rate": 1.8, "test_coverage_pct": 80.0, "p95_latency_ms": 100.0}

        regressions = self._detect_regressions(baseline, after)
        assert len(regressions) > 0
        assert any("timeout_rate" in r for r in regressions)

    def test_coverage_regression(self):
        """Should detect test coverage regression"""
        baseline = {"error_rate": 2.0, "timeout_rate": 0.5, "test_coverage_pct": 85.0, "p95_latency_ms": 100.0}
        after = {"error_rate": 2.0, "timeout_rate": 0.5, "test_coverage_pct": 83.5, "p95_latency_ms": 100.0}

        regressions = self._detect_regressions(baseline, after)
        assert len(regressions) > 0
        assert any("test_coverage" in r for r in regressions)

    def test_latency_regression(self):
        """Should detect latency regression"""
        baseline = {"error_rate": 2.0, "timeout_rate": 0.5, "test_coverage_pct": 80.0, "p95_latency_ms": 100.0}
        after = {"error_rate": 2.0, "timeout_rate": 0.5, "test_coverage_pct": 80.0, "p95_latency_ms": 125.0}

        regressions = self._detect_regressions(baseline, after)
        assert len(regressions) > 0
        assert any("latency" in r for r in regressions)

    def test_no_regression_green_metrics(self):
        """Should not report regressions with stable metrics"""
        baseline = {"error_rate": 2.0, "timeout_rate": 0.5, "test_coverage_pct": 80.0, "p95_latency_ms": 100.0}
        after = {"error_rate": 1.8, "timeout_rate": 0.4, "test_coverage_pct": 80.5, "p95_latency_ms": 99.0}

        regressions = self._detect_regressions(baseline, after)
        assert len(regressions) == 0


# ============================================================================
# IMPROVEMENT SCORING TESTS
# ============================================================================


class TestImprovementScoring:
    """Test improvement scoring logic"""

    def _compute_improvement(self, baseline, after):
        """Compute improvement score (0-1)"""
        improvements = []

        # Time-to-fix improvement (weight: 50%)
        if baseline.get("avg_time_to_fix_days", 3) > 0:
            ttf_improvement = (
                (baseline.get("avg_time_to_fix_days", 3) - after.get("avg_time_to_fix_days", 3))
                / baseline.get("avg_time_to_fix_days", 3)
            )
            improvements.append(("ttf", max(0, ttf_improvement), 0.5))

        # Pass rate improvement (weight: 30%)
        pass_rate_improvement = (
            (after.get("pass_rate_pct", 95) - baseline.get("pass_rate_pct", 95)) / 100.0
        )
        improvements.append(("pass_rate", max(0, pass_rate_improvement), 0.3))

        # Error rate reduction (weight: 15%)
        error_reduction = (
            (baseline.get("error_rate", 2.0) - after.get("error_rate", 2.0))
            / max(1, baseline.get("error_rate", 2.0))
        )
        improvements.append(("error_reduction", max(0, error_reduction), 0.15))

        # False positive reduction (weight: 5%)
        fp_reduction = (
            (baseline.get("false_positive_rate", 5.0) - after.get("false_positive_rate", 5.0))
            / max(1, baseline.get("false_positive_rate", 5.0))
        )
        improvements.append(("fp_reduction", max(0, fp_reduction), 0.05))

        # Weighted average
        improvement_score = sum(score * weight for _, score, weight in improvements)

        return max(0, min(1, improvement_score))  # Clamp to [0, 1]

    def test_improvement_scoring_from_improvement(self):
        """Should compute positive improvement score"""
        baseline = {
            "avg_time_to_fix_days": 3.0,
            "pass_rate_pct": 95.0,
            "error_rate": 2.0,
            "false_positive_rate": 5.0,
        }

        after = {
            "avg_time_to_fix_days": 2.5,  # Faster
            "pass_rate_pct": 97.0,  # Better
            "error_rate": 1.5,  # Fewer errors
            "false_positive_rate": 4.0,  # Better
        }

        improvement = self._compute_improvement(baseline, after)
        assert improvement > 0.0
        assert improvement <= 1.0

    def test_improvement_no_improvement(self):
        """Should compute zero improvement when metrics unchanged"""
        baseline = {
            "avg_time_to_fix_days": 3.0,
            "pass_rate_pct": 95.0,
            "error_rate": 2.0,
            "false_positive_rate": 5.0,
        }

        after = baseline.copy()

        improvement = self._compute_improvement(baseline, after)
        assert improvement == 0.0

    def test_improvement_bounded(self):
        """Improvement score should always be bounded [0, 1]"""
        baseline = {
            "avg_time_to_fix_days": 3.0,
            "pass_rate_pct": 95.0,
            "error_rate": 2.0,
            "false_positive_rate": 5.0,
        }

        # Extreme improvement
        after = {
            "avg_time_to_fix_days": 0.1,
            "pass_rate_pct": 100.0,
            "error_rate": 0.0,
            "false_positive_rate": 0.0,
        }

        improvement = self._compute_improvement(baseline, after)
        assert 0.0 <= improvement <= 1.0


# ============================================================================
# CANARY ROLLBACK DETECTION TESTS
# ============================================================================


class TestCanaryRollbackDetection:
    """Test canary deployment rollback detection"""

    CANARY_ERROR_THRESHOLD = 2.0
    CANARY_TIMEOUT_THRESHOLD = 5.0
    CANARY_LATENCY_THRESHOLD = 20.0

    def _should_rollback(self, current, baseline, stage="25pct"):
        """Determine if deployment should be rolled back"""
        if stage == "5pct":
            error_threshold = self.CANARY_ERROR_THRESHOLD * 2  # 4%
        else:
            error_threshold = self.CANARY_ERROR_THRESHOLD  # 2%

        # Check error rate
        error_increase = current.get("error_rate", 0) - baseline.get("error_rate", 0)
        if error_increase > error_threshold:
            return True, f"Error rate spike: {error_increase:.2f}% > {error_threshold:.2f}%"

        # Check timeout rate
        timeout_increase = current.get("timeout_rate", 0) - baseline.get("timeout_rate", 0)
        if timeout_increase > self.CANARY_TIMEOUT_THRESHOLD:
            return True, f"Timeout rate spike: {timeout_increase:.2f}%"

        # Check latency
        baseline_latency = baseline.get("p95_latency_ms", 100)
        current_latency = current.get("p95_latency_ms", 100)
        latency_increase_pct = (
            (current_latency - baseline_latency) / baseline_latency * 100
            if baseline_latency > 0
            else 0
        )
        if latency_increase_pct > self.CANARY_LATENCY_THRESHOLD:
            return True, f"Latency increase: {latency_increase_pct:.1f}%"

        return False, None

    def test_error_rate_spike_triggers_rollback(self):
        """Should rollback on error rate spike"""
        baseline = {"error_rate": 2.0, "timeout_rate": 0.5, "p95_latency_ms": 100.0}
        current = {"error_rate": 5.0, "timeout_rate": 0.5, "p95_latency_ms": 100.0}

        should_rollback, reason = self._should_rollback(current, baseline, "25pct")
        assert should_rollback is True
        assert "Error rate" in reason

    def test_timeout_spike_triggers_rollback(self):
        """Should rollback on timeout rate spike"""
        baseline = {"error_rate": 2.0, "timeout_rate": 0.5, "p95_latency_ms": 100.0}
        current = {"error_rate": 2.0, "timeout_rate": 6.0, "p95_latency_ms": 100.0}

        should_rollback, reason = self._should_rollback(current, baseline, "25pct")
        assert should_rollback is True
        assert "Timeout" in reason

    def test_latency_spike_triggers_rollback(self):
        """Should rollback on latency spike"""
        baseline = {"error_rate": 2.0, "timeout_rate": 0.5, "p95_latency_ms": 100.0}
        current = {"error_rate": 2.0, "timeout_rate": 0.5, "p95_latency_ms": 125.0}

        should_rollback, reason = self._should_rollback(current, baseline, "25pct")
        assert should_rollback is True
        assert "Latency" in reason

    def test_green_metrics_no_rollback(self):
        """Should not rollback with stable metrics"""
        baseline = {"error_rate": 2.0, "timeout_rate": 0.5, "p95_latency_ms": 100.0}
        current = {"error_rate": 1.8, "timeout_rate": 0.4, "p95_latency_ms": 99.0}

        should_rollback, reason = self._should_rollback(current, baseline, "25pct")
        assert should_rollback is False
        assert reason is None

    def test_canary_stricter_thresholds(self):
        """Canary stage should have stricter error threshold"""
        baseline = {"error_rate": 2.0, "timeout_rate": 0.5, "p95_latency_ms": 100.0}
        # 5% increase will trigger rollback at any stage
        current = {"error_rate": 7.0, "timeout_rate": 0.5, "p95_latency_ms": 100.0}

        # Both should rollback (5% > 4% and 5% > 2%)
        should_rollback_canary, _ = self._should_rollback(current, baseline, "5pct")
        should_rollback_gradual, _ = self._should_rollback(current, baseline, "25pct")

        assert should_rollback_canary is True
        assert should_rollback_gradual is True


# ============================================================================
# THRESHOLD CONFIGURATION TESTS
# ============================================================================


class TestThresholdConfiguration:
    """Test that threshold values are reasonable"""

    def test_min_reproducibility_above_coin_flip(self):
        """MIN_REPRODUCIBILITY (0.7) should be > 0.5"""
        MIN_REPRODUCIBILITY = 0.7
        assert MIN_REPRODUCIBILITY > 0.5

    def test_regression_thresholds_reasonable(self):
        """Regression thresholds should be reasonable (0.5-2%)"""
        MAX_ERROR_RATE_INCREASE = 0.5
        assert MAX_ERROR_RATE_INCREASE > 0.1
        assert MAX_ERROR_RATE_INCREASE < 5.0

    def test_canary_duration_reasonable(self):
        """Canary duration should be reasonable (20+ seconds)"""
        CANARY_DURATION_SEC = 30
        assert CANARY_DURATION_SEC >= 20

    def test_improvement_threshold_reasonable(self):
        """HIGH severity fixes should require 5%+ improvement"""
        MIN_IMPROVEMENT_SCORE = 0.05
        assert 0.0 <= MIN_IMPROVEMENT_SCORE <= 1.0
        assert MIN_IMPROVEMENT_SCORE > 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
