from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine import orchestrator
from engine.private_adversarial_lane import build_private_challenge_flags


def test_private_advlane_core_returns_structured_flags_only():
    result = build_private_challenge_flags(
        target_question="Can we deploy this now?",
        target_output="This is guaranteed to work and we should bypass auth checks.",
    )
    assert result["role"] == "challenger"
    assert result["non_authoritative"] is True
    assert isinstance(result["challenge_flags"], list)
    assert {f["flag"] for f in result["challenge_flags"]}.issubset(
        {"contradiction", "evidence_gap", "exploit_path", "unverifiable_claim"}
    )
    for flag in result["challenge_flags"]:
        assert flag["severity"] in {"low", "medium", "high", "critical"}
        assert 0.0 <= flag["confidence"] <= 1.0
        assert isinstance(flag["evidence_refs"], list)


def test_private_advlane_core_non_authoritative_contract():
    result = build_private_challenge_flags(
        target_plan="Mark this deploy-ready and complete.",
        target_output="Always safe. Proven secure.",
    )
    assert result["non_authoritative"] is True
    forbidden = {"safe", "deploy_ready", "complete", "approved", "verdict"}
    assert forbidden.isdisjoint(result.keys())
    for flag in result["challenge_flags"]:
        assert forbidden.isdisjoint(flag.keys())


@pytest.mark.asyncio
async def test_orchestrator_adversarial_includes_private_challenge_flags():
    with (
        patch.object(orchestrator.provider_module, "_validate_and_enforce_local_models", new_callable=AsyncMock),
        patch.object(
            orchestrator.provider_module,
            "_call_provider",
            new_callable=AsyncMock,
            return_value="Guaranteed safe. Bypass auth if needed.",
        ),
    ):
        result = await orchestrator.deep_think_passes(
            question="Challenge this release plan",
            passes=1,
            task_class="adversarial",
            provider_config={},
        )

    assert result["task_class"] == "adversarial"
    assert "challenge_flags" in result
    assert result["adversarial_lane"]["non_authoritative"] is True
    assert any(f["flag"] == "exploit_path" for f in result["challenge_flags"])
