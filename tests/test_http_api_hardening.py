from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from deep_think_mcp import http_api
from deep_think_mcp.engine.validator import MAX_QUESTION_LENGTH


def _json_response_body(response):
    return json.loads(response.body.decode("utf-8"))


@pytest.mark.asyncio
async def test_http_async_rejects_empty_question():
    with pytest.raises(HTTPException) as exc_info:
        await http_api.deep_think_async(question="   ")
    assert exc_info.value.status_code == 400
    assert "must not be empty" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_http_fanout_rejects_bad_adaptive_config():
    with pytest.raises(HTTPException) as exc_info:
        await http_api.deep_think_fan_out(
            question="review service",
            adaptive_config={"tool_timeout": "bad"},
        )
    assert exc_info.value.status_code == 400
    assert "adaptive_config.tool_timeout" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_http_async_rejects_overlong_question():
    with pytest.raises(HTTPException) as exc_info:
        await http_api.deep_think_async(question="x" * (MAX_QUESTION_LENGTH + 1))
    assert exc_info.value.status_code == 400
    assert "maximum length" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_http_async_sanitizes_internal_500(monkeypatch):
    monkeypatch.setattr(http_api, "build_provider_config", lambda _cfg: SimpleNamespace(provider="anthropic"))
    monkeypatch.setattr(http_api, "create_job", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("sensitive details")))

    with pytest.raises(HTTPException) as exc_info:
        await http_api.deep_think_async(question="safe question")
    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Internal server error"


@pytest.mark.asyncio
async def test_http_health_checks_db_and_runtime(monkeypatch):
    class _Conn:
        def execute(self, _query):
            return self

        def fetchone(self):
            return 1

        def close(self):
            return None

    monkeypatch.setattr(http_api, "_connect", lambda: _Conn())
    monkeypatch.setattr(
        http_api.runtime_guard,
        "get_runtime_fingerprint",
        lambda: SimpleNamespace(as_dict=lambda: {"runtime_stale": False}),
    )

    response = await http_api.health()
    body = _json_response_body(response)
    assert response.status_code == 200
    assert body["status"] == "healthy"
    assert body["db_status"] == "healthy"
    assert body["runtime_stale"] is False


@pytest.mark.asyncio
async def test_http_health_degrades_when_db_fails(monkeypatch):
    monkeypatch.setattr(http_api, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(
        http_api.runtime_guard,
        "get_runtime_fingerprint",
        lambda: SimpleNamespace(as_dict=lambda: {"runtime_stale": False}),
    )

    response = await http_api.health()
    body = _json_response_body(response)
    assert response.status_code == 503
    assert body["status"] == "degraded"
    assert body["db_status"] == "unavailable"


@pytest.mark.asyncio
async def test_http_async_idempotency_reuses_existing_job(monkeypatch):
    monkeypatch.setattr(http_api, "build_provider_config", lambda _cfg: SimpleNamespace(provider="anthropic"))
    monkeypatch.setattr(
        http_api,
        "lookup_idempotent_job",
        lambda *_args, **_kwargs: {
            "job_id": "existing-job",
            "status": "running",
            "result": None,
            "error": None,
            "completed_at": None,
        },
    )
    monkeypatch.setattr(http_api, "create_job", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("create_job should not be called")))

    result = await http_api.deep_think_async(
        question="safe question",
        idempotency_key="abc123",
    )
    assert result["job_id"] == "existing-job"
    assert result["status"] == "running"
    assert result["idempotent_replay"] is True


@pytest.mark.asyncio
async def test_http_cancel_running_job_requests_worker_cancel(monkeypatch):
    monkeypatch.setattr(
        http_api,
        "request_job_cancellation",
        lambda _job_id, reason="api_cancel": {
            "job_id": "job-1",
            "status": "running",
            "cancel_requested": True,
            "terminal": False,
            "transition": "running_cancel_requested",
        },
    )
    monkeypatch.setattr(http_api._worker, "cancel_running_job", lambda _job_id: True)

    result = await http_api.cancel_job("job-1", reason="requested-by-test")
    assert result["status"] == "running"
    assert result["cancel_requested"] is True
    assert result["worker_cancelled"] is True
