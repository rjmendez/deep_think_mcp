"""CreativeReasoningEngine — high-temperature, ambitious reasoning modes for deep_think.

Supports four creative modes:
  - lateral-thinking: sideways problem solving, constraint-violation exploration
  - blue-sky:         unconstrained ideation, "what if" scenarios
  - socratic:         questioning assumptions, dialectical exploration
  - evolutionary:     iterative idea building, temperature decreases across passes

Temperature scheduling (per pass):
  - Exploration passes 1-2: 0.8–1.0  (high novelty)
  - Middle passes 3-4:       0.6–0.7  (medium refinement)
  - Final pass:              0.3–0.5  (validation / convergence)
  - Dynamic adjustment:      novelty_score nudges temperature up/down ±0.05

Quality metrics computed per pass:
  - novelty_score   (0-1): divergence from baseline reasoning
  - feasibility_score (0-1): implementability / realism
  - impact_score      (0-1): potential significance
  - combined_score    = novelty × feasibility × impact

Learning / adaptation:
  - Track which passes validate well via Nova (if available)
  - Log creativity metrics per job for trend analysis
  - Adjust prompt temperature hint based on novelty feedback
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Creative mode definitions
# ---------------------------------------------------------------------------

CREATIVE_MODES = ("lateral-thinking", "blue-sky", "socratic", "evolutionary")


# ---------------------------------------------------------------------------
# Temperature scheduling
# ---------------------------------------------------------------------------

# Base schedule: indexed by pass_num (1-indexed).
# Pass numbers beyond the table length use the last entry.
_BASE_TEMPERATURE_SCHEDULE: dict[int, float] = {
    1: 0.95,
    2: 0.85,
    3: 0.65,
    4: 0.60,
    5: 0.40,
}
_FINAL_PASS_TEMPERATURE = 0.35


def get_temperature(pass_num: int, total_passes: int, novelty_score: float = 0.5) -> float:
    """Return the temperature to use for a given pass.

    Final pass always uses low temperature for convergence.
    Dynamic adjustment: if novelty_score > 0.7 (ideas are highly novel / wild),
    nudge temperature slightly down to anchor; if < 0.3 (too conservative),
    nudge up to push for more divergence.

    Args:
        pass_num:     Current pass number (1-indexed).
        total_passes: Total number of passes planned.
        novelty_score: Novelty score from the previous pass (0-1, default 0.5).

    Returns:
        Temperature float in [0.1, 1.0].
    """
    if pass_num == total_passes:
        base = _FINAL_PASS_TEMPERATURE
    else:
        base = _BASE_TEMPERATURE_SCHEDULE.get(pass_num, _BASE_TEMPERATURE_SCHEDULE[max(_BASE_TEMPERATURE_SCHEDULE)])

    # Dynamic nudge based on novelty feedback from prior pass
    if novelty_score > 0.7:
        adjustment = -0.05  # too wild — anchor slightly
    elif novelty_score < 0.3:
        adjustment = +0.05  # too conservative — push harder
    else:
        adjustment = 0.0

    return max(0.1, min(1.0, base + adjustment))


# ---------------------------------------------------------------------------
# Dynamic prompt templates (per mode)
# ---------------------------------------------------------------------------

_LATERAL_THINKING_TEMPLATES: list[str] = [
    # Pass 1: break the obvious frame
    (
        "You are a lateral thinking expert. Your mission: deliberately look sideways at this problem.\n\n"
        "1. State the obvious, conventional solution in one sentence — then DISCARD it.\n"
        "2. List 5 constraints that everyone assumes are fixed. Challenge each one: what if it were reversed?\n"
        "3. Generate 3 unconventional solution directions that violate at least one conventional constraint.\n"
        "Do NOT converge yet. Generate divergent possibilities only.\n\n"
        "[CREATIVITY MODE: lateral-thinking | PASS 1 — constraint inversion]\n"
        "[QUESTION]: {question}"
    ),
    # Pass 2: random stimulus injection
    (
        "You are a lateral thinking expert in Pass 2.\n\n"
        "Prior exploration identified these unconventional directions:\n{prior_summary}\n\n"
        "Now apply Random Entry: pick any unrelated domain (biology, music, architecture, cooking — your choice) "
        "and find an analogy to the problem. Describe:\n"
        "1. The analogy and why it is structurally similar.\n"
        "2. Two new solution ideas it suggests that were NOT in the prior pass.\n"
        "3. Which prior idea gains the most strength when viewed through this lens?\n\n"
        "[CREATIVITY MODE: lateral-thinking | PASS 2 — random stimulus]\n"
        "[QUESTION]: {question}"
    ),
    # Pass 3: provocation and movement
    (
        "You are a lateral thinking expert in Pass 3 (provocation & movement).\n\n"
        "Prior passes produced:\n{prior_summary}\n\n"
        "Apply Provocation: state something deliberately wrong or absurd about the problem, "
        "then 'move' from that provocation to a practical idea. Show your movement step.\n"
        "Then: which idea from all passes so far is most promising? Why?\n\n"
        "[CREATIVITY MODE: lateral-thinking | PASS 3 — provocation]\n"
        "[QUESTION]: {question}"
    ),
    # Final: harvest and score
    (
        "You are a lateral thinking expert in the final validation pass.\n\n"
        "All prior exploration:\n{prior_summary}\n\n"
        "Harvest the 3 best non-obvious ideas discovered across all passes. For each:\n"
        "- Novelty (0-1): how different from standard thinking?\n"
        "- Feasibility (0-1): how implementable is it realistically?\n"
        "- Impact (0-1): if it works, how significant is the outcome?\n"
        "- Combined score = novelty × feasibility × impact\n\n"
        "State your final recommended idea and why.\n\n"
        "[CREATIVITY MODE: lateral-thinking | FINAL PASS — harvest & validate]\n"
        "[QUESTION]: {question}"
    ),
]

_BLUE_SKY_TEMPLATES: list[str] = [
    # Pass 1: unconstrained ideation
    (
        "You are a blue-sky ideation engine with no constraints.\n\n"
        "Ignore ALL practical limitations (budget, physics, current technology, social norms). "
        "Generate 5 'what if' scenarios that reimagine the problem from scratch. "
        "Each scenario should begin with 'What if...' and describe a fundamentally different world "
        "in which this problem is solved or doesn't exist.\n\n"
        "[CREATIVITY MODE: blue-sky | PASS 1 — unconstrained what-if generation]\n"
        "[QUESTION]: {question}"
    ),
    # Pass 2: build on the wildest idea
    (
        "You are a blue-sky ideation engine in Pass 2.\n\n"
        "Prior what-if scenarios:\n{prior_summary}\n\n"
        "Select the WILDEST scenario from above — the one that feels most impossible. "
        "Now: imagine you are living in that world 20 years from now. "
        "Work BACKWARDS: what intermediate steps, technologies, or social changes made it possible? "
        "List at least 5 stepping stones. Be specific.\n\n"
        "[CREATIVITY MODE: blue-sky | PASS 2 — backward induction from utopia]\n"
        "[QUESTION]: {question}"
    ),
    # Pass 3: reality bridge
    (
        "You are a blue-sky ideation engine in Pass 3 (reality bridge).\n\n"
        "All prior exploration:\n{prior_summary}\n\n"
        "Build a bridge: which blue-sky stepping stone is CLOSEST to what is achievable today "
        "with existing (or near-term) technology/resources? "
        "Describe a concrete 6-month experiment that would test whether this stepping stone is viable.\n\n"
        "[CREATIVITY MODE: blue-sky | PASS 3 — reality bridge]\n"
        "[QUESTION]: {question}"
    ),
    # Final
    (
        "You are a blue-sky ideation engine in the final convergence pass.\n\n"
        "All prior exploration:\n{prior_summary}\n\n"
        "Synthesize: identify the single most valuable idea that emerged across all passes — "
        "the one with the best balance of ambition and feasibility. Score it:\n"
        "- Novelty (0-1)\n- Feasibility (0-1)\n- Impact (0-1)\n- Combined = novelty × feasibility × impact\n\n"
        "Conclude with one paragraph describing what success looks like if this idea is pursued.\n\n"
        "[CREATIVITY MODE: blue-sky | FINAL PASS — convergence]\n"
        "[QUESTION]: {question}"
    ),
]

_SOCRATIC_TEMPLATES: list[str] = [
    # Pass 1: assumption audit
    (
        "You are a Socratic questioner. Your role is to expose hidden assumptions.\n\n"
        "Do NOT answer the question directly. Instead:\n"
        "1. List 6 assumptions embedded in the question itself (what does it take for granted?).\n"
        "2. For each assumption, ask a pointed question that challenges it.\n"
        "3. Which assumption, if false, would most radically change how we approach this?\n\n"
        "[CREATIVITY MODE: socratic | PASS 1 — assumption audit]\n"
        "[QUESTION]: {question}"
    ),
    # Pass 2: dialectical tension
    (
        "You are a Socratic questioner in Pass 2 — dialectical exploration.\n\n"
        "Challenged assumptions from Pass 1:\n{prior_summary}\n\n"
        "Select the most powerful challenged assumption. Now write a short dialectic:\n"
        "- THESIS: the conventional wisdom\n"
        "- ANTITHESIS: the strongest challenge to it\n"
        "- SYNTHESIS: a higher-level truth that reconciles both\n\n"
        "Then ask: what NEW question does this synthesis reveal about the problem?\n\n"
        "[CREATIVITY MODE: socratic | PASS 2 — dialectic]\n"
        "[QUESTION]: {question}"
    ),
    # Pass 3: elenchus (cross-examination)
    (
        "You are a Socratic questioner in Pass 3 — elenchus (cross-examination).\n\n"
        "All prior reasoning:\n{prior_summary}\n\n"
        "Take the proposed synthesis from Pass 2 and subject it to rigorous cross-examination:\n"
        "- What is its weakest point?\n"
        "- What evidence would falsify it?\n"
        "- What does it still fail to explain?\n"
        "End with: the one question that, if answered, would resolve the whole problem.\n\n"
        "[CREATIVITY MODE: socratic | PASS 3 — elenchus]\n"
        "[QUESTION]: {question}"
    ),
    # Final
    (
        "You are a Socratic questioner in the final synthesis pass.\n\n"
        "All prior dialectical exploration:\n{prior_summary}\n\n"
        "Answer the question now — but ONLY what you can justify based on what survived elenchus. "
        "Score your answer:\n"
        "- Novelty (0-1): how much does this differ from the conventional answer?\n"
        "- Feasibility (0-1): how grounded and actionable?\n"
        "- Impact (0-1): significance of acting on this insight?\n"
        "- Combined = novelty × feasibility × impact\n\n"
        "[CREATIVITY MODE: socratic | FINAL PASS — justified answer]\n"
        "[QUESTION]: {question}"
    ),
]

_EVOLUTIONARY_TEMPLATES: list[str] = [
    # Pass 1: initial population (high temperature)
    (
        "You are an evolutionary ideation engine. This is Pass 1 — seeding the initial population.\n\n"
        "Generate 4 diverse 'candidate solutions' to the problem. "
        "Each should represent a different strategy, approach, or paradigm. "
        "Label them CANDIDATE A, B, C, D. Be bold — imperfect candidates are better than safe ones.\n\n"
        "[CREATIVITY MODE: evolutionary | PASS 1 — initial population | HIGH TEMPERATURE]\n"
        "[QUESTION]: {question}"
    ),
    # Pass 2: selection and mutation
    (
        "You are an evolutionary ideation engine in Pass 2 — selection and mutation.\n\n"
        "Initial population:\n{prior_summary}\n\n"
        "Apply evolutionary pressure:\n"
        "1. FITNESS TEST: Score each candidate on (novelty, feasibility, impact) 0-1 each.\n"
        "2. SELECTION: Keep the top 2 candidates (highest combined_score = n×f×i).\n"
        "3. MUTATION: Modify each selected candidate in one significant way — improve its weakest dimension.\n"
        "4. CROSSOVER: Create 1 new candidate by combining the strongest features of the top 2.\n\n"
        "[CREATIVITY MODE: evolutionary | PASS 2 — selection & mutation | MEDIUM TEMPERATURE]\n"
        "[QUESTION]: {question}"
    ),
    # Pass 3: refinement (medium-low temperature)
    (
        "You are an evolutionary ideation engine in Pass 3 — refinement.\n\n"
        "Evolved population:\n{prior_summary}\n\n"
        "Apply a second round of selection:\n"
        "1. Re-score all surviving candidates (mutated + crossover).\n"
        "2. Eliminate the weakest. Describe why it did not survive.\n"
        "3. For each remaining candidate, identify ONE concrete obstacle that must be solved "
        "for it to be viable. Propose a solution to that obstacle.\n\n"
        "[CREATIVITY MODE: evolutionary | PASS 3 — refinement | MEDIUM-LOW TEMPERATURE]\n"
        "[QUESTION]: {question}"
    ),
    # Final: fittest solution (low temperature)
    (
        "You are an evolutionary ideation engine in the final validation pass.\n\n"
        "All evolved candidates:\n{prior_summary}\n\n"
        "Declare the FITTEST SOLUTION — the candidate with the highest combined fitness score "
        "after all evolutionary pressure has been applied. Justify your selection.\n\n"
        "Final score breakdown:\n"
        "- Novelty (0-1)\n- Feasibility (0-1)\n- Impact (0-1)\n- Combined = novelty × feasibility × impact\n\n"
        "Describe the solution in enough detail for someone to act on it immediately.\n\n"
        "[CREATIVITY MODE: evolutionary | FINAL PASS — fittest solution | LOW TEMPERATURE]\n"
        "[QUESTION]: {question}"
    ),
]

# Map mode name → templates list
CREATIVE_TEMPLATES: dict[str, list[str]] = {
    "lateral-thinking": _LATERAL_THINKING_TEMPLATES,
    "blue-sky":         _BLUE_SKY_TEMPLATES,
    "socratic":         _SOCRATIC_TEMPLATES,
    "evolutionary":     _EVOLUTIONARY_TEMPLATES,
}


def get_pass_template(mode: str, pass_num: int, total_passes: int) -> str:
    """Return the prompt template for the given mode and pass number.

    For modes with fewer templates than passes, the last template is recycled
    (except the true final pass always uses the last template).

    Args:
        mode:         One of CREATIVE_MODES.
        pass_num:     Current pass (1-indexed).
        total_passes: Total passes planned.

    Returns:
        Prompt template string with {question} and {prior_summary} placeholders.
    """
    templates = CREATIVE_TEMPLATES.get(mode, CREATIVE_TEMPLATES["lateral-thinking"])

    if pass_num == total_passes:
        return templates[-1]

    # Map pass_num to template index; cap at second-to-last (preserve final)
    idx = min(pass_num - 1, len(templates) - 2)
    return templates[idx]


# ---------------------------------------------------------------------------
# Quality metrics extraction
# ---------------------------------------------------------------------------

# Patterns to extract scored metrics from model output
_SCORE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("novelty_score",      re.compile(r"(?i)novelty[^\d]*?([0-9]\.[0-9]+|[01]\.?[0-9]*)", re.DOTALL)),
    ("feasibility_score",  re.compile(r"(?i)feasib[^\d]*?([0-9]\.[0-9]+|[01]\.?[0-9]*)", re.DOTALL)),
    ("impact_score",       re.compile(r"(?i)impact[^\d]*?([0-9]\.[0-9]+|[01]\.?[0-9]*)", re.DOTALL)),
]


def extract_quality_metrics(text: str) -> dict[str, float]:
    """Extract novelty, feasibility, impact scores from model output text.

    Searches for patterns like "Novelty (0-1): 0.7" or "- Novelty: 0.8".
    Falls back to 0.5 for any missing dimension.

    Returns:
        Dict with novelty_score, feasibility_score, impact_score, combined_score.
    """
    scores: dict[str, float] = {}

    for key, pattern in _SCORE_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                val = float(match.group(1))
                scores[key] = max(0.0, min(1.0, val))
            except ValueError:
                scores[key] = 0.5
        else:
            scores[key] = 0.5

    novelty     = scores.get("novelty_score", 0.5)
    feasibility = scores.get("feasibility_score", 0.5)
    impact      = scores.get("impact_score", 0.5)
    scores["combined_score"] = novelty * feasibility * impact

    return scores


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CreativePassResult:
    """Result of a single creative reasoning pass."""
    pass_num:          int
    mode:              str
    framing:           str
    temperature:       float
    output:            str
    novelty_score:     float = 0.5
    feasibility_score: float = 0.5
    impact_score:      float = 0.5
    combined_score:    float = 0.125  # 0.5^3
    nova_verified:     bool  = False
    nova_confidence:   float = 0.0


@dataclass
class CreativeJobResult:
    """Aggregated result for a full creative reasoning job."""
    job_id:         str
    mode:           str
    question:       str
    passes:         list[CreativePassResult] = field(default_factory=list)
    final_answer:   str = ""
    best_pass:      Optional[CreativePassResult] = None
    avg_novelty:    float = 0.0
    avg_feasibility: float = 0.0
    avg_impact:     float = 0.0
    peak_combined:  float = 0.0
    duration_secs:  float = 0.0

    def to_dict(self) -> dict:
        result = {
            "type":            "creative",
            "job_id":          self.job_id,
            "mode":            self.mode,
            "question":        self.question,
            "final_answer":    self.final_answer,
            "avg_novelty":     round(self.avg_novelty, 3),
            "avg_feasibility": round(self.avg_feasibility, 3),
            "avg_impact":      round(self.avg_impact, 3),
            "peak_combined":   round(self.peak_combined, 3),
            "duration_secs":   round(self.duration_secs, 1),
            "pass_outputs":    [
                {
                    "pass_num":          p.pass_num,
                    "mode":              p.mode,
                    "framing":           p.framing,
                    "temperature":       p.temperature,
                    "output":            p.output,
                    "novelty_score":     round(p.novelty_score, 3),
                    "feasibility_score": round(p.feasibility_score, 3),
                    "impact_score":      round(p.impact_score, 3),
                    "combined_score":    round(p.combined_score, 3),
                    "nova_verified":     p.nova_verified,
                    "nova_confidence":   round(p.nova_confidence, 3),
                }
                for p in self.passes
            ],
        }
        if self.best_pass:
            result["best_pass_num"] = self.best_pass.pass_num
        return result


# ---------------------------------------------------------------------------
# Nova verification helper
# ---------------------------------------------------------------------------

async def _verify_with_nova(output: str, question: str) -> tuple[bool, float]:
    """Attempt to verify a creative pass output via Nova's /pre_action endpoint.

    Returns (verified: bool, confidence: float).
    Fail-open — if Nova is unreachable, returns (False, 0.0) without raising.
    """
    import os
    import httpx

    nova_url = os.getenv("NOVA_URL", "http://[REDACTED_INTERNAL_IP]:30850")
    endpoint = f"{nova_url}/pre_action"

    payload = {
        "action":  "creative_reasoning_verify",
        "target":  "deep_think_creative",
        "context": f"Question: {question}\n\nOutput summary: {output[:500]}",
    }

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(endpoint, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                allowed    = data.get("allowed", True)
                confidence = float(data.get("confidence", 0.6) or 0.6)
                return allowed, confidence
    except Exception as exc:
        log.debug("Nova verification unavailable: %s", exc)

    return False, 0.0


# ---------------------------------------------------------------------------
# Adaptation / metrics logging
# ---------------------------------------------------------------------------

@dataclass
class CreativeMetricsLog:
    """Accumulated metrics for trend analysis across creative jobs."""
    total_jobs:          int   = 0
    total_passes:        int   = 0
    verified_passes:     int   = 0
    avg_novelty:         float = 0.0
    avg_feasibility:     float = 0.0
    avg_impact:          float = 0.0
    avg_combined:        float = 0.0
    mode_counts:         dict  = field(default_factory=dict)
    mode_avg_combined:   dict  = field(default_factory=dict)


# Module-level metrics accumulator (in-process; survives across calls in a running server)
_metrics: CreativeMetricsLog = CreativeMetricsLog()


def get_metrics_snapshot() -> dict:
    """Return a snapshot of accumulated creativity metrics."""
    return {
        "total_jobs":       _metrics.total_jobs,
        "total_passes":     _metrics.total_passes,
        "verified_passes":  _metrics.verified_passes,
        "avg_novelty":      round(_metrics.avg_novelty, 3),
        "avg_feasibility":  round(_metrics.avg_feasibility, 3),
        "avg_impact":       round(_metrics.avg_impact, 3),
        "avg_combined":     round(_metrics.avg_combined, 3),
        "mode_counts":      dict(_metrics.mode_counts),
        "mode_avg_combined": {k: round(v, 3) for k, v in _metrics.mode_avg_combined.items()},
    }


def _update_metrics(result: CreativeJobResult) -> None:
    """Update the in-process metrics accumulator with a completed job."""
    global _metrics

    _metrics.total_jobs += 1
    _metrics.total_passes += len(result.passes)
    _metrics.verified_passes += sum(1 for p in result.passes if p.nova_verified)

    # Running average update (Welford-style approximation)
    n = _metrics.total_jobs
    _metrics.avg_novelty     = (_metrics.avg_novelty     * (n - 1) + result.avg_novelty)     / n
    _metrics.avg_feasibility = (_metrics.avg_feasibility * (n - 1) + result.avg_feasibility) / n
    _metrics.avg_impact      = (_metrics.avg_impact      * (n - 1) + result.avg_impact)      / n
    _metrics.avg_combined    = (_metrics.avg_combined    * (n - 1) + result.peak_combined)    / n

    mode = result.mode
    _metrics.mode_counts[mode] = _metrics.mode_counts.get(mode, 0) + 1
    prev_avg = _metrics.mode_avg_combined.get(mode, 0.0)
    mode_n = _metrics.mode_counts[mode]
    _metrics.mode_avg_combined[mode] = (prev_avg * (mode_n - 1) + result.peak_combined) / mode_n

    log.info(
        "[creative] Job metrics — mode=%s novelty=%.2f feasibility=%.2f impact=%.2f combined=%.3f",
        result.mode, result.avg_novelty, result.avg_feasibility, result.avg_impact, result.peak_combined,
    )


# ---------------------------------------------------------------------------
# Prior-pass summary builder
# ---------------------------------------------------------------------------

def _build_prior_summary(pass_results: list[CreativePassResult], max_chars: int = 2000) -> str:
    """Build a condensed summary of all prior pass outputs for injection into the next prompt."""
    if not pass_results:
        return "(no prior passes)"

    parts = []
    for p in pass_results:
        header = f"[Pass {p.pass_num} — {p.framing}]"
        # Truncate each pass to keep the combined summary manageable
        budget = max_chars // max(len(pass_results), 1)
        excerpt = p.output[:budget].strip()
        if len(p.output) > budget:
            excerpt += "\n[...truncated...]"
        parts.append(f"{header}\n{excerpt}")

    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# CreativeReasoningEngine
# ---------------------------------------------------------------------------

class CreativeReasoningEngine:
    """High-temperature, multi-mode creative reasoning engine.

    Usage::

        engine = CreativeReasoningEngine()
        result = await engine.run(
            question="How might we reinvent urban mobility in 2040?",
            mode="blue-sky",
            passes=4,
            provider_config={"provider": "copilot"},
            verify_with_nova=True,
        )
        print(result.final_answer)
        print(result.peak_combined)

    The engine:
    1. Schedules temperatures per-pass (high exploration → low validation).
    2. Selects the appropriate dynamic prompt template for each pass and mode.
    3. Builds a "prior summary" from completed passes and injects it into each
       subsequent template so ideas evolve progressively.
    4. Extracts quality metrics (novelty, feasibility, impact) from each pass output.
    5. Optionally verifies the best pass via Nova for confidence boosting.
    6. Updates an in-process metrics log for trend analysis.
    """

    def __init__(self) -> None:
        self._provider_call = None  # injected by tests or wired in run()

    async def run(
        self,
        question:         str,
        mode:             str                  = "lateral-thinking",
        passes:           int                  = 4,
        provider_config:  Optional[dict]       = None,
        verify_with_nova: bool                 = False,
        job_id:           str                  = "",
    ) -> CreativeJobResult:
        """Execute a full creative reasoning job.

        Args:
            question:         The problem or question to explore.
            mode:             One of 'lateral-thinking', 'blue-sky', 'socratic', 'evolutionary'.
            passes:           Number of passes (2–6, default 4).
            provider_config:  Provider/model overrides (same format as deep_think_async).
            verify_with_nova: If True, verify the best pass output with Nova's /pre_action.
            job_id:           Optional job ID for log correlation.

        Returns:
            CreativeJobResult with per-pass metrics and final answer.
        """
        from . import provider as provider_module

        mode       = mode if mode in CREATIVE_MODES else "lateral-thinking"
        passes     = max(2, min(passes, 6))
        start_time = time.time()

        if provider_config is None:
            provider_config = {}

        cfg = provider_module.build_provider_config(provider_config)

        log.info(
            "[creative] Starting job job_id=%s mode=%s passes=%d provider=%s",
            job_id or "(none)", mode, passes, cfg.provider,
        )

        pass_results: list[CreativePassResult] = []
        last_novelty = 0.5  # seed novelty for temperature scheduling

        for pass_num in range(1, passes + 1):
            temperature = get_temperature(pass_num, passes, last_novelty)
            template    = get_pass_template(mode, pass_num, passes)

            # Build prior summary for injection
            prior_summary = _build_prior_summary(pass_results)

            # Render the prompt template
            prompt = template.format(
                question=question,
                prior_summary=prior_summary,
                creativity_mode=mode,
            )

            framing_name = f"{mode}_pass{pass_num}"
            if pass_num == passes:
                framing_name = f"{mode}_final"

            # Determine tier: exploration passes → medium; final pass → heavy
            tier = "heavy" if pass_num == passes else "medium"
            provider_name = cfg.provider or provider_config.get("provider", "ollama")
            model_name = provider_config.get("model", "")

            log.info(
                "[creative] Pass %d/%d mode=%s temperature=%.2f tier=%s",
                pass_num, passes, mode, temperature, tier,
            )

            try:
                pass_provider_config = dict(provider_config)
                pass_provider_config["temperature"] = temperature
                output = await provider_module._call_provider(
                    provider=provider_name,
                    model=model_name,
                    system=(
                        f"You are a creative reasoning assistant operating in "
                        f"'{mode}' mode. "
                        f"Temperature hint: {temperature:.2f} — "
                        f"{'explore boldly and divergently' if temperature >= 0.7 else 'refine and validate'}."
                    ),
                    user_prompt=prompt,
                    tier=tier,
                    provider_config=pass_provider_config,
                )
            except Exception as exc:
                error_msg = str(exc) or type(exc).__qualname__ or f"Exception: {repr(exc)}"
                custom_params = {}
                try:
                    custom_params = provider_module._custom_params_from_provider_config(
                        provider_name, pass_provider_config
                    )
                except Exception:
                    log.debug("[creative] Failed to extract custom params", exc_info=True)
                log.error(
                    "pass_event %s",
                    json.dumps(
                        {
                            "event": "creative_pass_exception",
                            "job_id": job_id,
                            "mode": mode,
                            "pass_num": pass_num,
                            "framing": framing_name,
                            "tier": tier,
                            "provider": provider_name,
                            "model": model_name,
                            "temperature": temperature,
                            "custom_params": custom_params,
                            "exception_type": type(exc).__qualname__,
                            "error": error_msg,
                        },
                        sort_keys=True,
                        default=str,
                    ),
                )
                log.error("[creative] Pass %d failed: %s", pass_num, error_msg, exc_info=True)
                output = f"[ERROR: {error_msg}]"

            metrics = extract_quality_metrics(output)
            last_novelty = metrics["novelty_score"]

            pr = CreativePassResult(
                pass_num=pass_num,
                mode=mode,
                framing=framing_name,
                temperature=temperature,
                output=output,
                novelty_score=metrics["novelty_score"],
                feasibility_score=metrics["feasibility_score"],
                impact_score=metrics["impact_score"],
                combined_score=metrics["combined_score"],
            )
            pass_results.append(pr)

            log.info(
                "[creative] Pass %d complete — novelty=%.2f feasibility=%.2f impact=%.2f combined=%.3f",
                pass_num, pr.novelty_score, pr.feasibility_score, pr.impact_score, pr.combined_score,
            )

        # Identify best pass by combined_score
        best_pass = max(pass_results, key=lambda p: p.combined_score, default=None)
        final_answer = pass_results[-1].output if pass_results else ""

        # Optional Nova verification on best pass
        if verify_with_nova and best_pass:
            verified, confidence = await _verify_with_nova(best_pass.output, question)
            best_pass.nova_verified   = verified
            best_pass.nova_confidence = confidence
            log.info(
                "[creative] Nova verification — verified=%s confidence=%.2f",
                verified, confidence,
            )

        # Aggregate metrics
        avg_novelty     = sum(p.novelty_score     for p in pass_results) / max(len(pass_results), 1)
        avg_feasibility = sum(p.feasibility_score for p in pass_results) / max(len(pass_results), 1)
        avg_impact      = sum(p.impact_score      for p in pass_results) / max(len(pass_results), 1)
        peak_combined   = max((p.combined_score   for p in pass_results), default=0.0)

        result = CreativeJobResult(
            job_id=job_id,
            mode=mode,
            question=question,
            passes=pass_results,
            final_answer=final_answer,
            best_pass=best_pass,
            avg_novelty=avg_novelty,
            avg_feasibility=avg_feasibility,
            avg_impact=avg_impact,
            peak_combined=peak_combined,
            duration_secs=time.time() - start_time,
        )

        _update_metrics(result)
        return result
