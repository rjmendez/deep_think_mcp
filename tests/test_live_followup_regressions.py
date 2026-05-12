from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from deep_think_mcp import worker
from deep_think_mcp.api import reasoning as reasoning_api


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


@pytest.mark.asyncio
async def test_deep_think_async_applies_defaults_when_optional_args_omitted(monkeypatch):
    fake_mcp = FakeMCP()
    reasoning_api.register(fake_mcp)
    deep_think_async = fake_mcp.tools["deep_think_async"]

    captured: dict[str, object] = {}

    def fake_create_job(**kwargs):
        captured.update(kwargs)
        return "job-123"

    monkeypatch.setattr(reasoning_api.store, "create_job", fake_create_job)
    monkeypatch.setattr(reasoning_api, "build_provider_config", lambda _pc: SimpleNamespace(provider="anthropic"))
    monkeypatch.setattr(reasoning_api, "model_summary", lambda _cfg, _cls: "summary")

    result = await deep_think_async(question="test omitted defaults")

    assert result["status"] == "queued"
    assert captured["passes"] == 3
    provider_config = json.loads(captured["provider_config_json"])
    assert provider_config["fan_out"] is False
    assert provider_config["width"] == 1
    assert provider_config["height"] == 1


@pytest.mark.asyncio
async def test_deep_think_async_blocks_when_runtime_stale(monkeypatch):
    fake_mcp = FakeMCP()
    reasoning_api.register(fake_mcp)
    deep_think_async = fake_mcp.tools["deep_think_async"]

    monkeypatch.setattr(
        reasoning_api.runtime_guard,
        "stale_runtime_error",
        lambda: {
            "status": "failed",
            "error": "RUNTIME_STALE",
            "restart_required": True,
        },
    )

    result = await deep_think_async(question="stale runtime check")
    assert result["status"] == "failed"
    assert result["error"] == "RUNTIME_STALE"
    assert result["restart_required"] is True


@pytest.mark.asyncio
async def test_worker_preserves_cloud_data_policy_for_execution(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_deep_think_passes(**kwargs):
        captured.update(kwargs)
        return {
            "status": "complete",
            "final_answer": "ok",
            "pass_outputs": ["ok"],
            "pass_results": [
                {"status": "complete", "output": "ok", "pass_num": 1, "provider": "anthropic", "model": "test-model"}
            ],
            "confidence": 0.5,
            "duration_secs": 0.1,
        }

    async def fake_pipeline_run(result, job_id=""):
        return result

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(worker.engine, "deep_think_passes", fake_deep_think_passes)
    monkeypatch.setattr(worker.engine, "build_provider_config", lambda cfg: SimpleNamespace(provider=cfg.get("provider", "")))
    monkeypatch.setattr(worker, "_verification_pipeline", SimpleNamespace(run=fake_pipeline_run))
    monkeypatch.setattr(worker.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(worker.store, "complete_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.store, "fail_job", lambda *_args, **_kwargs: None)

    job = {
        "job_id": "job-cloud-routing",
        "question": "Route this to cloud",
        "passes": 4,
        "provider_config_json": json.dumps(
            {
                "provider": "anthropic",
                "data_policy": "cloud",
                "task_class": "general",
                "enable_research": True,
                "width": 1,
                "height": 1,
                "fan_out": False,
            }
        ),
    }

    await worker._run_job(job)

    assert captured["data_policy"] == "cloud"
    assert captured["provider_config"]["data_policy"] == "cloud"
    assert captured["provider_config"]["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_deep_think_fan_out_sets_workload_timeout(monkeypatch):
    fake_mcp = FakeMCP()
    reasoning_api.register(fake_mcp)
    deep_think_fan_out = fake_mcp.tools["deep_think_fan_out"]

    captured: dict[str, object] = {}

    def fake_create_job(**kwargs):
        captured.update(kwargs)
        return "job-timeout"

    monkeypatch.setattr(reasoning_api.store, "create_job", fake_create_job)
    monkeypatch.setattr(reasoning_api, "build_provider_config", lambda _pc: SimpleNamespace(provider="anthropic"))
    monkeypatch.setattr(reasoning_api, "model_summary", lambda _cfg, _cls: "summary")
    monkeypatch.setattr(
        reasoning_api,
        "resolve_skill_selection",
        lambda _requested: ("code_review", {"task_class": "code_review", "version": 1}),
    )

    await deep_think_fan_out(
        question="audit service",
        width=6,
        height=5,
        max_parallel=1,
        max_width=6,
        topology="adaptive",
        enable_tool_use=True,
        adaptive_config={"tool_timeout": 30, "max_tool_calls_global": 20, "max_tool_calls_per_perspective": 5},
    )

    assert captured["timeout_secs"] > 300


@pytest.mark.asyncio
async def test_deep_think_fan_out_blocks_when_runtime_stale(monkeypatch):
    fake_mcp = FakeMCP()
    reasoning_api.register(fake_mcp)
    deep_think_fan_out = fake_mcp.tools["deep_think_fan_out"]

    monkeypatch.setattr(
        reasoning_api.runtime_guard,
        "stale_runtime_error",
        lambda: {
            "status": "failed",
            "error": "RUNTIME_STALE",
            "restart_required": True,
        },
    )

    result = await deep_think_fan_out(question="stale fanout check")
    assert result["status"] == "failed"
    assert result["error"] == "RUNTIME_STALE"
    assert result["restart_required"] is True


@pytest.mark.asyncio
async def test_get_thinking_result_promotes_fan_out_fields_by_key_presence(monkeypatch):
    fake_mcp = FakeMCP()
    reasoning_api.register(fake_mcp)
    get_thinking_result = fake_mcp.tools["get_thinking_result"]

    fan_out_like_payload = {
        "status": "failed",
        "confidence_score": None,
        "converged_claims": [],
        "contested_areas": [],
        "claim_sets": [],
        "inference_only": False,
        "grounding_warnings": [],
        "tools_invoked_total": 0,
        "tool_successes_total": 0,
        "adaptive_triggered": False,
        "perspective_outputs": {"a": {"synthesis": "partial", "status": "complete"}},
    }
    monkeypatch.setattr(
        reasoning_api.store,
        "get_job",
        lambda _job_id: {
            "job_id": "job-1",
            "status": "complete",
            "provider": "anthropic",
            "model_summary": "summary",
            "created_at": datetime.now().isoformat(),
            "completed_at": datetime.now().isoformat(),
            "result": json.dumps(fan_out_like_payload),
        },
    )

    result = await get_thinking_result("job-1")

    assert "runtime_fingerprint" in result
    assert "runtime_stale" in result
    assert "contested_areas" in result and result["contested_areas"] == []
    assert "claim_sets" in result and result["claim_sets"] == []


@pytest.mark.asyncio
async def test_get_thinking_result_backfills_missing_fan_out_defaults(monkeypatch):
    fake_mcp = FakeMCP()
    reasoning_api.register(fake_mcp)
    get_thinking_result = fake_mcp.tools["get_thinking_result"]

    sparse_payload = {
        "type": "fan_out",
        "status": "failed",
        "perspectives_succeeded": 0,
        "final_answer": "",
    }
    monkeypatch.setattr(
        reasoning_api.store,
        "get_job",
        lambda _job_id: {
            "job_id": "job-sparse",
            "status": "complete",
            "provider": "anthropic",
            "model_summary": "summary",
            "created_at": datetime.now().isoformat(),
            "completed_at": datetime.now().isoformat(),
            "result": json.dumps(sparse_payload),
        },
    )

    result = await get_thinking_result("job-sparse")

    assert result["status"] == "failed"
    assert result["job_status"] == "complete"
    assert result["result_status"] == "failed"
    assert result["tools_invoked_total"] == 0
    assert result["tool_successes_total"] == 0
    assert result["inference_only"] is False
    assert result["grounding_warnings"] == []
    assert "grounding_warnings" in result and result["grounding_warnings"] == []
    assert "tools_invoked_total" in result and result["tools_invoked_total"] == 0
    assert "tool_successes_total" in result and result["tool_successes_total"] == 0
    assert "adaptive_triggered" in result and result["adaptive_triggered"] is False


@pytest.mark.asyncio
async def test_worker_honors_job_timeout_from_record(monkeypatch):
    captured_timeouts: list[float] = []

    async def fake_wait_for(coro, timeout):
        captured_timeouts.append(timeout)
        return await coro

    async def fake_deep_think_passes(**_kwargs):
        return {
            "status": "complete",
            "final_answer": "ok",
            "pass_outputs": ["ok"],
            "pass_results": [{"status": "complete", "output": "ok", "pass_num": 1}],
            "confidence": 0.5,
            "duration_secs": 0.1,
        }

    async def fake_pipeline_run(result, job_id=""):
        return result

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(worker.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(worker.engine, "deep_think_passes", fake_deep_think_passes)
    monkeypatch.setattr(worker.engine, "build_provider_config", lambda cfg: SimpleNamespace(provider=cfg.get("provider", "")))
    monkeypatch.setattr(worker, "_verification_pipeline", SimpleNamespace(run=fake_pipeline_run))
    monkeypatch.setattr(worker.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(worker.store, "complete_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker.store, "fail_job", lambda *_args, **_kwargs: None)

    job = {
        "job_id": "job-timeout",
        "question": "Route this to cloud",
        "passes": 1,
        "timeout_secs": 777,
        "provider_config_json": json.dumps({"provider": "anthropic", "task_class": "general", "fan_out": False}),
    }

    await worker._run_job(job)

    assert captured_timeouts[0] == 777
