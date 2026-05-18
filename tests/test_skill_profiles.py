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
    planning_profile = directives_module.get_skill_profile("planning")

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
    assert planning_profile is not None
    assert planning_profile["directives"][0][0] == "problem_structuring"


def test_resolve_skill_selection_keeps_planning_route():
    selected, profile = directives_module.resolve_skill_selection("planning")
    assert selected == "planning"
    assert profile["task_class"] == "planning"


def test_resolve_skill_selection_warns_on_unknown_and_falls_back(caplog):
    with caplog.at_level("WARNING", logger=directives_module.__name__):
        selected, profile = directives_module.resolve_skill_selection("planning_typo")
    assert selected == "general"
    assert profile["task_class"] == "general"
    assert any("falling back to 'general'" in record.message for record in caplog.records)


def test_data_governance_and_research_synthesis_use_class_specific_mandates():
    data_profile = directives_module.get_skill_profile("data_governance")
    research_profile = directives_module.get_skill_profile("research_synthesis")

    assert data_profile is not None
    assert research_profile is not None
    assert data_profile["directives"][0][0] == "telemetry_inventory"
    assert research_profile["directives"][0][0] == "literature_survey"

    data_mandates = directives_module.PERSPECTIVE_MANDATES["data_governance"]
    research_mandates = directives_module.PERSPECTIVE_MANDATES["research_synthesis"]

    assert "stream_integrity" in data_mandates
    assert "synthesis_decision" in research_mandates
    assert data_profile["fan_out"]["mandates"] == data_mandates
    assert research_profile["fan_out"]["mandates"] == research_mandates


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
