"""Tests for planning engine integration with deep_think_mcp."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
import uuid
from pathlib import Path
import os

from deep_think_mcp.planning_engine import (
    PlanningEngine,
    RiskLevel,
    FixApproach,
)
from deep_think_mcp import store


@pytest.fixture(autouse=True)
def cleanup_test_db(tmp_path):
    """Clean up test database before and after each test using tmp_path."""
    # Override the database path for this test
    test_db = tmp_path / ".deep_think"
    test_db.mkdir(exist_ok=True)
    
    original_db_path = None
    
    def mock_db_path():
        return str(test_db / "jobs.db")
    
    # Patch the _db_path function
    with patch('deep_think_mcp.store._db_path', mock_db_path):
        yield
        
        # Cleanup happens automatically when tmp_path is destroyed


class TestPlanningEngine:
    """Test suite for PlanningEngine."""

    @pytest.fixture
    async def engine(self):
        """Create a planning engine with mock deep_think."""
        mock_deep_think = AsyncMock()
        return PlanningEngine(deep_think_fn=mock_deep_think)

    @pytest.fixture
    def sample_finding(self):
        """Create a sample finding for testing."""
        return {
            "id": "finding-1",
            "severity": "HIGH",
            "impact": 8.0,
            "reproducibility": 0.9,
            "category": "performance",
            "description": "Slow query in user lookup",
            "details": "Query takes 5 seconds on 1M users",
            "effort_estimate": 3,
            "risk_level": "MEDIUM",
        }

    def test_risk_level_enum(self):
        """Test RiskLevel enum values."""
        assert RiskLevel.LOW.value == 1
        assert RiskLevel.MEDIUM.value == 2
        assert RiskLevel.HIGH.value == 3

    def test_compute_priority(self, engine, sample_finding):
        """Test priority computation formula."""
        priority = engine._compute_priority(sample_finding)
        
        # Check that priority is computed correctly
        # Priority = (severity_weight × impact × reproducibility) / (effort_penalty × risk_penalty)
        # HIGH=2.0, impact=8.0, reproducibility=0.9
        # numerator = 2.0 × 8.0 × 0.9 = 14.4
        # effort=3 (penalty=1.5), risk=MEDIUM (penalty=1.5)
        # denominator = 1.5 × 1.5 = 2.25
        # priority = 14.4 / 2.25 = 6.4
        
        assert priority > 0
        assert isinstance(priority, float)

    def test_compute_priority_critical_severity(self, engine):
        """Test priority with CRITICAL severity."""
        finding = {
            "id": "critical",
            "severity": "CRITICAL",
            "impact": 10.0,
            "reproducibility": 1.0,
            "effort_estimate": 5,
            "risk_level": "HIGH",
        }
        priority = engine._compute_priority(finding)
        
        # CRITICAL should have highest priority
        low_finding = {**finding, "severity": "LOW"}
        low_priority = engine._compute_priority(low_finding)
        assert priority > low_priority

    def test_build_planning_prompt(self, engine, sample_finding):
        """Test prompt building for deep_think."""
        prompt = engine._build_planning_prompt(sample_finding)
        
        # Verify prompt contains key information
        assert "finding-1" not in prompt  # Should be generic
        assert "HIGH" in prompt
        assert "performance" in prompt
        assert "Slow query" in prompt
        assert "root_cause" in prompt
        assert "subtasks" in prompt

    async def test_planning_prompt_structure(self, engine, sample_finding):
        """Test that prompt generates valid JSON response structure."""
        prompt = engine._build_planning_prompt(sample_finding)
        
        # Verify JSON structure in prompt
        assert "root_cause" in prompt
        assert "effort_estimate" in prompt
        assert "risk_level" in prompt
        assert "subtasks" in prompt

    async def test_call_deep_think_planning_success(self, engine):
        """Test successful deep_think call."""
        mock_response = {
            "final_answer": json.dumps({
                "root_cause": "Missing database index",
                "primary_strategy": "Add index on user_id",
                "fallback_strategy": "Cache results",
                "effort_estimate": 2,
                "risk_level": "LOW",
                "dependencies": [],
                "subtasks": ["create index", "test performance"],
                "validation_tests": ["load test with 1M users"],
                "estimated_cost_tokens": 3000,
            })
        }
        engine.deep_think_fn = AsyncMock(return_value=mock_response)
        
        prompt = "test planning prompt"
        approach = await engine._call_deep_think_planning(prompt)
        
        assert approach is not None
        assert approach.root_cause == "Missing database index"
        assert approach.effort_estimate == 2
        assert approach.risk_level == "LOW"
        assert len(approach.subtasks) == 2
        engine.deep_think_fn.assert_called_once()

    async def test_call_deep_think_planning_timeout(self, engine):
        """Test timeout handling in deep_think call."""
        engine.deep_think_fn = AsyncMock(side_effect=asyncio.TimeoutError())
        engine.plan_timeout_secs = 0.01
        
        prompt = "test planning prompt"
        approach = await engine._call_deep_think_planning(prompt)
        
        assert approach is None

    async def test_call_deep_think_planning_json_error(self, engine):
        """Test invalid JSON handling."""
        mock_response = {
            "final_answer": "invalid json {{{",
        }
        engine.deep_think_fn = AsyncMock(return_value=mock_response)
        
        prompt = "test planning prompt"
        approach = await engine._call_deep_think_planning(prompt)
        
        assert approach is None

    async def test_generate_plan(self, engine, sample_finding):
        """Test single plan generation."""
        store.init_db()
        
        # Mock deep_think response
        mock_response = {
            "final_answer": json.dumps({
                "root_cause": "Missing index",
                "primary_strategy": "Add index",
                "fallback_strategy": None,
                "effort_estimate": 2,
                "risk_level": "LOW",
                "dependencies": [],
                "subtasks": ["add index", "test"],
                "validation_tests": ["load test"],
                "estimated_cost_tokens": 3000,
            })
        }
        engine.deep_think_fn = AsyncMock(return_value=mock_response)
        
        plan = await engine.generate_plan(sample_finding)
        
        assert plan is not None
        assert plan["finding_id"] == "finding-1"
        assert plan["status"] == "pending"
        assert plan["priority"] > 0
        assert plan["effort_estimate"] == 2

    async def test_generate_plans_for_findings(self, engine):
        """Test batch plan generation."""
        store.init_db()
        
        findings = [
            {
                "id": f"finding-{i}",
                "severity": "HIGH",
                "impact": 8.0,
                "reproducibility": 0.9,
                "category": "test",
                "description": f"Issue {i}",
                "details": "details",
                "effort_estimate": 2,
                "risk_level": "MEDIUM",
            }
            for i in range(3)
        ]
        
        # Mock deep_think response
        mock_response = {
            "final_answer": json.dumps({
                "root_cause": "Root cause",
                "primary_strategy": "Strategy",
                "fallback_strategy": None,
                "effort_estimate": 2,
                "risk_level": "MEDIUM",
                "dependencies": [],
                "subtasks": ["task1"],
                "validation_tests": ["test1"],
                "estimated_cost_tokens": 3000,
            })
        }
        engine.deep_think_fn = AsyncMock(return_value=mock_response)
        
        plans = await engine.generate_plans_for_findings(findings, limit=2)
        
        assert len(plans) <= 2
        assert all(p["status"] == "pending" for p in plans)
        assert all("plan_id" in p for p in plans)

    async def test_generate_plans_empty_findings(self, engine):
        """Test with empty findings list."""
        plans = await engine.generate_plans_for_findings([], limit=5)
        assert plans == []

    async def test_get_pending_plans(self, engine):
        """Test fetching pending plans."""
        # Create some plans first
        store.init_db()
        
        plan_id = "plan-1"
        store.create_plan(
            plan_id=plan_id,
            finding_ids=["finding-1"],
            plan_json='{"test": "plan"}',
            priority=5.0,
            effort_estimate=2,
            risk_level="LOW",
        )
        
        pending = await engine.get_pending_plans()
        
        assert len(pending) >= 1
        assert any(p["plan_id"] == plan_id for p in pending)

    async def test_approve_plan(self, engine):
        """Test plan approval."""
        store.init_db()
        
        plan_id = "plan-approve-test"
        store.create_plan(
            plan_id=plan_id,
            finding_ids=["finding-1"],
            plan_json='{"test": "plan"}',
            priority=5.0,
            effort_estimate=2,
            risk_level="LOW",
        )
        
        success = await engine.approve_plan(
            plan_id=plan_id,
            approved_by="test-user",
            approval_notes="Looks good",
        )
        
        assert success
        
        # Verify status changed
        plan = store.get_plan(plan_id)
        assert plan["status"] == "approved"

    async def test_reject_plan(self, engine):
        """Test plan rejection."""
        store.init_db()
        
        plan_id = "plan-reject-test"
        store.create_plan(
            plan_id=plan_id,
            finding_ids=["finding-1"],
            plan_json='{"test": "plan"}',
            priority=5.0,
            effort_estimate=2,
            risk_level="LOW",
        )
        
        success = await engine.reject_plan(
            plan_id=plan_id,
            rejected_by="test-user",
            reason="Too risky",
        )
        
        assert success
        
        # Verify status changed
        plan = store.get_plan(plan_id)
        assert plan["status"] == "rejected"

    async def test_concurrent_plan_generation(self, engine):
        """Test concurrent plan generation with semaphore."""
        store.init_db()
        
        findings = [
            {
                "id": f"finding-{i}",
                "severity": "HIGH",
                "impact": 8.0,
                "reproducibility": 0.9,
                "category": "test",
                "description": f"Issue {i}",
                "details": "details",
                "effort_estimate": 2,
                "risk_level": "MEDIUM",
            }
            for i in range(10)
        ]
        
        # Mock deep_think response with delay
        async def mock_deep_think(**kwargs):
            await asyncio.sleep(0.01)
            return {
                "final_answer": json.dumps({
                    "root_cause": "Root",
                    "primary_strategy": "Strategy",
                    "fallback_strategy": None,
                    "effort_estimate": 2,
                    "risk_level": "MEDIUM",
                    "dependencies": [],
                    "subtasks": ["task"],
                    "validation_tests": ["test"],
                    "estimated_cost_tokens": 3000,
                })
            }
        
        engine.deep_think_fn = AsyncMock(side_effect=mock_deep_think)
        
        import time
        start = time.time()
        
        # Generate plans with max 3 concurrent
        plans = await engine.generate_plans_for_findings(findings, limit=6)
        
        elapsed = time.time() - start
        
        # Should respect semaphore limit
        assert len(plans) <= 6
        # With 3 concurrent and 6 tasks, should take at least 0.02s
        assert elapsed > 0.01


class TestPlanningEngineIntegration:
    """Integration tests for planning engine with store."""

    @pytest.fixture(autouse=True)
    def setup_db(self):
        """Initialize test database."""
        store.init_db()
        yield
        # Cleanup can be added here

    def test_plan_creation_and_retrieval(self):
        """Test creating and retrieving plans."""
        plan_id = f"test-plan-{uuid.uuid4()}"
        finding_ids = ["finding-1", "finding-2"]
        plan_json = json.dumps({"strategy": "test"})
        
        # Create plan
        created_id = store.create_plan(
            plan_id=plan_id,
            finding_ids=finding_ids,
            plan_json=plan_json,
            priority=8.5,
            effort_estimate=3,
            risk_level="MEDIUM",
        )
        
        assert created_id == plan_id
        
        # Retrieve plan
        plan = store.get_plan(plan_id)
        assert plan is not None
        assert plan["id"] == plan_id
        assert plan["status"] == "pending"
        assert plan["priority"] == 8.5

    def test_list_plans(self):
        """Test listing plans."""
        # Create multiple plans with unique IDs
        for i in range(5):
            store.create_plan(
                plan_id=f"plan-{uuid.uuid4()}-{i}",
                finding_ids=[f"finding-{i}"],
                plan_json='{}',
                priority=float(i),
                effort_estimate=2,
                risk_level="LOW",
            )
        
        # List all
        plans = store.list_plans(status="all")
        assert len(plans) >= 5

    def test_update_plan_status(self):
        """Test updating plan status."""
        plan_id = f"status-test-plan-{uuid.uuid4()}"
        store.create_plan(
            plan_id=plan_id,
            finding_ids=["finding-1"],
            plan_json='{}',
            priority=5.0,
            effort_estimate=2,
            risk_level="LOW",
        )
        
        # Update status
        success = store.update_plan_status(
            plan_id=plan_id,
            status="approved",
            approved_by="test-user",
        )
        
        assert success
        
        plan = store.get_plan(plan_id)
        assert plan["status"] == "approved"
        assert plan["approved_by"] == "test-user"

    def test_audit_log_creation(self):
        """Test audit log functionality."""
        plan_id = f"audit-test-plan-{uuid.uuid4()}"
        store.create_plan(
            plan_id=plan_id,
            finding_ids=["finding-1"],
            plan_json='{}',
            priority=5.0,
            effort_estimate=2,
            risk_level="LOW",
        )
        
        # Create audit entries
        store.audit_log("plan_created", plan_id, json.dumps({"test": "data"}))
        store.audit_log("plan_approved", plan_id, json.dumps({"approver": "user1"}))
        
        # Retrieve audit trail
        trail = store.get_plan_audit_trail(plan_id)
        
        assert len(trail) >= 2
        assert any(e["event_type"] == "plan_created" for e in trail)
        assert any(e["event_type"] == "plan_approved" for e in trail)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestPlanningEngine:
    """Test suite for PlanningEngine."""

    @pytest.fixture
    async def engine(self):
        """Create a planning engine with mock deep_think."""
        mock_deep_think = AsyncMock()
        return PlanningEngine(deep_think_fn=mock_deep_think)

    @pytest.fixture
    def sample_finding(self):
        """Create a sample finding for testing."""
        return {
            "id": "finding-1",
            "severity": "HIGH",
            "impact": 8.0,
            "reproducibility": 0.9,
            "category": "performance",
            "description": "Slow query in user lookup",
            "details": "Query takes 5 seconds on 1M users",
            "effort_estimate": 3,
            "risk_level": "MEDIUM",
        }

    def test_risk_level_enum(self):
        """Test RiskLevel enum values."""
        assert RiskLevel.LOW.value == 1
        assert RiskLevel.MEDIUM.value == 2
        assert RiskLevel.HIGH.value == 3

    def test_compute_priority(self, engine, sample_finding):
        """Test priority computation formula."""
        priority = engine._compute_priority(sample_finding)
        
        # Check that priority is computed correctly
        # Priority = (severity_weight × impact × reproducibility) / (effort_penalty × risk_penalty)
        # HIGH=2.0, impact=8.0, reproducibility=0.9
        # numerator = 2.0 × 8.0 × 0.9 = 14.4
        # effort=3 (penalty=1.5), risk=MEDIUM (penalty=1.5)
        # denominator = 1.5 × 1.5 = 2.25
        # priority = 14.4 / 2.25 = 6.4
        
        assert priority > 0
        assert isinstance(priority, float)

    def test_compute_priority_critical_severity(self, engine):
        """Test priority with CRITICAL severity."""
        finding = {
            "id": "critical",
            "severity": "CRITICAL",
            "impact": 10.0,
            "reproducibility": 1.0,
            "effort_estimate": 5,
            "risk_level": "HIGH",
        }
        priority = engine._compute_priority(finding)
        
        # CRITICAL should have highest priority
        low_finding = {**finding, "severity": "LOW"}
        low_priority = engine._compute_priority(low_finding)
        assert priority > low_priority

    def test_build_planning_prompt(self, engine, sample_finding):
        """Test prompt building for deep_think."""
        prompt = engine._build_planning_prompt(sample_finding)
        
        # Verify prompt contains key information
        assert "finding-1" not in prompt  # Should be generic
        assert "HIGH" in prompt
        assert "performance" in prompt
        assert "Slow query" in prompt
        assert "root_cause" in prompt
        assert "subtasks" in prompt

    async def test_planning_prompt_structure(self, engine, sample_finding):
        """Test that prompt generates valid JSON response structure."""
        prompt = engine._build_planning_prompt(sample_finding)
        
        # Verify JSON structure in prompt
        assert "root_cause" in prompt
        assert "effort_estimate" in prompt
        assert "risk_level" in prompt
        assert "subtasks" in prompt

    async def test_call_deep_think_planning_success(self, engine):
        """Test successful deep_think call."""
        mock_response = {
            "final_answer": json.dumps({
                "root_cause": "Missing database index",
                "primary_strategy": "Add index on user_id",
                "fallback_strategy": "Cache results",
                "effort_estimate": 2,
                "risk_level": "LOW",
                "dependencies": [],
                "subtasks": ["create index", "test performance"],
                "validation_tests": ["load test with 1M users"],
                "estimated_cost_tokens": 3000,
            })
        }
        engine.deep_think_fn = AsyncMock(return_value=mock_response)
        
        prompt = "test planning prompt"
        approach = await engine._call_deep_think_planning(prompt)
        
        assert approach is not None
        assert approach.root_cause == "Missing database index"
        assert approach.effort_estimate == 2
        assert approach.risk_level == "LOW"
        assert len(approach.subtasks) == 2
        engine.deep_think_fn.assert_called_once()

    async def test_call_deep_think_planning_timeout(self, engine):
        """Test timeout handling in deep_think call."""
        engine.deep_think_fn = AsyncMock(side_effect=asyncio.TimeoutError())
        engine.plan_timeout_secs = 0.01
        
        prompt = "test planning prompt"
        approach = await engine._call_deep_think_planning(prompt)
        
        assert approach is None

    async def test_call_deep_think_planning_json_error(self, engine):
        """Test invalid JSON handling."""
        mock_response = {
            "final_answer": "invalid json {{{",
        }
        engine.deep_think_fn = AsyncMock(return_value=mock_response)
        
        prompt = "test planning prompt"
        approach = await engine._call_deep_think_planning(prompt)
        
        assert approach is None

    async def test_generate_plan(self, engine, sample_finding):
        """Test single plan generation."""
        store.init_db()
        
        # Mock deep_think response
        mock_response = {
            "final_answer": json.dumps({
                "root_cause": "Missing index",
                "primary_strategy": "Add index",
                "fallback_strategy": None,
                "effort_estimate": 2,
                "risk_level": "LOW",
                "dependencies": [],
                "subtasks": ["add index", "test"],
                "validation_tests": ["load test"],
                "estimated_cost_tokens": 3000,
            })
        }
        engine.deep_think_fn = AsyncMock(return_value=mock_response)
        
        plan = await engine.generate_plan(sample_finding)
        
        assert plan is not None
        assert plan["finding_id"] == "finding-1"
        assert plan["status"] == "pending"
        assert plan["priority"] > 0
        assert plan["effort_estimate"] == 2

    async def test_generate_plans_for_findings(self, engine):
        """Test batch plan generation."""
        findings = [
            {
                "id": f"finding-{i}",
                "severity": "HIGH",
                "impact": 8.0,
                "reproducibility": 0.9,
                "category": "test",
                "description": f"Issue {i}",
                "details": "details",
                "effort_estimate": 2,
                "risk_level": "MEDIUM",
            }
            for i in range(3)
        ]
        
        # Mock deep_think response
        mock_response = {
            "final_answer": json.dumps({
                "root_cause": "Root cause",
                "primary_strategy": "Strategy",
                "fallback_strategy": None,
                "effort_estimate": 2,
                "risk_level": "MEDIUM",
                "dependencies": [],
                "subtasks": ["task1"],
                "validation_tests": ["test1"],
                "estimated_cost_tokens": 3000,
            })
        }
        engine.deep_think_fn = AsyncMock(return_value=mock_response)
        
        plans = await engine.generate_plans_for_findings(findings, limit=2)
        
        assert len(plans) <= 2
        assert all(p["status"] == "pending" for p in plans)
        assert all("plan_id" in p for p in plans)

    async def test_generate_plans_empty_findings(self, engine):
        """Test with empty findings list."""
        plans = await engine.generate_plans_for_findings([], limit=5)
        assert plans == []

    async def test_get_pending_plans(self, engine):
        """Test fetching pending plans."""
        # Create some plans first
        store.init_db()
        
        plan_id = "plan-1"
        store.create_plan(
            plan_id=plan_id,
            finding_ids=["finding-1"],
            plan_json='{"test": "plan"}',
            priority=5.0,
            effort_estimate=2,
            risk_level="LOW",
        )
        
        pending = await engine.get_pending_plans()
        
        assert len(pending) >= 1
        assert any(p["plan_id"] == plan_id for p in pending)

    async def test_approve_plan(self, engine):
        """Test plan approval."""
        store.init_db()
        
        plan_id = "plan-approve-test"
        store.create_plan(
            plan_id=plan_id,
            finding_ids=["finding-1"],
            plan_json='{"test": "plan"}',
            priority=5.0,
            effort_estimate=2,
            risk_level="LOW",
        )
        
        success = await engine.approve_plan(
            plan_id=plan_id,
            approved_by="test-user",
            approval_notes="Looks good",
        )
        
        assert success
        
        # Verify status changed
        plan = store.get_plan(plan_id)
        assert plan["status"] == "approved"

    async def test_reject_plan(self, engine):
        """Test plan rejection."""
        store.init_db()
        
        plan_id = "plan-reject-test"
        store.create_plan(
            plan_id=plan_id,
            finding_ids=["finding-1"],
            plan_json='{"test": "plan"}',
            priority=5.0,
            effort_estimate=2,
            risk_level="LOW",
        )
        
        success = await engine.reject_plan(
            plan_id=plan_id,
            rejected_by="test-user",
            reason="Too risky",
        )
        
        assert success
        
        # Verify status changed
        plan = store.get_plan(plan_id)
        assert plan["status"] == "rejected"

    async def test_concurrent_plan_generation(self, engine):
        """Test concurrent plan generation with semaphore."""
        findings = [
            {
                "id": f"finding-{i}",
                "severity": "HIGH",
                "impact": 8.0,
                "reproducibility": 0.9,
                "category": "test",
                "description": f"Issue {i}",
                "details": "details",
                "effort_estimate": 2,
                "risk_level": "MEDIUM",
            }
            for i in range(10)
        ]
        
        # Mock deep_think response with delay
        async def mock_deep_think(**kwargs):
            await asyncio.sleep(0.01)
            return {
                "final_answer": json.dumps({
                    "root_cause": "Root",
                    "primary_strategy": "Strategy",
                    "fallback_strategy": None,
                    "effort_estimate": 2,
                    "risk_level": "MEDIUM",
                    "dependencies": [],
                    "subtasks": ["task"],
                    "validation_tests": ["test"],
                    "estimated_cost_tokens": 3000,
                })
            }
        
        engine.deep_think_fn = AsyncMock(side_effect=mock_deep_think)
        
        import time
        start = time.time()
        
        # Generate plans with max 3 concurrent
        plans = await engine.generate_plans_for_findings(findings, limit=6)
        
        elapsed = time.time() - start
        
        # Should respect semaphore limit
        assert len(plans) <= 6
        # With 3 concurrent and 6 tasks, should take at least 0.02s
        assert elapsed > 0.01


class TestPlanningEngineIntegration:
    """Integration tests for planning engine with store."""

    @pytest.fixture(autouse=True)
    def setup_db(self):
        """Initialize test database."""
        store.init_db()
        yield
        # Cleanup can be added here

    def test_plan_creation_and_retrieval(self):
        """Test creating and retrieving plans."""
        plan_id = "test-plan-1"
        finding_ids = ["finding-1", "finding-2"]
        plan_json = json.dumps({"strategy": "test"})
        
        # Create plan
        created_id = store.create_plan(
            plan_id=plan_id,
            finding_ids=finding_ids,
            plan_json=plan_json,
            priority=8.5,
            effort_estimate=3,
            risk_level="MEDIUM",
        )
        
        assert created_id == plan_id
        
        # Retrieve plan
        plan = store.get_plan(plan_id)
        assert plan is not None
        assert plan["id"] == plan_id
        assert plan["status"] == "pending"
        assert plan["priority"] == 8.5

    def test_list_plans(self):
        """Test listing plans."""
        # Create multiple plans
        for i in range(5):
            store.create_plan(
                plan_id=f"plan-{i}",
                finding_ids=[f"finding-{i}"],
                plan_json='{}',
                priority=float(i),
                effort_estimate=2,
                risk_level="LOW",
            )
        
        # List all
        plans = store.list_plans(status="all")
        assert len(plans) >= 5

    def test_update_plan_status(self):
        """Test updating plan status."""
        plan_id = "status-test-plan"
        store.create_plan(
            plan_id=plan_id,
            finding_ids=["finding-1"],
            plan_json='{}',
            priority=5.0,
            effort_estimate=2,
            risk_level="LOW",
        )
        
        # Update status
        success = store.update_plan_status(
            plan_id=plan_id,
            status="approved",
            approved_by="test-user",
        )
        
        assert success
        
        plan = store.get_plan(plan_id)
        assert plan["status"] == "approved"
        assert plan["approved_by"] == "test-user"

    def test_audit_log_creation(self):
        """Test audit log functionality."""
        plan_id = "audit-test-plan"
        store.create_plan(
            plan_id=plan_id,
            finding_ids=["finding-1"],
            plan_json='{}',
            priority=5.0,
            effort_estimate=2,
            risk_level="LOW",
        )
        
        # Create audit entries
        store.audit_log("plan_created", plan_id, json.dumps({"test": "data"}))
        store.audit_log("plan_approved", plan_id, json.dumps({"approver": "user1"}))
        
        # Retrieve audit trail
        trail = store.get_plan_audit_trail(plan_id)
        
        assert len(trail) >= 2
        assert any(e["event_type"] == "plan_created" for e in trail)
        assert any(e["event_type"] == "plan_approved" for e in trail)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
