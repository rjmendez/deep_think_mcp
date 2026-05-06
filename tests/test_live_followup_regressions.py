from __future__ import annotations

import json
import sys
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
