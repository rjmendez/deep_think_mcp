#!/usr/bin/env python3
"""Regression tests for provider timeout and transport error handling."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine import provider


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeTimeoutClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, *args, **kwargs):
        raise provider.httpx.TimeoutException("timed out")


class _FakeTransportClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, *args, **kwargs):
        request = provider.httpx.Request("POST", "http://localhost/api/chat")
        raise provider.httpx.ConnectError("connection refused", request=request)


class _FakeRetryClient:
    def __init__(self, state: dict):
        self._state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, *args, **kwargs):
        self._state["calls"] += 1
        if self._state["calls"] == 1:
            return _FakeResponse(429, {"error": "rate limited"}, "rate limited")
        return _FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})


class _FakeModelMissingClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, *args, **kwargs):
        return _FakeResponse(404, {"error": "model 'phi4-mini:latest' not found"})


class _FakeRuntimeModelFailureClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, *args, **kwargs):
        return _FakeResponse(500, {"error": "llama runner process has terminated: check_tensor_dims rope_factors_long.weight wrong shape"})


@pytest.mark.asyncio
async def test_anthropic_timeout_returns_specific_message_and_metric(monkeypatch):
    provider.runtime_metrics.reset_metrics()
    monkeypatch.setattr(provider.httpx, "AsyncClient", lambda timeout: _FakeTimeoutClient())

    with pytest.raises(ValueError, match="Timeout calling Anthropic model 'claude-sonnet-4-6' after 120.0s"):
        await provider._call_anthropic(
            "sk-ant-test",
            "claude-sonnet-4-6",
            "system",
            "user prompt",
            "light",
        )

    m = provider.runtime_metrics.get_metrics()
    assert m.timeout_count == 1
    assert m.timeout_by_component["anthropic"] == 1


@pytest.mark.asyncio
async def test_ollama_transport_error_is_not_reported_as_timeout(monkeypatch):
    provider.runtime_metrics.reset_metrics()
    monkeypatch.setattr(provider.httpx, "AsyncClient", lambda timeout: _FakeTransportClient())

    with pytest.raises(ValueError, match="Ollama transport error calling model 'phi4-mini:latest'"):
        await provider._call_ollama(
            "http://localhost:11434",
            "phi4-mini:latest",
            "system",
            "user prompt",
            "light",
        )

    m = provider.runtime_metrics.get_metrics()
    assert m.timeout_count == 0


@pytest.mark.asyncio
async def test_copilot_retries_transient_rate_limit_then_succeeds(monkeypatch):
    state = {"calls": 0}
    monkeypatch.setattr(provider.httpx, "AsyncClient", lambda timeout: _FakeRetryClient(state))
    monkeypatch.setattr(provider.asyncio, "sleep", AsyncMock())

    result = await provider._call_copilot(
        "token",
        "gpt-5.4-mini",
        "system",
        "prompt",
        "light",
    )

    assert result == "ok"
    assert state["calls"] == 2


@pytest.mark.asyncio
async def test_ollama_model_not_found_raises_actionable_typed_error(monkeypatch):
    monkeypatch.setattr(provider.httpx, "AsyncClient", lambda timeout: _FakeModelMissingClient())

    with pytest.raises(provider.ProviderModelNotFoundError, match="Run: ollama pull phi4-mini:latest"):
        await provider._call_ollama(
            "http://localhost:11434",
            "phi4-mini:latest",
            "system",
            "prompt",
            "light",
        )


@pytest.mark.asyncio
async def test_call_provider_ollama_retries_with_discovered_fallback_on_model_missing(monkeypatch):
    calls = {"n": 0}

    async def _fake_call_ollama(base_url, model, system, user_prompt, tier, custom_params=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise provider.ProviderModelNotFoundError("missing")
        return f"ok:{model}"

    monkeypatch.setattr(provider, "_call_ollama", _fake_call_ollama)
    monkeypatch.setattr(provider, "_fallback_available_ollama_model", lambda tier, base_url="": "heretic-llama31-8b-instruct:latest")

    result = await provider._call_provider(
        provider="ollama",
        model="phi4-mini:latest",
        system="system",
        user_prompt="prompt",
        tier="light",
        provider_config={"base_url": "http://localhost:11434"},
    )

    assert result == "ok:heretic-llama31-8b-instruct:latest"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_ollama_runtime_model_failure_raises_typed_error_and_quarantines(monkeypatch):
    monkeypatch.setattr(provider.httpx, "AsyncClient", lambda timeout: _FakeRuntimeModelFailureClient())
    monkeypatch.setattr(provider, "_ollama_model_quarantine", {})

    with pytest.raises(provider.ProviderModelRuntimeError, match="temporarily quarantined"):
        await provider._call_ollama(
            "http://localhost:11434",
            "heretic-phi4-mini-reasoning:latest",
            "system",
            "prompt",
            "light",
        )

    assert provider._is_ollama_model_quarantined("heretic-phi4-mini-reasoning:latest", "http://localhost:11434")


@pytest.mark.asyncio
async def test_call_provider_ollama_retries_with_discovered_fallback_on_runtime_failure(monkeypatch):
    calls = {"n": 0}

    async def _fake_call_ollama(base_url, model, system, user_prompt, tier, custom_params=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise provider.ProviderModelRuntimeError("runtime failure")
        return f"ok:{model}"

    monkeypatch.setattr(provider, "_call_ollama", _fake_call_ollama)
    monkeypatch.setattr(provider, "_fallback_available_ollama_model", lambda tier, base_url="": "heretic-llama31-8b-instruct:latest")

    result = await provider._call_provider(
        provider="ollama",
        model="heretic-phi4-mini-reasoning:latest",
        system="system",
        user_prompt="prompt",
        tier="light",
        provider_config={"base_url": "http://localhost:11434"},
    )

    assert result == "ok:heretic-llama31-8b-instruct:latest"
    assert calls["n"] == 2
