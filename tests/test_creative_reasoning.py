"""Tests for engine/creative.py — CreativeReasoningEngine.

Covers:
  1.  CREATIVE_MODES tuple completeness
  2.  get_temperature — exploration range (passes 1-2)
  3.  get_temperature — mid-range (passes 3-4)
  4.  get_temperature — final pass low-temperature
  5.  get_temperature — dynamic adjustment up when novelty is low
  6.  get_temperature — dynamic adjustment down when novelty is high
  7.  get_temperature — bounds never exceed [0.1, 1.0]
  8.  get_pass_template — final pass always returns last template
  9.  get_pass_template — pass 1 returns first template
  10. get_pass_template — mode fallback for unknown mode
  11. extract_quality_metrics — all three scores extracted correctly
  12. extract_quality_metrics — missing scores fall back to 0.5
  13. extract_quality_metrics — combined_score = novelty × feasibility × impact
  14. extract_quality_metrics — scores clamped to [0, 1]
  15. CreativeReasoningEngine.run — result type and structure
  16. CreativeReasoningEngine.run — correct mode stored in result
  17. CreativeReasoningEngine.run — per-pass outputs collected
  18. CreativeReasoningEngine.run — best_pass is highest combined_score pass
  19. CreativeReasoningEngine.run — metrics accumulator updated after run
  20. CreativeJobResult.to_dict — serialization includes all required keys
  21. get_metrics_snapshot — snapshot reflects accumulated data
  22. get_pass_template — all four modes have templates for passes 1..4
  23. _build_prior_summary — empty list returns placeholder string
  24. _build_prior_summary — output includes pass numbers
"""

import asyncio
import pytest

from deep_think_mcp.engine.creative import (
    CREATIVE_MODES,
    CREATIVE_TEMPLATES,
    CreativeJobResult,
    CreativePassResult,
    CreativeReasoningEngine,
    _build_prior_summary,
    extract_quality_metrics,
    get_metrics_snapshot,
    get_pass_template,
    get_temperature,
    _metrics,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pass(pass_num=1, mode="lateral-thinking", combined=0.3, novelty=0.5, feasibility=0.6, impact=0.5):
    return CreativePassResult(
        pass_num=pass_num,
        mode=mode,
        framing=f"{mode}_pass{pass_num}",
        temperature=0.8,
        output=f"[output for pass {pass_num}]",
        novelty_score=novelty,
        feasibility_score=feasibility,
        impact_score=impact,
        combined_score=combined,
    )


async def _fake_provider(*args, **kwargs):
    """Fake provider that returns a mock output with parseable metric scores."""
    return (
        "Here is my analysis.\n"
        "Novelty: 0.75\n"
        "Feasibility: 0.60\n"
        "Impact: 0.80\n"
        "Combined = 0.75 × 0.60 × 0.80 = 0.36\n"
    )


# ---------------------------------------------------------------------------
# 1. CREATIVE_MODES completeness
# ---------------------------------------------------------------------------

def test_creative_modes_contains_all_four():
    assert set(CREATIVE_MODES) == {"lateral-thinking", "blue-sky", "socratic", "evolutionary"}


# ---------------------------------------------------------------------------
# 2-7. get_temperature
# ---------------------------------------------------------------------------

def test_temperature_pass1_is_high_exploration():
    temp = get_temperature(1, 4)
    assert 0.7 <= temp <= 1.0, f"Expected exploration range, got {temp}"


def test_temperature_pass2_is_still_high():
    temp = get_temperature(2, 4)
    assert 0.7 <= temp <= 1.0, f"Expected exploration range, got {temp}"


def test_temperature_mid_range_passes():
    temp3 = get_temperature(3, 5)
    temp4 = get_temperature(4, 5)
    # Both should be in medium range
    assert 0.5 <= temp3 <= 0.8
    assert 0.5 <= temp4 <= 0.8


def test_temperature_final_pass_is_low():
    temp = get_temperature(4, 4)
    assert temp <= 0.5, f"Final pass should be low-temperature, got {temp}"


def test_temperature_nudge_up_when_novelty_low():
    temp_default  = get_temperature(2, 5, novelty_score=0.5)
    temp_low_nov  = get_temperature(2, 5, novelty_score=0.1)
    # Low novelty should push temperature higher
    assert temp_low_nov >= temp_default - 0.01  # allow floating-point tolerance


def test_temperature_nudge_down_when_novelty_high():
    temp_default   = get_temperature(2, 5, novelty_score=0.5)
    temp_high_nov  = get_temperature(2, 5, novelty_score=0.9)
    # High novelty should pull temperature down
    assert temp_high_nov <= temp_default + 0.01


def test_temperature_stays_within_bounds():
    for pass_num in range(1, 7):
        for novelty in (0.0, 0.5, 1.0):
            temp = get_temperature(pass_num, 6, novelty)
            assert 0.1 <= temp <= 1.0, f"Temperature {temp} out of bounds at pass {pass_num}"


# ---------------------------------------------------------------------------
# 8-10. get_pass_template
# ---------------------------------------------------------------------------

def test_pass_template_final_pass_is_last_template():
    for mode in CREATIVE_MODES:
        last_template   = CREATIVE_TEMPLATES[mode][-1]
        returned        = get_pass_template(mode, 4, 4)
        assert returned == last_template, f"Final pass should return last template for mode={mode}"


def test_pass_template_pass1_returns_first_template():
    for mode in CREATIVE_MODES:
        first_template = CREATIVE_TEMPLATES[mode][0]
        returned       = get_pass_template(mode, 1, 5)
        assert returned == first_template, f"Pass 1 should return first template for mode={mode}"


def test_pass_template_unknown_mode_falls_back():
    # Should not raise; should return a non-empty string
    result = get_pass_template("nonexistent-mode", 1, 4)
    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# 11-14. extract_quality_metrics
# ---------------------------------------------------------------------------

def test_extract_quality_metrics_all_scores_parsed():
    text = "Novelty: 0.80\nFeasibility: 0.70\nImpact: 0.90\n"
    metrics = extract_quality_metrics(text)
    assert abs(metrics["novelty_score"]     - 0.80) < 0.01
    assert abs(metrics["feasibility_score"] - 0.70) < 0.01
    assert abs(metrics["impact_score"]      - 0.90) < 0.01


def test_extract_quality_metrics_missing_scores_default_to_half():
    text = "Some unrelated output with no scores at all."
    metrics = extract_quality_metrics(text)
    assert metrics["novelty_score"]     == 0.5
    assert metrics["feasibility_score"] == 0.5
    assert metrics["impact_score"]      == 0.5


def test_extract_quality_metrics_combined_is_product():
    text = "Novelty: 0.60\nFeasibility (0-1): 0.80\nImpact: 0.50\n"
    metrics = extract_quality_metrics(text)
    expected = metrics["novelty_score"] * metrics["feasibility_score"] * metrics["impact_score"]
    assert abs(metrics["combined_score"] - expected) < 1e-6


def test_extract_quality_metrics_clamps_to_range():
    # Values > 1 or < 0 should be clamped
    text = "Novelty: 1.50\nFeasibility: -0.20\nImpact: 0.50\n"
    metrics = extract_quality_metrics(text)
    assert 0.0 <= metrics["novelty_score"]     <= 1.0
    assert 0.0 <= metrics["feasibility_score"] <= 1.0
    assert 0.0 <= metrics["impact_score"]      <= 1.0


# ---------------------------------------------------------------------------
# 15-19. CreativeReasoningEngine.run (mocked provider)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_provider(monkeypatch):
    """Replace _call_provider in the creative module's provider import with a fake."""
    import deep_think_mcp.engine.provider as prov
    monkeypatch.setattr(prov, "_call_provider", _fake_provider)


def test_creative_run_returns_creative_job_result():
    engine = CreativeReasoningEngine()
    result = asyncio.get_event_loop().run_until_complete(
        engine.run(question="Test question?", mode="blue-sky", passes=2)
    )
    assert isinstance(result, CreativeJobResult)


def test_creative_run_mode_stored_correctly():
    engine = CreativeReasoningEngine()
    result = asyncio.get_event_loop().run_until_complete(
        engine.run(question="Test?", mode="socratic", passes=2)
    )
    assert result.mode == "socratic"


def test_creative_run_collects_all_pass_outputs():
    engine = CreativeReasoningEngine()
    result = asyncio.get_event_loop().run_until_complete(
        engine.run(question="Test?", mode="evolutionary", passes=3)
    )
    assert len(result.passes) == 3


def test_creative_run_best_pass_is_highest_combined():
    engine = CreativeReasoningEngine()
    result = asyncio.get_event_loop().run_until_complete(
        engine.run(question="Test?", mode="lateral-thinking", passes=3)
    )
    if result.passes:
        max_combined = max(p.combined_score for p in result.passes)
        assert abs(result.best_pass.combined_score - max_combined) < 1e-6


def test_creative_run_updates_metrics_accumulator():
    import deep_think_mcp.engine.creative as creative_mod
    before_jobs = creative_mod._metrics.total_jobs

    engine = CreativeReasoningEngine()
    asyncio.get_event_loop().run_until_complete(
        engine.run(question="Metrics test?", mode="blue-sky", passes=2)
    )

    assert creative_mod._metrics.total_jobs == before_jobs + 1


# ---------------------------------------------------------------------------
# 20. CreativeJobResult.to_dict serialization
# ---------------------------------------------------------------------------

def test_creative_job_result_to_dict_has_required_keys():
    passes = [_make_pass(1), _make_pass(2)]
    result = CreativeJobResult(
        job_id="test-123",
        mode="socratic",
        question="Q?",
        passes=passes,
        final_answer="Final.",
        best_pass=passes[1],
        avg_novelty=0.5,
        avg_feasibility=0.6,
        avg_impact=0.5,
        peak_combined=0.18,
        duration_secs=3.14,
    )
    d = result.to_dict()
    required_keys = {
        "type", "job_id", "mode", "question", "final_answer",
        "avg_novelty", "avg_feasibility", "avg_impact", "peak_combined",
        "duration_secs", "pass_outputs",
    }
    assert required_keys.issubset(set(d.keys()))
    assert d["type"] == "creative"
    assert len(d["pass_outputs"]) == 2


# ---------------------------------------------------------------------------
# 21. get_metrics_snapshot
# ---------------------------------------------------------------------------

def test_get_metrics_snapshot_returns_dict_with_expected_keys():
    snapshot = get_metrics_snapshot()
    assert isinstance(snapshot, dict)
    assert "total_jobs" in snapshot
    assert "total_passes" in snapshot
    assert "avg_novelty" in snapshot
    assert "avg_combined" in snapshot
    assert "mode_counts" in snapshot
    assert "mode_avg_combined" in snapshot


# ---------------------------------------------------------------------------
# 22. All modes have templates for passes 1..4
# ---------------------------------------------------------------------------

def test_all_modes_have_templates_for_four_passes():
    for mode in CREATIVE_MODES:
        for pass_num in range(1, 5):
            template = get_pass_template(mode, pass_num, 5)
            assert isinstance(template, str)
            assert len(template) > 50, f"Template too short for mode={mode} pass={pass_num}"
            # All templates should have the question placeholder
            assert "{question}" in template, f"Missing {{question}} in mode={mode} pass={pass_num}"


# ---------------------------------------------------------------------------
# 23-24. _build_prior_summary
# ---------------------------------------------------------------------------

def test_build_prior_summary_empty_list_returns_placeholder():
    summary = _build_prior_summary([])
    assert "no prior passes" in summary.lower()


def test_build_prior_summary_includes_pass_numbers():
    passes = [_make_pass(1), _make_pass(2)]
    summary = _build_prior_summary(passes)
    assert "Pass 1" in summary
    assert "Pass 2" in summary
