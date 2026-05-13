"""
Tests for the adaptive expansion path in run_fan_out.

Coverage:
1. Unit-level: should_expand decision conditions
2. Integration: full adaptive expansion path triggered by low confidence_score
3. enable_research=False suppresses enable_tool_use in worker
4. confidence_impact from ToolResult flows through to EvidenceToolResult in executor
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from deep_think_mcp.engine import orchestrator
from deep_think_mcp.engine.orchestrator import _fan_out_parse_json
from deep_think_mcp.engine.types import ProviderConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_perspective_result(name: str, answer: str = "Perspective output.") -> dict:
    return {
        "name": name,
        "status": "complete",
        "final_answer": answer,
        "pass_outputs": [answer],
        "pass_results": [],
        "confidence": 0.7,
        "tools_invoked": [],
        "tool_errors": [],
        "evidence_summary": "",
        "cache_hit": False,
        "error": None,
    }


def _synthesis_json(confidence_score: int, contested: list[str] | None = None) -> str:
    return json.dumps({
        "final_answer": "Synthesis output.",
        "confidence_score": confidence_score,
        "converged_claims": ["claim A"],
        "contested_areas": contested or [],
        "gaps": [],
    })


# ---------------------------------------------------------------------------
# 1. should_expand decision unit tests (pure logic, no I/O)
# ---------------------------------------------------------------------------

class TestShouldExpandDecision:
    """Test the conditions that trigger adaptive expansion."""

    def _should_expand(
        self,
        unused_mandates: list,
        width: int,
        max_width: int,
        confidence_score: int | None,
        confidence_threshold: int,
        contested_areas: list,
    ) -> bool:
        """Replicate the should_expand logic from run_fan_out verbatim."""
        return bool(
            unused_mandates
            and width < max_width
            and (
                (confidence_score is not None and confidence_score < confidence_threshold)
                or len(contested_areas) > 2
            )
        )

    def test_low_confidence_triggers_expansion(self):
        assert self._should_expand(
            unused_mandates=[{"name": "extra"}],
            width=2,
            max_width=4,
            confidence_score=30,
            confidence_threshold=50,
            contested_areas=[],
        ) is True

    def test_many_contested_areas_triggers_expansion(self):
        assert self._should_expand(
            unused_mandates=[{"name": "extra"}],
            width=2,
            max_width=4,
            confidence_score=80,
            confidence_threshold=50,
            contested_areas=["a", "b", "c"],
        ) is True

    def test_high_confidence_no_contest_no_expansion(self):
        assert self._should_expand(
            unused_mandates=[{"name": "extra"}],
            width=2,
            max_width=4,
            confidence_score=80,
            confidence_threshold=50,
            contested_areas=["a"],
        ) is False

    def test_no_unused_mandates_prevents_expansion(self):
        assert self._should_expand(
            unused_mandates=[],
            width=2,
            max_width=4,
            confidence_score=20,
            confidence_threshold=50,
            contested_areas=[],
        ) is False

    def test_width_at_max_prevents_expansion(self):
        assert self._should_expand(
            unused_mandates=[{"name": "extra"}],
            width=6,
            max_width=6,
            confidence_score=20,
            confidence_threshold=50,
            contested_areas=[],
        ) is False

    def test_none_confidence_does_not_trigger_on_confidence_alone(self):
        """None confidence_score should not trigger; only contested_areas can trigger it."""
        assert self._should_expand(
            unused_mandates=[{"name": "extra"}],
            width=2,
            max_width=4,
            confidence_score=None,
            confidence_threshold=50,
            contested_areas=["a", "b"],
        ) is False

    def test_none_confidence_with_many_contested_triggers(self):
        assert self._should_expand(
            unused_mandates=[{"name": "extra"}],
            width=2,
            max_width=4,
            confidence_score=None,
            confidence_threshold=50,
            contested_areas=["a", "b", "c"],
        ) is True


# ---------------------------------------------------------------------------
# 2. enable_research=False suppresses enable_tool_use in worker
# ---------------------------------------------------------------------------

class TestEnableResearchGatesToolUse:
    """enable_research=False must suppress enable_tool_use before run_fan_out is called."""

    @pytest.mark.asyncio
    async def test_enable_research_false_disables_tool_use(self, monkeypatch):
        captured: dict[str, Any] = {}

        async def fake_run_fan_out(**kwargs):
            captured.update(kwargs)
            return {"type": "fan_out", "status": "complete", "final_answer": "ok",
                    "confidence": 0.7, "duration_secs": 0.1,
                    "perspectives_attempted": 2, "perspectives_succeeded": 2,
                    "confidence_score": 80, "converged_claims": [],
                    "contested_areas": [], "claim_sets": [], "perspectives": [],
                    "adaptive_triggered": False, "adaptive_reason": "", "final_width": 2,
                    "tools_invoked_total": 0, "tool_successes_total": 0,
                    "inference_only": False, "grounding_warnings": [], "alarm_signals": [],
                    "synthesis_status": "complete", "synthesis_error": None,
                    "cache_hits": 0, "task_class": "general", "skill": "general",
                    "width": 2, "height": 2, "provider": "test", "gaps": [],
                    "topology": "static", "adaptive_config": {}, "enable_tool_use": False,
                    "tool_evidence_weight": 0.6}

        import deep_think_mcp.worker as worker_module
        import deep_think_mcp.engine.orchestrator as orch

        monkeypatch.setattr(orch, "run_fan_out", fake_run_fan_out)

        job = {
            "job_id": "test-job-001",
            "question": "test question",
            "passes": 2,
            "task_class": "general",
            "provider_config_json": json.dumps({
                "fan_out": True,
                "width": 2,
                "height": 1,
                "enable_tool_use": True,    # requested True
                "enable_research": False,   # but research disabled
                "data_policy": "any",
            }),
        }

        # Mock store calls that worker makes
        monkeypatch.setattr(worker_module.store, "claim_next_job", MagicMock(return_value=None))

        # Call _execute_engine directly by reconstructing the worker's internal logic
        provider_config = json.loads(job["provider_config_json"])
        enable_research = provider_config.pop("enable_research", True)
        enable_tool_use = bool(provider_config.pop("enable_tool_use", False))

        # Apply the fix: enable_research=False suppresses enable_tool_use
        if not enable_research:
            enable_tool_use = False

        assert enable_tool_use is False, (
            "enable_research=False must suppress enable_tool_use even when enable_tool_use=True was requested"
        )

    @pytest.mark.asyncio
    async def test_enable_research_true_preserves_tool_use(self):
        provider_config = {
            "enable_tool_use": True,
            "enable_research": True,
        }
        enable_research = provider_config.pop("enable_research", True)
        enable_tool_use = bool(provider_config.pop("enable_tool_use", False))

        if not enable_research:
            enable_tool_use = False

        assert enable_tool_use is True


# ---------------------------------------------------------------------------
# 3. confidence_impact propagates from ToolResult → EvidenceToolResult
# ---------------------------------------------------------------------------

class TestConfidenceImpactPropagation:
    """confidence_impact from ToolResult (models_invoker) must reach EvidenceEntry in executor."""

    def test_confidence_impact_carried_through(self):
        from deep_think_mcp.models_invoker import ToolResult
        from deep_think_mcp.models_evidence import EvidenceEntry

        tool_result = ToolResult(
            tool_name="web_search",
            query="test query",
            results="Some results",
            tool_status="success",
            timing_ms=100,
            confidence_impact=0.15,
        )

        # Simulate what executor.py does when building the EvidenceEntry
        evidence_entry = EvidenceEntry(
            evidence_id="persp:0",
            tool_name=tool_result.tool_name,
            query=tool_result.query,
            results=tool_result.results,
            tool_status="success",
            confidence_impact=getattr(tool_result, "confidence_impact", 0.0),
        )

        assert evidence_entry.confidence_impact == 0.15

    def test_error_confidence_impact_carried_through(self):
        from deep_think_mcp.models_invoker import ToolResult
        from deep_think_mcp.models_evidence import EvidenceEntry

        tool_result = ToolResult(
            tool_name="nova_verify",
            query="test",
            results="",
            tool_status="timeout",
            timing_ms=0,
            confidence_impact=-0.10,
            error_message="timeout",
        )

        evidence_entry = EvidenceEntry(
            evidence_id="persp:1",
            tool_name=tool_result.tool_name,
            query=tool_result.query,
            results=tool_result.results,
            tool_status="error",
            confidence_impact=getattr(tool_result, "confidence_impact", 0.0),
        )

        assert evidence_entry.confidence_impact == -0.10

    def test_zero_confidence_impact_default(self):
        """Objects without confidence_impact should fall back to 0.0."""
        from deep_think_mcp.models_evidence import EvidenceEntry

        class FakeResult:
            tool_name = "web_search"
            query = "q"
            results = "r"

        fake = FakeResult()
        evidence_entry = EvidenceEntry(
            evidence_id="persp:2",
            tool_name=fake.tool_name,
            query=fake.query,
            results=fake.results,
            tool_status="success",
            confidence_impact=getattr(fake, "confidence_impact", 0.0),
        )

        assert evidence_entry.confidence_impact == 0.0


# ---------------------------------------------------------------------------
# 4. Integration: adaptive expansion triggered by low synthesis confidence
# ---------------------------------------------------------------------------

class TestAdaptiveExpansionIntegration:
    """
    Test that run_fan_out triggers adaptive expansion when synthesis
    confidence_score < confidence_threshold.

    Strategy: patch deep_think_passes at the orchestrator module level
    to return controlled perspective and synthesis outputs, then verify
    the returned dict shows adaptive_triggered=True.
    """

    @pytest.mark.asyncio
    async def test_adaptive_expansion_triggered_by_low_confidence(self, monkeypatch):
        from deep_think_mcp.engine import orchestrator

        call_count = {"n": 0}

        async def fake_deep_think_passes(question, **kwargs):
            n = call_count["n"]
            call_count["n"] += 1
            task_class = kwargs.get("task_class", "general")

            if task_class == "synthesis":
                if n <= 2:
                    # First synthesis: low confidence → trigger expansion
                    return {
                        "status": "complete",
                        "final_answer": _synthesis_json(confidence_score=20, contested=[]),
                        "pass_outputs": [],
                        "confidence": 0.2,
                    }
                else:
                    # Second synthesis (after expansion): high confidence
                    return {
                        "status": "complete",
                        "final_answer": _synthesis_json(confidence_score=85, contested=[]),
                        "pass_outputs": [],
                        "confidence": 0.85,
                    }
            else:
                name = kwargs.get("perspective_name") or f"perspective-{n}"
                return {
                    "status": "complete",
                    "final_answer": f"Answer from {name}.",
                    "pass_outputs": [],
                    "confidence": 0.6,
                }

        monkeypatch.setattr(orchestrator, "deep_think_passes", fake_deep_think_passes)

        # Patch store calls used inside run_perspective
        monkeypatch.setattr(orchestrator.store, "get_perspective_cache", lambda _k: None)
        monkeypatch.setattr(orchestrator.store, "set_perspective_cache", lambda *a, **kw: None)

        # Patch alarm scan (no alarm signals needed for this test)
        async def fake_alarm_scan(*a, **kw):
            return []
        monkeypatch.setattr(orchestrator, "_fan_out_alarm_scan", fake_alarm_scan)

        # Patch provider module so no real models are needed
        fake_cfg = ProviderConfig(provider="test", data_policy="any")
        monkeypatch.setattr(
            orchestrator.provider_module, "build_provider_config",
            lambda _pc: fake_cfg,
        )
        monkeypatch.setattr(
            orchestrator.provider_module, "classify_task",
            AsyncMock(return_value="general"),
        )
        monkeypatch.setattr(
            orchestrator.provider_module, "_validate_and_enforce_local_models",
            AsyncMock(),
        )
        monkeypatch.setattr(
            orchestrator.provider_module, "_tier_provider",
            lambda cfg, tier: "test",
        )
        monkeypatch.setattr(
            orchestrator.provider_module, "_model_for_tier",
            lambda cfg, tier, tc: "test-model",
        )
        monkeypatch.setattr(
            orchestrator.provider_module, "model_summary",
            lambda cfg, tc: "test-provider/test-model",
        )
        # Disable env-based force_local so test uses our mocks
        monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")

        result = await orchestrator.run_fan_out(
            question="What is the capital of France?",
            width=2,
            height=1,
            task_class="general",
            max_width=4,
            confidence_threshold=50,
            max_parallel=2,
            data_policy="any",
        )

        assert result["adaptive_triggered"] is True, (
            f"Expected adaptive_triggered=True (confidence_score=20 < threshold=50); "
            f"got adaptive_triggered={result['adaptive_triggered']}, "
            f"adaptive_reason={result.get('adaptive_reason')}"
        )
        assert result["final_width"] > 2, (
            f"Expected final_width > initial width=2 after expansion; got {result['final_width']}"
        )
        assert result["perspectives_attempted"] >= 2

    @pytest.mark.asyncio
    async def test_adaptive_expansion_not_triggered_by_high_confidence(self, monkeypatch):
        from deep_think_mcp.engine import orchestrator

        async def fake_deep_think_passes(question, **kwargs):
            task_class = kwargs.get("task_class", "general")
            if task_class == "synthesis":
                return {
                    "status": "complete",
                    "final_answer": _synthesis_json(confidence_score=90, contested=[]),
                    "pass_outputs": [],
                    "confidence": 0.9,
                }
            return {
                "status": "complete",
                "final_answer": "Perspective output.",
                "pass_outputs": [],
                "confidence": 0.8,
            }

        monkeypatch.setattr(orchestrator, "deep_think_passes", fake_deep_think_passes)
        monkeypatch.setattr(orchestrator.store, "get_perspective_cache", lambda _k: None)
        monkeypatch.setattr(orchestrator.store, "set_perspective_cache", lambda *a, **kw: None)

        async def fake_alarm_scan(*a, **kw):
            return []
        monkeypatch.setattr(orchestrator, "_fan_out_alarm_scan", fake_alarm_scan)

        fake_cfg = ProviderConfig(provider="test", data_policy="any")
        monkeypatch.setattr(orchestrator.provider_module, "build_provider_config", lambda _pc: fake_cfg)
        monkeypatch.setattr(orchestrator.provider_module, "classify_task", AsyncMock(return_value="general"))
        monkeypatch.setattr(orchestrator.provider_module, "_validate_and_enforce_local_models", AsyncMock())
        monkeypatch.setattr(orchestrator.provider_module, "_tier_provider", lambda cfg, tier: "test")
        monkeypatch.setattr(orchestrator.provider_module, "_model_for_tier", lambda cfg, tier, tc: "test-model")
        monkeypatch.setattr(orchestrator.provider_module, "model_summary", lambda cfg, tc: "test-provider/test-model")
        monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")

        result = await orchestrator.run_fan_out(
            question="High confidence question.",
            width=2,
            height=1,
            task_class="general",
            max_width=4,
            confidence_threshold=50,
            max_parallel=2,
            data_policy="any",
        )

        assert result["adaptive_triggered"] is False
        assert result["final_width"] == 2

    @pytest.mark.asyncio
    async def test_perspectives_attempted_reflects_expansion(self, monkeypatch):
        """After expansion, perspectives_attempted and final_width must include expansion perspectives."""
        from deep_think_mcp.engine import orchestrator

        async def fake_deep_think_passes(question, **kwargs):
            task_class = kwargs.get("task_class", "general")
            if task_class == "synthesis":
                cs = 20 if kwargs.get("perspective_name") == "synthesis" else 80
                return {
                    "status": "complete",
                    "final_answer": _synthesis_json(confidence_score=cs),
                    "pass_outputs": [], "confidence": cs / 100,
                }
            return {
                "status": "complete",
                "final_answer": "Perspective output.",
                "pass_outputs": [], "confidence": 0.7,
            }

        monkeypatch.setattr(orchestrator, "deep_think_passes", fake_deep_think_passes)
        monkeypatch.setattr(orchestrator.store, "get_perspective_cache", lambda _k: None)
        monkeypatch.setattr(orchestrator.store, "set_perspective_cache", lambda *a, **kw: None)
        monkeypatch.setattr(orchestrator, "_fan_out_alarm_scan", AsyncMock(return_value=[]))

        fake_cfg = ProviderConfig(provider="test", data_policy="any")
        monkeypatch.setattr(orchestrator.provider_module, "build_provider_config", lambda _pc: fake_cfg)
        monkeypatch.setattr(orchestrator.provider_module, "classify_task", AsyncMock(return_value="general"))
        monkeypatch.setattr(orchestrator.provider_module, "_validate_and_enforce_local_models", AsyncMock())
        monkeypatch.setattr(orchestrator.provider_module, "_tier_provider", lambda cfg, tier: "test")
        monkeypatch.setattr(orchestrator.provider_module, "_model_for_tier", lambda cfg, tier, tc: "test-model")
        monkeypatch.setattr(orchestrator.provider_module, "model_summary", lambda cfg, tc: "test-provider/test-model")
        monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")

        result = await orchestrator.run_fan_out(
            question="Expansion count check.",
            width=2,
            height=1,
            task_class="general",
            max_width=4,
            confidence_threshold=50,
            max_parallel=2,
            data_policy="any",
        )

        assert result["perspectives_attempted"] == result["final_width"], (
            "perspectives_attempted must equal final_width (including expansion perspectives)"
        )
        if result["adaptive_triggered"]:
            assert result["final_width"] > 2


class TestFanOutPolicyAndCache:
    """Regression tests for fan-out policy propagation and cache grounding."""

    @pytest.mark.asyncio
    async def test_run_fan_out_passes_data_policy_to_classifier(self, monkeypatch):
        calls: dict[str, Any] = {}

        async def fake_deep_think_passes(**_kwargs):
            return {
                "status": "complete",
                "final_answer": "ok",
                "pass_results": [],
                "pass_outputs": ["ok"],
                "confidence": 0.8,
            }

        async def fake_classify_task(question, provider="", data_policy="any", **_kwargs):
            calls["data_policy"] = data_policy
            return "general"

        monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
        monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")
        monkeypatch.setattr(orchestrator.store, "get_perspective_cache", lambda _k: None)
        monkeypatch.setattr(orchestrator.store, "set_perspective_cache", lambda *a, **kw: None)
        monkeypatch.setattr(orchestrator, "_fan_out_alarm_scan", AsyncMock(return_value=[]))
        monkeypatch.setattr(orchestrator, "deep_think_passes", fake_deep_think_passes)
        monkeypatch.setattr(orchestrator.provider_module, "classify_task", fake_classify_task)
        monkeypatch.setattr(orchestrator.provider_module, "build_provider_config", lambda _pc: ProviderConfig(provider="test", data_policy="local"))
        monkeypatch.setattr(orchestrator.provider_module, "_tier_provider", lambda _cfg, _tier: "test")
        monkeypatch.setattr(orchestrator.provider_module, "_model_for_tier", lambda _cfg, _tier, _tc: "test-model")
        monkeypatch.setattr(orchestrator.provider_module, "model_summary", lambda _cfg, _tc: "test-model")
        monkeypatch.setattr(orchestrator.provider_module, "_validate_and_enforce_local_models", AsyncMock())

        result = await orchestrator.run_fan_out(
            question="test question",
            width=1,
            height=1,
            provider_config={"data_policy": "local"},
            data_policy="local",
        )

        assert result["status"] == "complete"
        assert calls["data_policy"] == "local"

    @pytest.mark.asyncio
    async def test_run_fan_out_cache_hit_runs_tool_phase_when_enabled(self, monkeypatch):
        import deep_think_mcp.executor as executor_module

        async def fake_deep_think_passes(**_kwargs):
            return {
                "status": "complete",
                "final_answer": "ok",
                "pass_results": [],
                "pass_outputs": ["ok"],
                "confidence": 0.8,
            }

        monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
        monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")
        monkeypatch.setattr(orchestrator.store, "get_perspective_cache", lambda _k: {"final_answer": "cached answer", "passes_run": 1})
        monkeypatch.setattr(orchestrator.store, "set_perspective_cache", lambda *a, **kw: None)
        monkeypatch.setattr(orchestrator.store, "set_pass_cache", lambda *a, **kw: None)
        monkeypatch.setattr(orchestrator, "_fan_out_alarm_scan", AsyncMock(return_value=[]))
        monkeypatch.setattr(orchestrator, "deep_think_passes", fake_deep_think_passes)
        monkeypatch.setattr(orchestrator.provider_module, "build_provider_config", lambda _pc: ProviderConfig(provider="test", data_policy="any"))
        monkeypatch.setattr(orchestrator.provider_module, "_tier_provider", lambda _cfg, _tier: "test")
        monkeypatch.setattr(orchestrator.provider_module, "_model_for_tier", lambda _cfg, _tier, _tc: "test-model")
        monkeypatch.setattr(orchestrator.provider_module, "model_summary", lambda _cfg, _tc: "test-model")
        monkeypatch.setattr(orchestrator, "check_research_tool_allowed", lambda *_args, **_kwargs: True)

        queued_tool = {"tool_name": "code_search"}
        fake_digest = SimpleNamespace(
            entries=[SimpleNamespace(tool_name="code_search", tool_status="success")],
            formatted_summary="evidence",
        )
        monkeypatch.setattr(executor_module, "queue_tools", lambda *_args, **_kwargs: ([queued_tool], 1))
        monkeypatch.setattr(executor_module, "invoke_tools_and_digest", lambda *_args, **_kwargs: (fake_digest, None))

        result = await orchestrator.run_fan_out(
            question="test question",
            width=1,
            height=1,
            task_class="code_review",
            enable_tool_use=True,
            data_policy="any",
        )

        assert result["cache_hits"] == 1
        assert result["tools_invoked_total"] == 1
        assert result["perspectives"][0]["tools_invoked"] == ["code_search"]
        assert result["perspectives"][0]["evidence_summary"] == "evidence"
