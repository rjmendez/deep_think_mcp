from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine import orchestrator
from engine.types import ProviderConfig


@pytest.mark.asyncio
async def test_deep_think_passes_keeps_failed_passes_out_of_semantic_outputs(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    call_count = {"value": 0}

    async def fake_call_provider(**_kwargs):
        call_count["value"] += 1
        if call_count["value"] == 1:
            raise RuntimeError("primary lane exploded")
        return "Clean semantic output"

    monkeypatch.setattr(
        orchestrator.provider_module,
        "build_provider_config",
        lambda _cfg: ProviderConfig(provider="ollama"),
    )
    monkeypatch.setattr(orchestrator.provider_module, "_tier_provider", lambda _cfg, _tier: "ollama")
    monkeypatch.setattr(
        orchestrator.provider_module,
        "_model_for_tier",
        lambda _cfg, tier, _task_class: f"{tier}-model",
    )
    monkeypatch.setattr(orchestrator.provider_module, "_call_provider", fake_call_provider)

    result = await orchestrator.deep_think_passes(
        question="Why is the sky blue?",
        passes=2,
        task_class="general",
        provider_config={},
    )

    assert result["status"] == "partial"
    assert result["final_answer"] == "Clean semantic output"
    assert result["pass_outputs"] == ["Clean semantic output"]
    assert len(result["pass_results"]) == 2
    assert result["pass_results"][0]["status"] == "failed"
    assert result["pass_results"][0]["error"] == "primary lane exploded"
    assert result["pass_results"][0]["output"] == ""
    assert result["pass_results"][1]["status"] == "complete"
    assert result["pass_results"][1]["output"] == "Clean semantic output"


@pytest.mark.asyncio
async def test_deep_think_passes_logs_structured_exception_context(monkeypatch, caplog):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    async def fake_call_provider(**_kwargs):
        raise RuntimeError("structured lane exploded")

    monkeypatch.setattr(
        orchestrator.provider_module,
        "build_provider_config",
        lambda _cfg: ProviderConfig(provider="ollama"),
    )
    monkeypatch.setattr(orchestrator.provider_module, "_tier_provider", lambda _cfg, _tier: "ollama")
    monkeypatch.setattr(
        orchestrator.provider_module,
        "_model_for_tier",
        lambda _cfg, tier, _task_class: f"{tier}-model",
    )
    monkeypatch.setattr(orchestrator.provider_module, "_call_provider", fake_call_provider)

    with caplog.at_level("ERROR"):
        await orchestrator.deep_think_passes(
            question="Why is the sky blue?",
            passes=1,
            task_class="general",
            provider_config={"temperature": 1.1, "top_p": 0.7},
            job_id="job-123",
        )

    assert "pass_event" in caplog.text
    assert '"job_id": "job-123"' in caplog.text
    assert '"provider": "ollama"' in caplog.text
    assert '"model": "medium-model"' in caplog.text
    assert '"temperature": 1.1' in caplog.text
    assert '"exception_type": "RuntimeError"' in caplog.text


@pytest.mark.asyncio
async def test_deep_think_passes_ignores_whitespace_only_success_outputs(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    outputs = iter([
        "Substantive answer with real content",
        "\n",
        "   \n  ",
    ])

    async def fake_call_provider(**_kwargs):
        return next(outputs)

    monkeypatch.setattr(
        orchestrator.provider_module,
        "build_provider_config",
        lambda _cfg: ProviderConfig(provider="ollama"),
    )
    monkeypatch.setattr(orchestrator.provider_module, "_tier_provider", lambda _cfg, _tier: "ollama")
    monkeypatch.setattr(
        orchestrator.provider_module,
        "_model_for_tier",
        lambda _cfg, tier, _task_class: f"{tier}-model",
    )
    monkeypatch.setattr(orchestrator.provider_module, "_call_provider", fake_call_provider)

    result = await orchestrator.deep_think_passes(
        question="How should final answer selection work?",
        passes=3,
        task_class="general",
        provider_config={},
    )

    assert result["status"] == "partial"
    assert result["final_answer"] == "Substantive answer with real content"
    assert result["pass_outputs"] == ["Substantive answer with real content"]
    assert len(result["pass_results"]) == 3
    assert result["pass_results"][0]["output"] == "Substantive answer with real content"
    assert result["pass_results"][1]["output"] == "\n"
    assert result["pass_results"][2]["output"] == "   \n  "


@pytest.mark.asyncio
async def test_deep_think_passes_handles_none_data_policy_in_run_signature(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    async def fake_call_provider(**_kwargs):
        return "content"

    monkeypatch.setattr(
        orchestrator.provider_module,
        "build_provider_config",
        lambda _cfg: ProviderConfig(provider="ollama", data_policy=None),
    )
    monkeypatch.setattr(orchestrator.provider_module, "_tier_provider", lambda _cfg, _tier: "ollama")
    monkeypatch.setattr(
        orchestrator.provider_module,
        "_model_for_tier",
        lambda _cfg, tier, _task_class: f"{tier}-model",
    )
    monkeypatch.setattr(orchestrator.provider_module, "_call_provider", fake_call_provider)

    result = await orchestrator.deep_think_passes(
        question="data policy none",
        passes=1,
        task_class="code_review",
        provider_config={"data_policy": None},
    )

    assert result["status"] == "complete"
    assert result["final_answer"] == "content"


@pytest.mark.asyncio
async def test_fan_out_logs_structured_perspective_exception(monkeypatch, caplog):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    async def fake_call_provider(**_kwargs):
        raise RuntimeError("perspective lane exploded")

    monkeypatch.setattr(
        orchestrator.provider_module,
        "build_provider_config",
        lambda _cfg: ProviderConfig(provider="ollama"),
    )
    monkeypatch.setattr(orchestrator.provider_module, "_tier_provider", lambda _cfg, _tier: "ollama")
    monkeypatch.setattr(
        orchestrator.provider_module,
        "_model_for_tier",
        lambda _cfg, tier, _task_class: f"{tier}-model",
    )
    monkeypatch.setattr(orchestrator.provider_module, "_call_provider", fake_call_provider)

    with caplog.at_level("ERROR"):
        await orchestrator.run_fan_out(
            question="Test fan out failures",
            width=1,
            height=1,
            task_class="reasoning",
            provider_config={"temperature": 0.9},
        )

    assert "pass_event" in caplog.text
    assert '"perspective":' in caplog.text
    assert '"provider": "ollama"' in caplog.text
    assert '"temperature": 0.9' in caplog.text


@pytest.mark.asyncio
async def test_run_fan_out_propagates_perspective_failure_status(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    async def fake_deep_think_passes(**kwargs):
        if kwargs.get("task_class") == "synthesis":
            raise AssertionError("synthesis should not run when no perspectives succeed")
        return {
            "status": "failed",
            "error": "perspective failed upstream",
            "final_answer": "",
            "pass_results": [{"status": "failed", "output": "", "error": "perspective failed upstream"}],
        }

    monkeypatch.setattr(orchestrator, "deep_think_passes", fake_deep_think_passes)
    monkeypatch.setattr(orchestrator.store, "get_perspective_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator.store, "set_perspective_cache", lambda *_args, **_kwargs: None)

    result = await orchestrator.run_fan_out(
        question="Test perspective status propagation",
        width=1,
        height=1,
        task_class="reasoning",
        provider_cfg=ProviderConfig(provider="ollama"),
    )

    assert result["status"] == "failed"
    assert result["perspectives"][0]["status"] == "failed"
    assert result["perspectives"][0]["error"] == "perspective failed upstream"


@pytest.mark.asyncio
async def test_run_fan_out_marks_failed_when_synthesis_fails(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    async def fake_deep_think_passes(**kwargs):
        if kwargs.get("task_class") == "synthesis":
            return {"status": "failed", "error": "synthesis failed", "final_answer": "", "pass_results": []}
        return {
            "status": "complete",
            "final_answer": "Perspective answer",
            "pass_results": [{"status": "complete", "output": "Perspective answer"}],
        }

    async def fake_alarm_scan(*_args, **_kwargs):
        return []

    monkeypatch.setattr(orchestrator, "deep_think_passes", fake_deep_think_passes)
    monkeypatch.setattr(orchestrator, "_fan_out_alarm_scan", fake_alarm_scan)
    monkeypatch.setattr(orchestrator.store, "get_perspective_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator.store, "set_perspective_cache", lambda *_args, **_kwargs: None)

    result = await orchestrator.run_fan_out(
        question="Test synthesis status propagation",
        width=1,
        height=1,
        task_class="reasoning",
        provider_cfg=ProviderConfig(provider="ollama"),
    )

    assert result["status"] == "failed"
    assert result["synthesis_status"] == "failed"
    assert result["synthesis_error"] == "synthesis failed"


@pytest.mark.asyncio
async def test_fan_out_early_failure_preserves_outputs_and_metrics(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    async def fake_deep_think_passes(**kwargs):
        mandate_prefix = kwargs.get("mandate_prefix", "")
        if "PRIMARY" in mandate_prefix:
            return {
                "status": "complete",
                "final_answer": "primary perspective output",
                "pass_results": [{"status": "complete", "output": "primary perspective output"}],
            }
        raise RuntimeError(f"boom: {mandate_prefix}")

    monkeypatch.setattr(orchestrator, "deep_think_passes", fake_deep_think_passes)
    monkeypatch.setattr(
        orchestrator.provider_module,
        "build_provider_config",
        lambda _cfg: ProviderConfig(provider="ollama"),
    )

    result = await orchestrator.run_fan_out(
        question="trigger early fail",
        width=4,
        height=1,
        max_parallel=4,
        task_class="general",
        provider_config={},
    )

    assert result["status"] == "failed"
    assert result["tools_invoked_total"] == 0
    assert result["tool_successes_total"] == 0
    assert result["grounding_warnings"] == []
    assert result["claim_sets"] == []
    assert set(result["perspective_outputs"].keys()) == {p["name"] for p in result["perspectives"]}
    assert result["perspective_outputs"]["primary"]["synthesis"] == "primary perspective output"
    assert result["perspective_outputs"]["primary"]["status"] == "complete"
