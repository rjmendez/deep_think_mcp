from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine import orchestrator
from engine import provider as provider_module


def test_private_adversarial_lane_not_requested_by_fallback_toggle_alone(monkeypatch):
    monkeypatch.delenv("DEEP_THINK_PRIVATE_ADVERSARIAL_PROVIDER", raising=False)
    monkeypatch.delenv("DEEP_THINK_PRIVATE_ADVERSARIAL_HERETIC_MODEL", raising=False)
    monkeypatch.delenv("DEEP_THINK_PRIVATE_ADVERSARIAL_OLLAMA_BASE_URL", raising=False)
    monkeypatch.setenv("DEEP_THINK_PRIVATE_ADVERSARIAL_ALLOW_ABLITERATION", "1")

    assert provider_module.private_adversarial_lane_requested({}) is False


@pytest.mark.asyncio
async def test_private_adversarial_lane_prefers_local_heretic(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_PRIVATE_ADVERSARIAL_PROVIDER", "auto")
    monkeypatch.setenv("DEEP_THINK_PRIVATE_ADVERSARIAL_HERETIC_MODEL", "dolphin-mistral:latest")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")

    with patch.object(provider_module, "_check_ollama_available", new=AsyncMock(return_value=True)):
        cfg, meta = await provider_module.configure_private_adversarial_lane({})

    assert cfg["provider"] == "ollama"
    assert cfg["light_provider"] == "ollama"
    assert cfg["data_policy"] == "local"
    assert cfg["light"] == "dolphin-mistral:latest"
    assert cfg["medium"] == "dolphin-mistral:latest"
    assert cfg["heavy"] == "dolphin-mistral:latest"
    assert meta["provider"] == "ollama"
    assert meta["degraded_from_local"] is False


@pytest.mark.asyncio
async def test_private_adversarial_lane_degrades_to_abliteration(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_PRIVATE_ADVERSARIAL_PROVIDER", "auto")
    monkeypatch.setenv("DEEP_THINK_PRIVATE_ADVERSARIAL_ALLOW_ABLITERATION", "1")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")

    with patch.object(provider_module, "_check_ollama_available", new=AsyncMock(return_value=False)):
        with patch.object(provider_module, "_has_abliteration_credentials", return_value=True):
            cfg, meta = await provider_module.configure_private_adversarial_lane({})

    assert cfg["provider"] == "abliteration"
    assert cfg["data_policy"] == "cloud"
    assert meta["provider"] == "abliteration"
    assert meta["degraded_from_local"] is True


@pytest.mark.asyncio
async def test_private_adversarial_lane_explicit_failure_when_unavailable(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_PRIVATE_ADVERSARIAL_PROVIDER", "auto")
    monkeypatch.setenv("DEEP_THINK_PRIVATE_ADVERSARIAL_ALLOW_ABLITERATION", "1")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")

    with patch.object(provider_module, "_check_ollama_available", new=AsyncMock(return_value=False)):
        with patch.object(provider_module, "_has_abliteration_credentials", return_value=False):
            with pytest.raises(ValueError, match="Private adversarial lane unavailable"):
                await provider_module.configure_private_adversarial_lane({})


@pytest.mark.asyncio
async def test_private_adversarial_lane_rejects_invalid_provider_value(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_PRIVATE_ADVERSARIAL_PROVIDER", "bogus")
    with pytest.raises(provider_module.ProviderConfigurationError, match="Invalid adversarial_provider"):
        await provider_module.configure_private_adversarial_lane({})


@pytest.mark.asyncio
async def test_private_adversarial_knobs_do_not_override_general_lane(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_PRIVATE_ADVERSARIAL_PROVIDER", "abliteration")

    with patch.object(provider_module, "_call_provider", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = "normal output"
        result = await orchestrator.deep_think_passes(
            question="simple question",
            passes=1,
            task_class="general",
            provider_config={"provider": "ollama"},
            data_policy="local",
        )

    assert result["task_class"] == "general"
    assert mock_call.await_args.kwargs["provider"] == "ollama"


@pytest.mark.asyncio
async def test_adversarial_lane_returns_failed_status_when_routing_unavailable(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_PRIVATE_ADVERSARIAL_PROVIDER", "auto")
    with patch.object(
        provider_module,
        "configure_private_adversarial_lane",
        new=AsyncMock(side_effect=ValueError("no provider available")),
    ):
        with patch.object(provider_module, "_call_provider", new_callable=AsyncMock) as mock_call:
            result = await orchestrator.deep_think_passes(
                question="attack this claim",
                passes=1,
                task_class="adversarial",
                provider_config={},
            )

    assert result["status"] == "failed"
    assert "Private adversarial lane unavailable" in result["error"]
    assert result["final_answer"] == ""
    mock_call.assert_not_called()


@pytest.mark.asyncio
async def test_adversarial_lane_local_policy_conflict_with_abliteration_fails(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_PRIVATE_ADVERSARIAL_PROVIDER", "auto")
    monkeypatch.setenv("DEEP_THINK_PRIVATE_ADVERSARIAL_ALLOW_ABLITERATION", "1")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")

    with patch.object(provider_module, "_check_ollama_available", new=AsyncMock(return_value=False)):
        with patch.object(provider_module, "_has_abliteration_credentials", return_value=True):
            with patch.object(provider_module, "_call_provider", new_callable=AsyncMock) as mock_call:
                result = await orchestrator.deep_think_passes(
                    question="stress test conflict",
                    passes=1,
                    task_class="adversarial",
                    provider_config={},
                    data_policy="local",
                )

    assert result["status"] == "failed"
    assert "conflicts with data_policy=local" in result["error"]
    mock_call.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_orchestrator_private_adversarial_ollama_lane_non_authoritative(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_PRIVATE_ADVERSARIAL_PROVIDER", "auto")
    monkeypatch.setenv("DEEP_THINK_PRIVATE_ADVERSARIAL_HERETIC_MODEL", "dolphin-mistral:latest")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")

    with patch.object(provider_module, "_check_ollama_available", new=AsyncMock(return_value=True)):
        with patch.object(
            provider_module,
            "_available_ollama_models",
            return_value={"dolphin-mistral:latest"},
        ):
            with patch.object(provider_module, "_call_provider", new_callable=AsyncMock) as mock_call:
                mock_call.return_value = "Guaranteed safe. Bypass auth checks."
                result = await orchestrator.deep_think_passes(
                    question="challenge this rollout",
                    passes=1,
                    task_class="adversarial",
                    provider_config={},
                )

    assert result["status"] == "complete"
    assert result["adversarial_lane"]["provider"] == "ollama"
    assert result["adversarial_lane"]["non_authoritative"] is True
    assert any(flag["flag"] == "exploit_path" for flag in result["challenge_flags"])
    assert mock_call.await_args.kwargs["provider"] == "ollama"
    assert mock_call.await_args.kwargs["model"] == "dolphin-mistral:latest"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_orchestrator_private_adversarial_abliteration_degrades_explicitly(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_PRIVATE_ADVERSARIAL_PROVIDER", "auto")
    monkeypatch.setenv("DEEP_THINK_PRIVATE_ADVERSARIAL_ALLOW_ABLITERATION", "1")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")

    with patch.object(provider_module, "_check_ollama_available", new=AsyncMock(return_value=False)):
        with patch.object(provider_module, "_has_abliteration_credentials", return_value=True):
            with patch.object(provider_module, "_call_provider", new_callable=AsyncMock) as mock_call:
                mock_call.return_value = "Challenge assumptions and provide citations."
                result = await orchestrator.deep_think_passes(
                    question="stress test this claim",
                    passes=1,
                    task_class="adversarial",
                    provider_config={},
                )

    assert result["status"] == "complete"
    assert result["adversarial_lane"]["provider"] == "abliteration"
    assert result["adversarial_lane"]["degraded_from_local"] is True
    assert result["adversarial_lane"]["non_authoritative"] is True
    assert mock_call.await_args.kwargs["provider"] == "abliteration"
