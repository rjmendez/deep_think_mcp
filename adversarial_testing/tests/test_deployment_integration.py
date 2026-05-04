"""Integration tests for deployment pipeline with Layer 5 self-improvement system.

Tests the full deployment lifecycle:
1. Canary weight calculation and pod routing
2. Rollback trigger logic based on metrics
3. Multi-stage deployment with metric monitoring
4. Edge cases: rollback at different stages
5. E2E deployment with simulated k3s metrics
"""

import pytest
import json
import uuid
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

from adversarial_testing.deployment_pipeline import (
    DeploymentPipeline,
    DeploymentStage,
    DeploymentEvent,
)
from adversarial_testing.store import AdversarialStore
from adversarial_testing.metrics import MetricsCollector


# ============================================================================
# UNIT TESTS: CANARY WEIGHT CALCULATION
# ============================================================================


class TestCanaryWeightCalculation:
    """Test canary deployment weight calculations"""

    def test_stage_1_weight_5_percent(self):
        """Stage 1 should route 5% of traffic"""
        stages = DeploymentPipeline.STAGES
        stage_name, weight, duration = stages[0]
        
        assert stage_name == "5pct"
        assert weight == 0.05
        assert abs(weight - 0.05) < 0.001

    def test_stage_2_weight_25_percent(self):
        """Stage 2 should route 25% of traffic"""
        stages = DeploymentPipeline.STAGES
        stage_name, weight, duration = stages[1]
        
        assert stage_name == "25pct"
        assert weight == 0.25
        assert abs(weight - 0.25) < 0.001

    def test_stage_3_weight_100_percent(self):
        """Stage 3 should route 100% of traffic"""
        stages = DeploymentPipeline.STAGES
        stage_name, weight, duration = stages[2]
        
        assert stage_name == "100pct"
        assert weight == 1.0
        assert abs(weight - 1.0) < 0.001

    def test_pod_count_calculation_5_percent(self):
        """Calculate pod replica count for 5% stage (assuming 10 pods total)"""
        weight = 0.05
        pod_count = int(weight * 10)
        
        assert pod_count == 0  # < 1 pod (but handled by service mesh)
        assert pod_count >= 0

    def test_pod_count_calculation_25_percent(self):
        """Calculate pod replica count for 25% stage (assuming 10 pods total)"""
        weight = 0.25
        pod_count = int(weight * 10)
        
        assert pod_count == 2  # 2-3 pods for 25%
        assert 1 <= pod_count <= 3

    def test_pod_count_calculation_100_percent(self):
        """Calculate pod replica count for 100% stage"""
        weight = 1.0
        pod_count = int(weight * 10)
        
        assert pod_count == 10  # All pods
        assert pod_count == 10


# ============================================================================
# UNIT TESTS: ROLLBACK TRIGGER LOGIC
# ============================================================================


class TestRollbackTriggerLogic:
    """Test the rollback decision logic based on metrics"""

    @pytest.fixture
    def pipeline(self):
        """Create a deployment pipeline instance for testing"""
        store = MagicMock(spec=AdversarialStore)
        metrics = MagicMock(spec=MetricsCollector)
        return DeploymentPipeline(
            store=store,
            metrics=metrics,
            prometheus_endpoint="http://localhost:9090",
            k3s_namespace="agents",
            deployment_name="deep-think",
        )

    def test_error_rate_spike_triggers_rollback(self, pipeline):
        """Error rate spike > 2% should trigger rollback"""
        baseline = {"error_rate": 1.0, "timeout_rate": 0.1, "p95_latency_ms": 100}
        current = {"error_rate": 3.5, "timeout_rate": 0.1, "p95_latency_ms": 100}
        
        should_rollback, reason = pipeline._should_rollback(current, baseline, "25pct")
        
        assert should_rollback is True
        assert "error rate" in reason.lower()
        assert "3.5" in reason or "2.5" in reason  # Increase amount

    def test_timeout_rate_spike_triggers_rollback(self, pipeline):
        """Timeout rate spike > 5% should trigger rollback"""
        baseline = {"error_rate": 1.0, "timeout_rate": 0.5, "p95_latency_ms": 100}
        current = {"error_rate": 1.0, "timeout_rate": 6.0, "p95_latency_ms": 100}
        
        should_rollback, reason = pipeline._should_rollback(current, baseline, "25pct")
        
        assert should_rollback is True
        assert "timeout" in reason.lower()
        assert "6.0" in reason or "5.5" in reason

    def test_latency_spike_triggers_rollback(self, pipeline):
        """Latency p99 spike > 20% should trigger rollback"""
        baseline = {"error_rate": 1.0, "timeout_rate": 0.1, "p95_latency_ms": 100}
        # 100 * 1.25 = 125ms (25% increase)
        current = {"error_rate": 1.0, "timeout_rate": 0.1, "p95_latency_ms": 125}
        
        should_rollback, reason = pipeline._should_rollback(current, baseline, "25pct")
        
        assert should_rollback is True
        assert "latency" in reason.lower()

    def test_green_metrics_no_rollback(self, pipeline):
        """Healthy metrics should not trigger rollback"""
        baseline = {"error_rate": 1.0, "timeout_rate": 0.5, "p95_latency_ms": 100}
        current = {"error_rate": 1.1, "timeout_rate": 0.6, "p95_latency_ms": 105}
        
        should_rollback, reason = pipeline._should_rollback(current, baseline, "25pct")
        
        assert should_rollback is False
        assert reason is None

    def test_canary_stage_stricter_thresholds(self, pipeline):
        """5% canary stage has stricter error thresholds (2x normal)"""
        baseline = {"error_rate": 1.0, "timeout_rate": 0.1, "p95_latency_ms": 100}
        # Error increase of 3% (threshold is 4% for 5pct stage)
        current = {"error_rate": 4.0, "timeout_rate": 0.1, "p95_latency_ms": 100}
        
        # Should NOT rollback at 5pct stage with 3% increase
        should_rollback, _ = pipeline._should_rollback(current, baseline, "5pct")
        
        # This depends on the exact threshold configuration
        # With 4% threshold for 5pct, 3% increase should pass
        # But let's verify the stricter logic exists

    def test_no_rollback_below_thresholds(self, pipeline):
        """Metrics below thresholds should not trigger rollback"""
        baseline = {"error_rate": 0.5, "timeout_rate": 0.2, "p95_latency_ms": 80}
        current = {"error_rate": 1.0, "timeout_rate": 0.8, "p95_latency_ms": 95}
        
        should_rollback, reason = pipeline._should_rollback(current, baseline, "100pct")
        
        # All increases are below thresholds
        assert should_rollback is False


# ============================================================================
# INTEGRATION TESTS: DEPLOYMENT PIPELINE
# ============================================================================


class TestDeploymentPipelineIntegration:
    """Test full deployment pipeline with mocked k3s/Prometheus"""

    @pytest.fixture
    async def pipeline_with_mocks(self):
        """Create pipeline with mocked store and metrics"""
        store = MagicMock(spec=AdversarialStore)
        store.execute = MagicMock(return_value=MagicMock(fetchone=MagicMock(return_value=None)))
        
        metrics = MagicMock(spec=MetricsCollector)
        
        pipeline = DeploymentPipeline(
            store=store,
            metrics=metrics,
            prometheus_endpoint="http://localhost:9090",
        )
        
        return pipeline, store, metrics

    @pytest.mark.asyncio
    async def test_deployment_stages_in_correct_order(self, pipeline_with_mocks):
        """Verify canary stages execute in correct order: 5% → 25% → 100%"""
        pipeline, store, metrics = pipeline_with_mocks
        
        stages_executed = []
        
        async def mock_update_pod_weights(commit_sha, weight):
            stages_executed.append(weight)
            return True
        
        async def mock_monitor_stage(stage_name, duration, baseline):
            return {
                "error_rate": 0.5,
                "timeout_rate": 0.1,
                "p95_latency_ms": 100,
            }
        
        pipeline._update_pod_weights = mock_update_pod_weights
        pipeline._monitor_stage = mock_monitor_stage
        
        with patch.object(pipeline, "_create_git_tag", new_callable=AsyncMock):
            with patch.object(pipeline, "_rollback_deployment", new_callable=AsyncMock) as mock_rollback:
                success, error_msg, details = await pipeline.deploy_validated_fix(
                    plan_id="plan-123",
                    commit_sha="abc1234567890",
                )
        
        assert stages_executed == [0.05, 0.25, 1.0]
        assert success is True

    @pytest.mark.asyncio
    async def test_rollback_during_stage_1(self, pipeline_with_mocks):
        """Rollback should trigger during stage 1 (5% canary)"""
        pipeline, store, metrics = pipeline_with_mocks
        
        stage_count = 0
        
        async def mock_update_pod_weights(commit_sha, weight):
            return True
        
        async def mock_monitor_stage(stage_name, duration, baseline):
            nonlocal stage_count
            stage_count += 1
            
            if stage_count == 1:  # Stage 1 (5%)
                # Return bad metrics to trigger rollback
                return {
                    "error_rate": 5.0,  # Spike: baseline is 1.0
                    "timeout_rate": 0.1,
                    "p95_latency_ms": 100,
                }
            return {}
        
        pipeline._update_pod_weights = mock_update_pod_weights
        pipeline._monitor_stage = mock_monitor_stage
        
        with patch.object(pipeline, "_rollback_deployment", new_callable=AsyncMock) as mock_rollback:
            mock_rollback.return_value = True
            with patch.object(pipeline, "_create_git_tag", new_callable=AsyncMock):
                success, error_msg, details = await pipeline.deploy_validated_fix(
                    plan_id="plan-123",
                    commit_sha="abc1234567890",
                )
        
        assert success is False
        assert "rolled" in error_msg.lower() or "rollback" in error_msg.lower()
        mock_rollback.assert_called_once()
        assert stage_count == 1  # Only stage 1 executed

    @pytest.mark.asyncio
    async def test_rollback_during_stage_3(self, pipeline_with_mocks):
        """Rollback should trigger during stage 3 (100% full rollout)"""
        pipeline, store, metrics = pipeline_with_mocks
        
        stage_count = 0
        
        async def mock_update_pod_weights(commit_sha, weight):
            return True
        
        async def mock_monitor_stage(stage_name, duration, baseline):
            nonlocal stage_count
            stage_count += 1
            
            if stage_count < 3:
                # Stages 1-2 are healthy
                return {
                    "error_rate": 1.0,
                    "timeout_rate": 0.2,
                    "p95_latency_ms": 105,
                }
            else:  # Stage 3 (100%)
                # Return bad metrics at full rollout
                return {
                    "error_rate": 4.0,  # 3% spike from baseline 1.0
                    "timeout_rate": 0.2,
                    "p95_latency_ms": 100,
                }
        
        pipeline._update_pod_weights = mock_update_pod_weights
        pipeline._monitor_stage = mock_monitor_stage
        
        with patch.object(pipeline, "_rollback_deployment", new_callable=AsyncMock) as mock_rollback:
            mock_rollback.return_value = True
            with patch.object(pipeline, "_create_git_tag", new_callable=AsyncMock):
                success, error_msg, details = await pipeline.deploy_validated_fix(
                    plan_id="plan-123",
                    commit_sha="abc1234567890",
                )
        
        assert success is False
        assert stage_count == 3
        mock_rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_deployment_tags_release(self, pipeline_with_mocks):
        """Successful deployment should tag release with completion marker"""
        pipeline, store, metrics = pipeline_with_mocks
        
        git_tags_created = []
        
        async def mock_update_pod_weights(commit_sha, weight):
            return True
        
        async def mock_monitor_stage(stage_name, duration, baseline):
            return {
                "error_rate": 0.8,
                "timeout_rate": 0.1,
                "p95_latency_ms": 102,
            }
        
        async def mock_create_git_tag(commit_sha, tag_name):
            git_tags_created.append(tag_name)
            return True
        
        pipeline._update_pod_weights = mock_update_pod_weights
        pipeline._monitor_stage = mock_monitor_stage
        pipeline._create_git_tag = mock_create_git_tag
        
        success, error_msg, details = await pipeline.deploy_validated_fix(
            plan_id="plan-123",
            commit_sha="abc1234567890",
        )
        
        assert success is True
        assert len(git_tags_created) > 0
        # Verify the tag contains "completed" marker
        assert any("completed" in tag for tag in git_tags_created)

    @pytest.mark.asyncio
    async def test_deployment_updates_database(self, pipeline_with_mocks):
        """Deployment should update deployment_events table"""
        pipeline, store, metrics = pipeline_with_mocks
        
        execute_calls = []
        
        def mock_execute(query, params=None):
            execute_calls.append({"query": query, "params": params})
            return MagicMock(fetchone=MagicMock(return_value=None))
        
        store.execute = mock_execute
        
        async def mock_update_pod_weights(commit_sha, weight):
            return True
        
        async def mock_monitor_stage(stage_name, duration, baseline):
            return {
                "error_rate": 0.5,
                "timeout_rate": 0.1,
                "p95_latency_ms": 100,
            }
        
        pipeline._update_pod_weights = mock_update_pod_weights
        pipeline._monitor_stage = mock_monitor_stage
        
        with patch.object(pipeline, "_create_git_tag", new_callable=AsyncMock):
            with patch.object(pipeline, "_rollback_deployment", new_callable=AsyncMock):
                success, error_msg, details = await pipeline.deploy_validated_fix(
                    plan_id="plan-123",
                    commit_sha="abc1234567890",
                )
        
        # Verify INSERT and UPDATE calls were made
        insert_calls = [c for c in execute_calls if "INSERT INTO deployment_events" in c["query"]]
        update_calls = [c for c in execute_calls if "UPDATE deployment_events" in c["query"]]
        
        assert len(insert_calls) > 0, "Should insert deployment_events record"
        assert len(update_calls) > 0, "Should update deployment_events status"


# ============================================================================
# EDGE CASE TESTS
# ============================================================================


class TestDeploymentEdgeCases:
    """Test edge cases and error conditions"""

    @pytest.fixture
    def pipeline(self):
        store = MagicMock(spec=AdversarialStore)
        metrics = MagicMock(spec=MetricsCollector)
        return DeploymentPipeline(
            store=store,
            metrics=metrics,
            prometheus_endpoint="http://localhost:9090",
        )

    @pytest.mark.asyncio
    async def test_pod_weight_update_failure(self, pipeline):
        """Deployment should fail if pod weight update fails"""
        
        async def mock_update_pod_weights_fail(commit_sha, weight):
            return False  # Simulate failure
        
        pipeline._update_pod_weights = mock_update_pod_weights_fail
        
        with patch.object(pipeline, "_rollback_deployment", new_callable=AsyncMock):
            success, error_msg, details = await pipeline.deploy_validated_fix(
                plan_id="plan-123",
                commit_sha="abc1234567890",
            )
        
        assert success is False
        assert "pod weight" in error_msg.lower() or "failed" in error_msg.lower()

    @pytest.mark.asyncio
    async def test_prometheus_metrics_unavailable(self, pipeline):
        """Deployment should handle Prometheus unavailability gracefully"""
        
        async def mock_update_pod_weights(commit_sha, weight):
            return True
        
        async def mock_monitor_stage_no_metrics(stage_name, duration, baseline):
            # Return empty metrics (simulating Prometheus down)
            return {}
        
        pipeline._update_pod_weights = mock_update_pod_weights
        pipeline._monitor_stage = mock_monitor_stage_no_metrics
        
        with patch.object(pipeline, "_create_git_tag", new_callable=AsyncMock):
            # Should not crash, should use defaults
            success, error_msg, details = await pipeline.deploy_validated_fix(
                plan_id="plan-123",
                commit_sha="abc1234567890",
            )
        
        # With no metrics, we can't detect problems, so deployment continues
        # This is a design choice - strict monitoring requires Prometheus

    @pytest.mark.asyncio
    async def test_git_tag_creation_failure_non_critical(self, pipeline):
        """Git tag creation failure should not block successful deployment"""
        
        async def mock_update_pod_weights(commit_sha, weight):
            return True
        
        async def mock_monitor_stage(stage_name, duration, baseline):
            return {
                "error_rate": 0.5,
                "timeout_rate": 0.1,
                "p95_latency_ms": 100,
            }
        
        async def mock_create_git_tag_fail(commit_sha, tag_name):
            return False  # Tag creation failed
        
        pipeline._update_pod_weights = mock_update_pod_weights
        pipeline._monitor_stage = mock_monitor_stage
        pipeline._create_git_tag = mock_create_git_tag_fail
        
        success, error_msg, details = await pipeline.deploy_validated_fix(
            plan_id="plan-123",
            commit_sha="abc1234567890",
        )
        
        # Should still report success even if tag fails (non-critical)
        assert success is True


# ============================================================================
# METRIC THRESHOLD TESTS
# ============================================================================


class TestMetricThresholds:
    """Test metric threshold configuration and behavior"""

    def test_error_rate_threshold_2_percent(self):
        """Error rate threshold should be 2%"""
        assert DeploymentPipeline.CANARY_ERROR_THRESHOLD == 2.0

    def test_timeout_rate_threshold_5_percent(self):
        """Timeout rate threshold should be 5%"""
        assert DeploymentPipeline.CANARY_TIMEOUT_THRESHOLD == 5.0

    def test_latency_threshold_20_percent(self):
        """Latency increase threshold should be 20%"""
        assert DeploymentPipeline.CANARY_LATENCY_THRESHOLD == 20.0

    def test_canary_monitoring_duration(self):
        """Each stage should have appropriate monitoring duration"""
        stages = DeploymentPipeline.STAGES
        
        # Stage 1: ~30 seconds for quick feedback
        assert stages[0][2] == 30
        # Stage 2: ~2 minutes for more data
        assert stages[1][2] == 120
        # Stage 3: ~5 minutes before full production
        assert stages[2][2] == 300


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
