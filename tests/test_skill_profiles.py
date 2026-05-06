from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine import directives as directives_module
from engine import orchestrator
from engine import provider as provider_module


def test_builtin_skill_files_loaded():
    summaries = {entry["id"]: entry for entry in directives_module.list_skill_profiles()}

    assert "general" in summaries
    assert "adversarial" in summaries
    assert "research" in summaries
    assert "planning" in summaries
    assert summaries["general"]["controls"]["evidence_policy"]["answer_highest_verified_layer_only"] is True
    assert summaries["investigation"]["controls"]["layer_order"][0] == "identity"
    assert summaries["adversarial"]["controls"]["force_local"] is True
    assert summaries["adversarial"]["controls"]["block_research_tools"] is True
    assert summaries["planning"]["controls"]["approval_policy"]["explicit_confirmation_for_external_mutation"] is True
    assert summaries["planning"]["task_class"] == "planning"
    assert "planning" in directives_module.TASK_CLASS_NAMES


def test_reload_skill_registry_loads_custom_skill(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "custom-investigation.yaml").write_text(
        "\n".join(
            [
                "kind: deep-think-skill",
                "version: 1",
                "id: custom_investigation",
                "task_class: investigation",
                "description: Investigation profile with explicit skill identity.",
                "routing:",
                "  directive_set: investigation",
                "  mandate_set: investigation",
                "controls:",
                "  verification_mode: evidence",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("DEEP_THINK_SKILLS_DIR", str(skills_dir))
    directives_module.reload_skill_registry()
    try:
        profile = directives_module.get_skill_profile("custom_investigation")
        assert profile is not None
        assert profile["task_class"] == "investigation"
        assert profile["directives"][0][0] == "evidence_inventory"
        assert profile["ollama"]["medium"] == "qwen3.5:27b"
        assert "custom_investigation" in directives_module.SKILL_NAMES
    finally:
        monkeypatch.delenv("DEEP_THINK_SKILLS_DIR", raising=False)
        directives_module.reload_skill_registry()


@pytest.mark.asyncio
async def test_adversarial_skill_forces_local_models():
    with (
        patch.object(provider_module, "_validate_and_enforce_local_models", new_callable=AsyncMock) as mock_local,
        patch.object(provider_module, "_call_provider", new_callable=AsyncMock, return_value="challenge output"),
    ):
        result = await orchestrator.deep_think_passes(
            question="Challenge the default explanation.",
            passes=2,
            task_class="adversarial",
            provider_config={},
        )

    cfg = mock_local.await_args.args[0]
    assert cfg.provider == "ollama"
    assert cfg.light_provider == "ollama"
    assert cfg.medium_provider == "ollama"
    assert cfg.heavy_provider == "ollama"
    assert result["skill"] == "adversarial"
    assert result["task_class"] == "adversarial"


@pytest.mark.asyncio
async def test_planning_skill_runs_in_orchestrator():
    with patch.object(provider_module, "_call_provider", new_callable=AsyncMock, return_value='{"root_cause":"x"}'):
        result = await orchestrator.deep_think_passes(
            question="Plan a remediation for this finding.",
            passes=2,
            task_class="planning",
            data_policy="local",
            provider_config={"provider": "ollama"},
        )

    assert result["status"] == "complete"
    assert result["skill"] == "planning"
    assert result["task_class"] == "planning"
    assert result["final_answer"] == '{"root_cause":"x"}'
