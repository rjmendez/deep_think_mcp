from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

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
async def test_deep_think_passes_resumes_from_cached_pass_prefix(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    provider_calls = {"value": 0}
    cache_writes = []
    captured_run_sig = {"value": ""}

    async def fake_call_provider(**_kwargs):
        provider_calls["value"] += 1
        return f"fresh-pass-{provider_calls['value']}"

    def fake_get_pass_history(job_id, perspective, run_sig):
        captured_run_sig["value"] = run_sig
        assert job_id == "job-resume-1"
        assert perspective == "alpha"
        return [
            {
                "pass_num": 1,
                "framing": "opening",
                "tier": "medium",
                "model_used": "cached-model",
                "provider": "cached-provider",
                "output": "cached-pass-1",
            }
        ]

    def fake_set_pass_cache(*args, **_kwargs):
        cache_writes.append(args)

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
    monkeypatch.setattr(orchestrator.provider_module, "model_summary", lambda _cfg, _task_class: "sig-model-summary")
    monkeypatch.setattr(orchestrator.provider_module, "_call_provider", fake_call_provider)
    monkeypatch.setattr(orchestrator.store, "get_pass_history", fake_get_pass_history)
    monkeypatch.setattr(orchestrator.store, "set_pass_cache", fake_set_pass_cache)

    result = await orchestrator.deep_think_passes(
        question="resume this job",
        passes=3,
        task_class="general",
        provider_config={},
        job_id="job-resume-1",
        perspective_name="alpha",
    )

    assert provider_calls["value"] == 2
    assert len(captured_run_sig["value"]) == 64
    assert [pr["pass_num"] for pr in result["pass_results"]] == [1, 2, 3]
    assert result["pass_results"][0]["output"] == "cached-pass-1"
    assert result["pass_results"][1]["output"] == "fresh-pass-1"
    assert result["pass_results"][2]["output"] == "fresh-pass-2"
    assert len(cache_writes) == 2
    assert [entry[2] for entry in cache_writes] == [2, 3]
    assert all(entry[3] == captured_run_sig["value"] for entry in cache_writes)


@pytest.mark.asyncio
async def test_deep_think_passes_run_sig_changes_with_model_override(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    run_sigs = []

    async def fake_call_provider(**_kwargs):
        return "ok"

    def fake_get_pass_history(_job_id, _perspective, run_sig):
        run_sigs.append(run_sig)
        return []

    monkeypatch.setattr(
        orchestrator.provider_module,
        "build_provider_config",
        lambda _cfg: ProviderConfig(provider="ollama"),
    )
    monkeypatch.setattr(orchestrator.provider_module, "_tier_provider", lambda _cfg, tier: f"ollama-{tier}")
    monkeypatch.setattr(
        orchestrator.provider_module,
        "_model_for_tier",
        lambda _cfg, tier, _task_class: f"default-{tier}",
    )
    monkeypatch.setattr(orchestrator.provider_module, "model_summary", lambda _cfg, _task_class: "summary")
    monkeypatch.setattr(orchestrator.provider_module, "_call_provider", fake_call_provider)
    monkeypatch.setattr(orchestrator.store, "get_pass_history", fake_get_pass_history)

    await orchestrator.deep_think_passes(
        question="same question",
        passes=1,
        task_class="general",
        provider_config={},
        model="model-a",
        job_id="job-a",
    )
    await orchestrator.deep_think_passes(
        question="same question",
        passes=1,
        task_class="general",
        provider_config={},
        model="model-b",
        job_id="job-b",
    )

    assert len(run_sigs) == 2
    assert run_sigs[0] != run_sigs[1]


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
    assert '"exception_type": "RuntimeError"' in caplog.text
    # temperature is not preserved through ProviderConfig (no field); assert structured fields instead
    assert '"error": "perspective lane exploded"' in caplog.text


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
    assert "FAN_OUT_FAILURE" in result["error"]
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
    assert any("FAN_OUT_FAILURE" in w for w in result["grounding_warnings"])
    assert result["claim_sets"] == []
    assert set(result["perspective_outputs"].keys()) == {p["name"] for p in result["perspectives"]}
    assert result["perspective_outputs"]["primary"]["synthesis"] == "primary perspective output"
    assert result["perspective_outputs"]["primary"]["status"] == "complete"


@pytest.mark.asyncio
async def test_deep_think_passes_blocks_pass_override_provider_conflict_with_local_policy(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    monkeypatch.setattr(
        orchestrator.provider_module,
        "build_provider_config",
        lambda _cfg: ProviderConfig(provider="ollama", data_policy="local"),
    )
    monkeypatch.setattr(orchestrator.provider_module, "_tier_provider", lambda _cfg, _tier: "ollama")
    monkeypatch.setattr(
        orchestrator.provider_module,
        "_model_for_tier",
        lambda _cfg, tier, _task_class: f"{tier}-model",
    )
    result = await orchestrator.deep_think_passes(
        question="enforce local policy",
        passes=1,
        task_class="general",
        data_policy="local",
        provider_config={},
        pass_overrides=[{"provider": "anthropic"}],
    )

    assert result["status"] == "failed"
    assert "data_policy=local blocks provider 'anthropic'" in result["error"]
    assert result["pass_results"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_deep_think_passes_threads_local_policy_into_safety_precheck(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    captured = {}

    async def fake_safety_precheck(question, provider="", data_policy="any"):
        captured["question"] = question
        captured["provider"] = provider
        captured["data_policy"] = data_policy
        return True, "ok"

    async def fake_call_provider(**_kwargs):
        return "Safe output"

    monkeypatch.setattr(
        orchestrator.provider_module,
        "build_provider_config",
        lambda _cfg: ProviderConfig(provider="anthropic", data_policy="local"),
    )
    monkeypatch.setattr(orchestrator.provider_module, "_tier_provider", lambda _cfg, _tier: "ollama")
    monkeypatch.setattr(
        orchestrator.provider_module,
        "_model_for_tier",
        lambda _cfg, tier, _task_class: f"{tier}-model",
    )
    monkeypatch.setattr(orchestrator.provider_module, "_run_safety_precheck", fake_safety_precheck)
    monkeypatch.setattr(orchestrator.provider_module, "_call_provider", fake_call_provider)

    result = await orchestrator.deep_think_passes(
        question="run safety route",
        passes=1,
        task_class="safety",
        data_policy="local",
        provider_config={},
    )

    assert result["status"] == "complete"
    assert captured["provider"] == "anthropic"
    assert captured["data_policy"] == "local"


@pytest.mark.asyncio
async def test_run_fan_out_tool_mode_threads_local_data_policy_into_tool_execution(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    captured_policies = []

    async def fake_deep_think_passes(**kwargs):
        if kwargs.get("task_class") == "synthesis":
            return {
                "status": "complete",
                "final_answer": "Synthesis output",
                "pass_results": [{"status": "complete", "output": "Synthesis output"}],
            }
        return {
            "status": "complete",
            "final_answer": "Perspective output",
            "pass_results": [{"status": "complete", "output": "Perspective output"}],
            "confidence": 0.65,
        }

    def fake_queue_tools(*_args, **_kwargs):
        return ([{"tool_name": "code_search", "query": "policy", "reason": "ground", "priority": 1}], 1)

    def fake_invoke_tools_and_digest(*_args, **kwargs):
        captured_policies.append(kwargs.get("data_policy"))
        digest = SimpleNamespace(
            entries=[SimpleNamespace(tool_name="code_search", tool_status="success")],
            formatted_summary="tool evidence",
        )
        return digest, 1

    monkeypatch.setattr(orchestrator, "deep_think_passes", fake_deep_think_passes)
    monkeypatch.setattr("deep_think_mcp.executor.queue_tools", fake_queue_tools)
    monkeypatch.setattr("deep_think_mcp.executor.invoke_tools_and_digest", fake_invoke_tools_and_digest)
    monkeypatch.setattr(orchestrator.store, "get_perspective_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator.store, "set_perspective_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator.provider_module,
        "build_provider_config",
        lambda _cfg: ProviderConfig(provider="ollama", data_policy="local"),
    )

    result = await orchestrator.run_fan_out(
        question="thread local policy into tool phase",
        width=1,
        height=1,
        task_class="code_review",
        provider_config={},
        data_policy="local",
        enable_tool_use=True,
        topology="adaptive",
    )

    assert result["status"] == "complete"
    assert captured_policies
    assert set(captured_policies) == {"local"}


@pytest.mark.asyncio
async def test_run_fan_out_tool_mode_local_policy_blocks_external_web_tool(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    captured_policies = []

    async def fake_deep_think_passes(**kwargs):
        if kwargs.get("task_class") == "synthesis":
            return {
                "status": "complete",
                "final_answer": json.dumps(
                    {
                        "confidence_score": 70,
                        "converged_claims": ["ok"],
                        "contested_areas": [],
                        "gaps": [],
                        "final_answer": "ok",
                    }
                ),
                "pass_results": [],
            }
        return {
            "status": "complete",
            "final_answer": "Perspective output",
            "pass_results": [{"status": "complete", "output": "Perspective output"}],
            "confidence": 0.65,
        }

    def fake_queue_tools(*_args, **_kwargs):
        return ([{"tool_name": "web_search", "query": "policy", "reason": "ground", "priority": 1}], 1)

    def fake_invoke_tools_and_digest(*_args, **kwargs):
        captured_policies.append(kwargs.get("data_policy"))
        digest = SimpleNamespace(
            entries=[
                SimpleNamespace(tool_name="web_search", tool_status="error")
            ],
            formatted_summary="web_search blocked under local policy",
        )
        return digest, 1

    monkeypatch.setattr(orchestrator, "deep_think_passes", fake_deep_think_passes)
    monkeypatch.setattr("deep_think_mcp.executor.queue_tools", fake_queue_tools)
    monkeypatch.setattr("deep_think_mcp.executor.invoke_tools_and_digest", fake_invoke_tools_and_digest)
    monkeypatch.setattr(orchestrator.store, "get_perspective_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator.store, "set_perspective_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        orchestrator.provider_module,
        "build_provider_config",
        lambda _cfg: ProviderConfig(provider="ollama", data_policy="local"),
    )

    result = await orchestrator.run_fan_out(
        question="thread local policy into external tool phase",
        width=1,
        height=1,
        task_class="research",
        provider_config={},
        data_policy="local",
        enable_tool_use=True,
        topology="adaptive",
    )

    assert result["status"] == "complete"
    assert captured_policies == ["local"]
    perspective = next(iter(result["perspective_outputs"].values()))
    assert perspective["tools_invoked"] == ["web_search"]
    assert perspective["tool_errors"] == ["web_search:error"]


@pytest.mark.asyncio
async def test_run_fan_out_caps_perspective_context_in_synthesis_prompt(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    captured: dict[str, str] = {}
    very_long_text = "A" * 30000
    very_long_evidence = "EVIDENCE " * 2000

    async def fake_deep_think_passes(question, **kwargs):
        if kwargs.get("task_class") == "synthesis":
            captured["synthesis_question"] = question
            return {
                "status": "complete",
                "final_answer": json.dumps({
                    "confidence_score": 70,
                    "converged_claims": ["bounded"],
                    "contested_areas": [],
                    "gaps": [],
                    "final_answer": "ok",
                }),
                "pass_results": [],
            }
        return {
            "status": "complete",
            "final_answer": very_long_text,
            "evidence_summary": very_long_evidence,
            "pass_results": [{"status": "complete", "output": very_long_text}],
        }

    monkeypatch.setattr(orchestrator, "deep_think_passes", fake_deep_think_passes)
    async def fake_alarm_scan(*_a, **_k):
        return []

    monkeypatch.setattr(orchestrator.store, "get_perspective_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator.store, "set_perspective_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator, "_fan_out_alarm_scan", fake_alarm_scan)

    result = await orchestrator.run_fan_out(
        question="overflow guard",
        width=3,
        height=1,
        task_class="general",
        provider_cfg=ProviderConfig(provider="ollama"),
    )

    assert result["status"] == "complete"
    assert "synthesis_question" in captured
    assert len(captured["synthesis_question"]) < 50000
    assert "truncated" in captured["synthesis_question"]


@pytest.mark.asyncio
async def test_run_fan_out_surfaces_malformed_synthesis_json(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    async def fake_deep_think_passes(**kwargs):
        if kwargs.get("task_class") == "synthesis":
            return {
                "status": "complete",
                "final_answer": "not valid json",
                "pass_results": [],
            }
        return {
            "status": "complete",
            "final_answer": "Perspective output",
            "pass_results": [{"status": "complete", "output": "Perspective output"}],
        }

    monkeypatch.setattr(orchestrator, "deep_think_passes", fake_deep_think_passes)
    async def fake_alarm_scan(*_a, **_k):
        return []

    monkeypatch.setattr(orchestrator.store, "get_perspective_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator.store, "set_perspective_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator, "_fan_out_alarm_scan", fake_alarm_scan)

    result = await orchestrator.run_fan_out(
        question="malformed synthesis",
        width=2,
        height=1,
        max_width=2,
        task_class="general",
        provider_cfg=ProviderConfig(provider="ollama"),
    )

    assert result["confidence_score"] == 0
    assert result["confidence"] == 0.0
    assert result["synthesis_error"] and "parse failed" in result["synthesis_error"]
    assert any("malformed" in item.lower() for item in result["contested_areas"])


@pytest.mark.asyncio
async def test_run_fan_out_claim_extraction_parse_failure_is_signaled(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_FORCE_LOCAL", "0")
    monkeypatch.setenv("OLLAMA_ONLY_MODE", "0")

    async def fake_deep_think_passes(**kwargs):
        if kwargs.get("task_class") == "synthesis":
            return {
                "status": "complete",
                "final_answer": json.dumps({
                    "confidence_score": 65,
                    "converged_claims": [],
                    "contested_areas": [],
                    "gaps": [],
                    "final_answer": "ok",
                }),
                "pass_results": [],
            }
        return {
            "status": "complete",
            "final_answer": "Perspective analysis body",
            "pass_results": [{"status": "complete", "output": "Perspective analysis body"}],
        }

    async def fake_call_provider(**_kwargs):
        return "not-json-claims"

    monkeypatch.setattr(orchestrator, "deep_think_passes", fake_deep_think_passes)
    monkeypatch.setattr(orchestrator.provider_module, "_tier_provider", lambda _cfg, _tier: "ollama")
    monkeypatch.setattr(orchestrator.provider_module, "_model_for_tier", lambda _cfg, _tier, _tc: "mock")
    monkeypatch.setattr(orchestrator.provider_module, "_call_provider", fake_call_provider)
    async def fake_alarm_scan(*_a, **_k):
        return []

    monkeypatch.setattr(orchestrator.store, "get_perspective_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator.store, "set_perspective_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(orchestrator, "_fan_out_alarm_scan", fake_alarm_scan)

    result = await orchestrator.run_fan_out(
        question="claim extraction parse failure",
        width=1,
        height=1,
        task_class="general",
        provider_cfg=ProviderConfig(provider="ollama"),
        extract_claims=True,
    )

    assert result["claim_sets"]
    assert result["claim_sets"][0]["extractor_error"].startswith("claim_extraction_parse_failed")
    assert result["claim_extraction_warnings"]
