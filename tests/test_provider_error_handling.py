#!/usr/bin/env python3
"""Regression tests for provider timeout and transport error handling."""

from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine import provider


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


@pytest.mark.asyncio
async def test_anthropic_timeout_returns_specific_message_and_metric(monkeypatch):
    provider.runtime_metrics.reset_metrics()
    monkeypatch.setattr(provider.httpx, "AsyncClient", lambda timeout: _FakeTimeoutClient())

    with pytest.raises(ValueError, match="Timeout calling Anthropic model 'claude-sonnet-4-6' after 60s"):
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
