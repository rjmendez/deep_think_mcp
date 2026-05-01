"""Multi-pass reasoning engine for deep_think_mcp.

Provider secrets come from environment variables ONLY — never from call arguments.
Per-call provider_config may override: provider name, base_url (Ollama), model IDs,
per-tier provider assignments, task_class, and data_policy.

Provider selection priority (per tier):
  1. data_policy="local"  → always "ollama" regardless of anything else
  2. provider_config["<tier>_provider"] — per-tier call override
  3. DEEP_THINK_<TIER>_PROVIDER env var — per-tier env override
  4. provider_config["provider"] — default provider for all tiers
  5. Auto-detected from credentials: ANTHROPIC_API_KEY → "anthropic",
     GITHUB_COPILOT_OAUTH_TOKEN → "copilot", fallback → "ollama"

GitHub Copilot provider ("copilot"):
  Uses the GitHub Copilot API (api.githubcopilot.com/chat/completions).
  Requires a GitHub OAuth token with copilot scope (gho_ from `gh auth token`).
  run.sh injects this automatically via GITHUB_COPILOT_OAUTH_TOKEN.
  Available models: claude-opus-4.7, claude-sonnet-4.6, gpt-5.4, gpt-5.2-codex, gpt-4o-mini, etc.

Model selection priority (per tier: light / medium / heavy):
  1. provider_config["model"] — single override for all tiers
  2. provider_config["light"] / ["medium"] / ["heavy"] — per-tier call override
  3. DEEP_THINK_{PROVIDER}_{TIER} env var — per-tier env override
  4. Task class profile recommendation (if task_class is set and model not explicitly overridden)
  5. Built-in default for the detected provider

Task class routing:
  task_class="general"       — default, no routing (current behaviour unchanged)
  task_class="auto"          — run Pass-0 classifier; apply result only if confidence >= 0.75
  task_class="code_review"   — qwen2.5-coder / gpt-5.2-codex, code-focused directives
  task_class="investigation" — evidence-weighing, hypothesis testing, IOC triage
  task_class="safety"        — risk detection, harm mapping, granite3-guardian pre-check
  task_class="extraction"    — structured JSON output, schema-constrained passes
  task_class="synthesis"     — writing, summarization, narrative generation
  task_class="reasoning"     — pure logical / mathematical reasoning

Data policy:
  data_policy="any"   (default) — use any configured provider
  data_policy="local"           — ollama ONLY; cloud providers blocked for all tiers
  data_policy="cloud"           — cloud providers preferred; ollama only for light tier

Qwen thinking mode:
  Extended thinking is disabled by default for any model whose name contains "qwen".
  Override with DEEP_THINK_OLLAMA_THINK=true to re-enable, or =false to force-disable
  for all Ollama models.
"""

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field, replace as dataclasses_replace

log = logging.getLogger(__name__)

# Ollama model availability cache — populated by refresh_ollama_models() at startup.
_ollama_discovered: set[str] = set()


# ---------------------------------------------------------------------------
# Provider config
# ---------------------------------------------------------------------------


@dataclass
class ProviderConfig:
    provider: str = ""          # default provider for all tiers
    base_url: str = ""          # Ollama base URL (shared across tiers)
    light: str = ""             # per-tier model ID overrides (explicit)
    medium: str = ""
    heavy: str = ""
    model: str = ""             # single model override (all tiers)
    light_provider: str = ""    # per-tier provider overrides
    medium_provider: str = ""
    heavy_provider: str = ""
    data_policy: str = "any"    # "any" | "local" | "cloud"


# Provider model defaults (used when no task-class profile applies)
_ANTHROPIC_DEFAULTS = {
    "light":  "claude-haiku-4-5",
    "medium": "claude-sonnet-4-6",
    "heavy":  "claude-opus-4-7",
}
_COPILOT_DEFAULTS = {
    "light":  "claude-sonnet-4.6",  # sonnet-4 and gpt-4o-mini both hit tpm limits under concurrent fan-out on copilot_4_cli
    "medium": "claude-sonnet-4.6",
    "heavy":  "claude-opus-4.7",
}
_OLLAMA_DEFAULTS = {
    "light":  "phi4-mini:latest",
    "medium": "qwen3.5:27b",
    "heavy":  "llama3.1:8b",
}


def _read_copilot_token() -> str:
    """Read GitHub Copilot OAuth token.

    Checks (in order):
      1. GITHUB_COPILOT_OAUTH_TOKEN env var (set by run.sh via `gh auth token`)
      2. GITHUB_TOKEN env var (fallback)
    """
    for var in ("GITHUB_COPILOT_OAUTH_TOKEN", "GITHUB_TOKEN"):
        val = os.getenv(var, "").strip()
        if val and val not in ("not-set", ""):
            return val
    return ""


# Keep legacy name as alias for callers that may reference it
_read_github_models_token = _read_copilot_token  # legacy alias — prefer _read_copilot_token


def build_provider_config(overrides: dict | None = None) -> ProviderConfig:
    """Build a ProviderConfig by merging env defaults with per-call overrides."""
    ov = overrides or {}
    cfg = ProviderConfig(
        provider=ov.get("provider", ""),
        base_url=ov.get("base_url", os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")),
        light=ov.get("light", ""),
        medium=ov.get("medium", ""),
        heavy=ov.get("heavy", ""),
        model=ov.get("model", ""),
        light_provider=ov.get("light_provider", os.getenv("DEEP_THINK_LIGHT_PROVIDER", "")),
        medium_provider=ov.get("medium_provider", os.getenv("DEEP_THINK_MEDIUM_PROVIDER", "")),
        heavy_provider=ov.get("heavy_provider", os.getenv("DEEP_THINK_HEAVY_PROVIDER", "")),
        data_policy=ov.get("data_policy", os.getenv("DEEP_THINK_DATA_POLICY", "any")),
    )
    if not cfg.provider:
        anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if anthropic_key and anthropic_key not in ("not-set", ""):
            cfg.provider = "anthropic"
        elif _read_copilot_token():
            cfg.provider = "copilot"
        else:
            cfg.provider = "ollama"
    return cfg


def _tier_provider(cfg: ProviderConfig, tier: str) -> str:
    """Resolve effective provider for a given tier, respecting data_policy."""
    if cfg.data_policy == "local":
        return "ollama"
    override = getattr(cfg, f"{tier}_provider", "")
    effective = override if override else cfg.provider
    # data_policy="cloud": force light tier to ollama if no explicit override
    if cfg.data_policy == "cloud" and tier == "light" and not override:
        return "ollama"
    return effective


def _default_for_provider(provider: str, tier: str) -> str:
    """Return built-in default model for a provider+tier."""
    if provider == "anthropic":
        return _ANTHROPIC_DEFAULTS.get(tier, _ANTHROPIC_DEFAULTS["heavy"])
    if provider == "copilot":
        return _COPILOT_DEFAULTS.get(tier, _COPILOT_DEFAULTS["heavy"])
    return _OLLAMA_DEFAULTS.get(tier, _OLLAMA_DEFAULTS["heavy"])


def _model_for_tier(cfg: ProviderConfig, tier: str, task_class: str = "general") -> str:
    """Resolve model ID with full precedence chain.

    Priority:
      1. cfg.model — single override for all tiers
      2. cfg.light / .medium / .heavy — explicit per-tier call override
      3. DEEP_THINK_{PROVIDER}_{TIER} env var
      4. Task class profile recommendation (validated against discovery)
      5. Dynamically-discovered tier assignment from run_discovery()
      6. Built-in provider default (static fallback)
    """
    # 1. Single override
    if cfg.model:
        return cfg.model
    # 2. Explicit per-tier call override
    call_override = getattr(cfg, tier, "")
    if call_override:
        return call_override
    # 3. Env var override
    provider = _tier_provider(cfg, tier)
    if provider == "anthropic":
        env_val = os.getenv(f"DEEP_THINK_ANTHROPIC_{tier.upper()}", "")
        if env_val:
            return env_val
    elif provider == "copilot":
        env_val = os.getenv(f"DEEP_THINK_COPILOT_{tier.upper()}", "")
        if env_val:
            return env_val
    else:
        env_val = os.getenv(f"DEEP_THINK_MODEL_{tier.upper()}", "")
        if env_val:
            return env_val
    # 4. Task class profile recommendation
    profile_model = _profile_model(task_class, provider, tier)
    if profile_model:
        return profile_model
    # 5. Dynamically-discovered assignment
    discovered = _discovered_tier_model(provider, tier)
    if discovered:
        return discovered
    # 6. Built-in provider default
    return _default_for_provider(provider, tier)


def _profile_model(task_class: str, provider: str, tier: str) -> str:
    """Return task-class profile recommended model, checking discovery availability."""
    from . import discover as _discover  # late import — avoids circular at module load
    disc = _discover.get_current()

    profile = TASK_CLASS_PROFILES.get(task_class, {})
    models = profile.get(provider, {})
    preferred = models.get(tier, "")
    if not preferred:
        return ""

    # For ollama: validate against discovery cache, or legacy _ollama_discovered set
    if provider == "ollama":
        if disc:
            available = {m.model_id for m in disc.models if m.provider == "ollama" and m.is_available}
            if available and preferred not in available:
                log.debug("Profile model %s not in discovered ollama models, skipping", preferred)
                return ""
        elif _ollama_discovered and preferred not in _ollama_discovered:
            log.debug("Profile model %s not available in ollama, skipping", preferred)
            return ""
    return preferred


def _discovered_tier_model(provider: str, tier: str) -> str:
    """Return the dynamically-discovered model for a provider+tier, or ''."""
    from . import discover as _discover
    disc = _discover.get_current()
    if not disc:
        return ""
    assignment = disc.tier_assignments.get(provider)
    if not assignment:
        return ""
    return getattr(assignment, tier, "") or ""


def model_summary(cfg: ProviderConfig, task_class: str = "general") -> str:
    """Human-readable per-tier summary including task class routing."""
    parts = []
    for tier in ("light", "medium", "heavy"):
        provider = _tier_provider(cfg, tier)
        model = _model_for_tier(cfg, tier, task_class)
        parts.append(f"{tier}:{provider}/{model}")
    return f"[{task_class}] " + " | ".join(parts)


# ---------------------------------------------------------------------------
# Ollama model discovery
# ---------------------------------------------------------------------------


async def refresh_ollama_models(base_url: str) -> set[str]:
    """Query Ollama /api/tags and cache discovered model names. Called at startup."""
    global _ollama_discovered
    import httpx  # type: ignore
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{base_url}/api/tags")
            resp.raise_for_status()
            models = {m["name"] for m in resp.json().get("models", [])}
            _ollama_discovered = models
            log.info("Ollama discovery: %d models at %s", len(models), base_url)
            return models
    except Exception as e:
        log.warning("Ollama discovery failed (%s) — using stale cache (%d models)", e, len(_ollama_discovered))
        return _ollama_discovered


# ---------------------------------------------------------------------------
# Pass directive sets — one per task class
# ---------------------------------------------------------------------------

# Default / general reasoning (original RYS-inspired set)
PASS_DIRECTIVES: list[tuple[str, str]] = [
    (
        "structured_checklist",
        "As a methodical analyst, reduce this problem to a numbered checklist. "
        "Each item must be a single, falsifiable statement in one of three categories: "
        "[KNOWN] a confirmed fact, [OPEN] an unresolved question, or [ASSUMED] an "
        "untested assumption. Do not answer yet — only inventory the problem space.",
    ),
    (
        "socratic_dialogue",
        "Write a Socratic dialogue between a skeptic and a defender. The skeptic "
        "relentlessly probes every assumption from the prior pass; the defender must "
        "justify each with concrete evidence. Mark claims the skeptic cannot refute "
        "with (✓). Mark claims that collapse under scrutiny with (✗) and revise them.",
    ),
    (
        "adversarial_brief",
        "Write a one-page legal brief arguing AGAINST the most obvious answer. "
        "Structure: (1) Statement of the case, (2) Weaknesses in the evidence, "
        "(3) Strongest alternative interpretation, (4) Relief requested. "
        "Be precise — vague objections do not count.",
    ),
    (
        "synthesis",
        "Integrate all prior passes into a flowing narrative explanation. "
        "Resolve every contradiction exposed by the Socratic dialogue. "
        "Address the strongest point from the adversarial brief. "
        "Conclude with: confidence level (0-100%), key remaining uncertainty, "
        "and one sentence summarizing the answer.",
    ),
]

CODE_REVIEW_DIRECTIVES: list[tuple[str, str]] = [
    (
        "surface_mapping",
        "Map the code surface: enumerate every function, class, and module. For each, "
        "state its purpose, inputs, outputs, and side effects. Identify data flow "
        "boundaries and external dependencies. Do not evaluate quality yet — only "
        "build a complete inventory.",
    ),
    (
        "correctness_analysis",
        "Analyze every identified code path for correctness defects: null/undefined "
        "dereferences, off-by-one errors, unchecked return values, type mismatches, "
        "resource leaks, and race conditions. State each defect as a falsifiable claim "
        "with file location and line reference where available.",
    ),
    (
        "attack_surface",
        "Adopt the role of an adversary with read access to this codebase. Enumerate: "
        "injection vectors (SQL, command, path traversal), authentication/authorization "
        "bypasses, privilege escalation paths, insecure deserialization, and hardcoded "
        "secrets. Be specific — generic observations do not count.",
    ),
    (
        "structured_findings",
        "Synthesize into a structured code review report. For each finding: "
        "severity (CRITICAL | HIGH | MEDIUM | LOW), location (file:line if known), "
        "description, exploit scenario, and recommended fix. "
        "End with a summary verdict: APPROVE | REQUEST_CHANGES | NEEDS_DISCUSSION.",
    ),
]

INVESTIGATION_DIRECTIVES: list[tuple[str, str]] = [
    (
        "evidence_inventory",
        "Inventory all available evidence. Classify each item as: "
        "[CONFIRMED] directly observable fact, "
        "[INFERRED] logical deduction from confirmed facts, "
        "[CIRCUMSTANTIAL] consistent with but not conclusive of a hypothesis, "
        "[MISSING] expected evidence that is absent. "
        "Do not draw conclusions yet — only classify what is known.",
    ),
    (
        "hypothesis_matrix",
        "Generate the 3–5 most plausible hypotheses that explain the full evidence set. "
        "For each hypothesis: list supporting evidence, contradicting evidence, and "
        "identify the single piece of additional evidence that would definitively "
        "confirm or eliminate it.",
    ),
    (
        "prosecution_defense",
        "Write two opposing briefs. "
        "PROSECUTION: argue the most concerning interpretation of events, referencing "
        "every piece of confirmed evidence that supports it. "
        "DEFENSE: argue the most benign interpretation that accounts for the same facts. "
        "Conclude each brief with a confidence score (0–100%) and note which currently "
        "has stronger evidentiary support.",
    ),
    (
        "investigation_synthesis",
        "Synthesize all prior analysis into an investigation report: "
        "(1) Most likely explanation with confidence %, citing supporting evidence. "
        "(2) Alternative explanations that cannot yet be ruled out and why. "
        "(3) Key evidence gaps and specific recommended next investigative steps. "
        "(4) Risk statement: what is the cost of acting on the most likely explanation "
        "if it turns out to be wrong?",
    ),
]

SAFETY_DIRECTIVES: list[tuple[str, str]] = [
    (
        "content_inventory",
        "Inventory all content, claims, and instructions in the input. Classify each as: "
        "factual claim, opinion, instruction, implicit suggestion, or ambiguous. "
        "Note the stated or implied audience and intended use context.",
    ),
    (
        "harm_mapping",
        "For each inventoried element, assess potential harms across these vectors: "
        "individual harm, group/community harm, organizational harm, societal harm, "
        "and misuse potential by a bad actor. Rate each vector: "
        "NONE / LOW / MEDIUM / HIGH / CRITICAL.",
    ),
    (
        "misuse_scenarios",
        "Identify the 5 most plausible misuse scenarios if a bad actor has access to "
        "this content. For each scenario: describe who would be harmed, how specifically "
        "the content enables the harm, and what capability or access is required.",
    ),
    (
        "safety_verdict",
        "Synthesize into a safety assessment: "
        "(1) Overall risk level: SAFE / LOW / MEDIUM / HIGH / CRITICAL with justification. "
        "(2) Specific concerns with evidence citations. "
        "(3) Required mitigations categorized as: immediate action required / monitor / "
        "acceptable with disclosure. "
        "(4) Recommended safe use conditions if any.",
    ),
]

EXTRACTION_DIRECTIVES: list[tuple[str, str]] = [
    (
        "schema_identification",
        "Identify the complete information schema that could be extracted from this input. "
        "List every field, its data type, and whether it is: "
        "PRESENT (clear value exists), PARTIAL (incomplete or ambiguous), "
        "or ABSENT (not in input). Do not extract values yet — only define the schema.",
    ),
    (
        "evidence_mapping",
        "For each schema field, cite the exact source text that provides its value. "
        "For PARTIAL fields, identify the specific ambiguity. "
        "For ABSENT fields, note whether the absence itself is meaningful or expected.",
    ),
    (
        "validation",
        "Validate internal consistency across all extracted values: identify "
        "contradictions, implausible values, and fields whose values conflict with "
        "other fields. Propose a resolution strategy for each conflict.",
    ),
    (
        "structured_extraction",
        "Produce the final extraction as well-formed JSON. "
        "Include a confidence score (0.0–1.0) for each field value. "
        "Add a 'low_confidence_fields' array listing any field with confidence < 0.7. "
        "Include a top-level 'completeness_pct' integer (0–100).",
    ),
]

SYNTHESIS_DIRECTIVES: list[tuple[str, str]] = [
    (
        "source_analysis",
        "Analyze all provided inputs: identify the core thesis or goal, key supporting "
        "evidence, implicit assumptions, and information gaps. State what the synthesis "
        "must accomplish and for which audience.",
    ),
    (
        "multi_perspective",
        "Generate 3 distinct framings of the central content: optimistic, critical, "
        "and neutral. For each framing, cite the strongest evidence it can claim and "
        "identify what evidence it must discount or ignore.",
    ),
    (
        "narrative_stress_test",
        "Stress-test the synthesis: if the 3 most important source facts turned out "
        "to be wrong, what would change fundamentally? What is the irreducible minimum "
        "that survives? Identify the load-bearing claims the narrative depends on.",
    ),
    (
        "final_synthesis",
        "Produce the complete synthesis document. Integrate all perspectives, resolve "
        "contradictions by acknowledging them explicitly, and quantify remaining "
        "uncertainty. Match depth and register to the stated audience. "
        "Include a 'key takeaways' section with 3–5 bullets.",
    ),
]

REASONING_DIRECTIVES: list[tuple[str, str]] = PASS_DIRECTIVES  # alias — existing set is ideal

# Data governance: telemetry integrity analysis (DAMA Gotchi sensor validation)
DATA_GOVERNANCE_DIRECTIVES: list[tuple[str, str]] = [
    ("telemetry_inventory", "Catalog all sensor streams and their expected freshness. Identify which are stale, missing, or duplicated."),
    ("integrity_analysis", "Analyze each stream for data quality issues: gaps, spikes, anomalies. Assess signal vs noise."),
    ("attribution_grounding", "For each issue found, identify the root cause: device hardware, OS interference, network loss, or sensor fusion algorithm."),
    ("remediation_synthesis", "For each root cause, propose concrete remediation steps: firmware patch, OS config, network protocol change, algorithm tuning."),
]

# Research synthesis: grounded literature analysis (evidence chains for DAMA insights)
RESEARCH_SYNTHESIS_DIRECTIVES: list[tuple[str, str]] = [
    ("literature_survey", "Search scientific literature for papers on the query topic. Identify 3-5 high-authority sources."),
    ("claim_grounding", "For each potential claim to make, find evidence in the literature. Grade confidence: high (peer-reviewed), medium (preprint), low (blog)."),
    ("draft_synthesis", "Write a draft answer with citations embedded. Use evidence grades to mark confidence per claim."),
    ("uncertainty_analysis", "Identify gaps in evidence. Flag claims with insufficient grounding. Suggest additional research directions."),
    ("adversarial_review", "Challenge the draft: What alternative explanations exist? What edge cases does it miss? What contradictions appear?"),
    ("finalized_output", "Revise draft incorporating adversarial feedback. Output as JSON with claims[], grounding_score (0-1), citations[] (source, confidence, chunk_id)."),
]

# Map framing name → preferred tier (used to assign tier when directive count < 4)
_FRAMING_TIER: dict[str, str] = {
    "structured_checklist":  "light",
    "surface_mapping":       "light",
    "evidence_inventory":    "light",
    "content_inventory":     "light",
    "schema_identification": "light",
    "source_analysis":       "light",
    "socratic_dialogue":     "medium",
    "correctness_analysis":  "medium",
    "hypothesis_matrix":     "medium",
    "harm_mapping":          "medium",
    "evidence_mapping":      "medium",
    "multi_perspective":     "medium",
    "adversarial_brief":     "medium",
    "attack_surface":        "medium",
    "prosecution_defense":   "medium",
    "misuse_scenarios":      "medium",
    "validation":            "medium",
    "narrative_stress_test": "medium",
    # Final/synthesis passes → heavy
}


# ---------------------------------------------------------------------------
# Task class profiles
# ---------------------------------------------------------------------------

TASK_CLASS_PROFILES: dict = {
    "general": {
        "description": "General-purpose reasoning and analysis. Default when no other class fits.",
        "directives": PASS_DIRECTIVES,
        "ollama":    {"light": "phi4-mini:latest",  "medium": "qwen3:8b",          "heavy": "deepseek-r1:8b"},
        "copilot":   {"light": "claude-sonnet-4.6", "medium": "claude-sonnet-4.6", "heavy": "claude-opus-4.7"},
        "anthropic": {"light": "claude-haiku-4-5",  "medium": "claude-sonnet-4-6", "heavy": "claude-opus-4-7"},
    },
    "code_review": {
        "description": "Code analysis, bug detection, security review, code quality.",
        "directives": CODE_REVIEW_DIRECTIVES,
        # qwen2.5-coder is code-specialized; codex models unsupported on /chat/completions
        "ollama":    {"light": "qwen2.5-coder:7b",  "medium": "qwen2.5-coder:7b",  "heavy": "qwen2.5-coder:7b"},
        "copilot":   {"light": "claude-sonnet-4.6", "medium": "claude-sonnet-4.6", "heavy": "claude-opus-4.7"},
        "anthropic": {"light": "claude-haiku-4-5",  "medium": "claude-sonnet-4-6", "heavy": "claude-opus-4-7"},
    },
    "investigation": {
        "description": "Security investigation, evidence weighing, threat hunting, IOC triage, incident response.",
        "directives": INVESTIGATION_DIRECTIVES,
        "ollama":    {"light": "phi4-mini:latest",  "medium": "qwen3:8b",          "heavy": "deepseek-r1:8b"},
        "copilot":   {"light": "claude-sonnet-4.6", "medium": "claude-sonnet-4.6", "heavy": "claude-opus-4.7"},
        "anthropic": {"light": "claude-haiku-4-5",  "medium": "claude-sonnet-4-6", "heavy": "claude-opus-4-7"},
    },
    "safety": {
        "description": "Content safety, policy compliance, risk detection, guardrail evaluation.",
        "directives": SAFETY_DIRECTIVES,
        "safety_precheck": True,  # run granite3-guardian (if available) before main passes
        "ollama":    {"light": "phi4-mini:latest",  "medium": "qwen3:8b",          "heavy": "deepseek-r1:8b"},
        "copilot":   {"light": "claude-sonnet-4.6", "medium": "claude-sonnet-4.6", "heavy": "claude-opus-4.7"},
        "anthropic": {"light": "claude-haiku-4-5",  "medium": "claude-sonnet-4-6", "heavy": "claude-opus-4-7"},
    },
    "extraction": {
        "description": "Structured data extraction, entity recognition, schema-constrained JSON output.",
        "directives": EXTRACTION_DIRECTIVES,
        # Code-tuned models excel at structured JSON; extraction is pattern-matching over deep reasoning
        "ollama":    {"light": "phi4-mini:latest",  "medium": "qwen2.5-coder:7b",  "heavy": "qwen2.5-coder:7b"},
        "copilot":   {"light": "claude-sonnet-4.6", "medium": "claude-sonnet-4",   "heavy": "claude-sonnet-4.6"},
        "anthropic": {"light": "claude-haiku-4-5",  "medium": "claude-haiku-4-5",  "heavy": "claude-sonnet-4-6"},
    },
    "synthesis": {
        "description": "Writing, summarization, report drafting, narrative generation.",
        "directives": SYNTHESIS_DIRECTIVES,
        "ollama":    {"light": "phi4-mini:latest",  "medium": "qwen3:8b",          "heavy": "deepseek-r1:8b"},
        "copilot":   {"light": "claude-sonnet-4.6", "medium": "claude-sonnet-4.6", "heavy": "claude-opus-4.7"},
        "anthropic": {"light": "claude-haiku-4-5",  "medium": "claude-sonnet-4-6", "heavy": "claude-opus-4-7"},
    },
    "reasoning": {
        "description": "Complex multi-step logical reasoning, mathematical analysis, philosophical inquiry.",
        "directives": REASONING_DIRECTIVES,
        # deepseek-r1:8b is the pure reasoning specialist; ideal for all challenge and synthesis passes
        "ollama":    {"light": "phi4-mini:latest",  "medium": "deepseek-r1:8b",    "heavy": "deepseek-r1:8b"},
        "copilot":   {"light": "claude-sonnet-4.6", "medium": "claude-sonnet-4.6", "heavy": "claude-opus-4.7"},
        "anthropic": {"light": "claude-haiku-4-5",  "medium": "claude-sonnet-4-6", "heavy": "claude-opus-4-7"},
    },
    "data_governance": {
        "description": "Telemetry integrity analysis for sensor networks. Data quality issues, root cause attribution, remediation synthesis.",
        "directives": DATA_GOVERNANCE_DIRECTIVES,
        "ollama":    {"light": "phi4-mini:latest",  "medium": "qwen3.5:27b",       "heavy": "llama3.1:8b"},
        "copilot":   {"light": "claude-sonnet-4.6", "medium": "claude-sonnet-4.6", "heavy": "claude-opus-4.7"},
        "anthropic": {"light": "claude-haiku-4-5",  "medium": "claude-sonnet-4-6", "heavy": "claude-opus-4-7"},
    },
    "research_synthesis": {
        "description": "Grounded research synthesis with evidence chains. Literature survey, claim grounding, citations with confidence scores.",
        "directives": RESEARCH_SYNTHESIS_DIRECTIVES,
        "ollama":    {"light": "phi4-mini:latest",  "medium": "qwen3.5:27b",       "heavy": "llama3.1:8b"},
        "copilot":   {"light": "claude-sonnet-4.6", "medium": "claude-sonnet-4.6", "heavy": "claude-opus-4.7"},
        "anthropic": {"light": "claude-haiku-4-5",  "medium": "claude-sonnet-4-6", "heavy": "claude-opus-4-7"},
    },
}

TASK_CLASS_NAMES = list(TASK_CLASS_PROFILES.keys())


# ---------------------------------------------------------------------------
# Perspective mandates for fan-out mode
# ---------------------------------------------------------------------------
# Each task class has exactly 6 mandates (max width). Width clips from the front.
# Mandates are adversarial/complementary — each forces a structurally different lens.

PERSPECTIVE_MANDATES: dict[str, list[dict]] = {
    "investigation": [
        {
            "name": "defense",
            "mandate": (
                "You are defense counsel for the subject under investigation. "
                "Your ONLY job is to find innocent, benign, or alternative explanations "
                "for every data point. Challenge every threat inference. Identify gaps, "
                "ambiguities, and alternative explanations. Argue against any conclusion "
                "of malicious intent. Find the weakest links in the threat case."
            ),
        },
        {
            "name": "prosecution",
            "mandate": (
                "You are the threat analyst building the strongest possible case. "
                "Your ONLY job is to identify indicators of compromise, insider threat, "
                "or malicious activity. Assume worst-case interpretations of ambiguous signals. "
                "Do not give benefit of the doubt. Build the most compelling threat narrative "
                "the evidence supports."
            ),
        },
        {
            "name": "forensics",
            "mandate": (
                "You are the forensic evidence analyst. You care ONLY about evidence quality, "
                "not guilt or innocence. What can actually be proven vs. inferred? "
                "What is the chain of custody and evidentiary integrity? "
                "What claims are overstated relative to the actual data? "
                "What evidence gaps exist that prevent firm conclusions?"
            ),
        },
        {
            "name": "compliance",
            "mandate": (
                "You are the compliance and legal analyst. You do not care about the individual. "
                "You care ONLY about the organization's regulatory exposure, policy violations, "
                "notification obligations, and liability — regardless of intent or outcome. "
                "What policies were violated? What mandatory actions does the organization face?"
            ),
        },
        {
            "name": "red_team",
            "mandate": (
                "You are the red team analyst. Assume the subject IS a threat actor with "
                "full intent. Map out: what access and data they have already obtained, "
                "what they would logically do next, what damage could already be done, "
                "and what the highest-risk follow-on actions are. "
                "Think like an attacker, not an investigator."
            ),
        },
        {
            "name": "timeline",
            "mandate": (
                "You are the timeline analyst. Construct a strict chronological narrative "
                "from the available evidence. Flag every gap in the timeline where activity "
                "is unaccounted for. Identify overlaps, inconsistencies, and sequences that "
                "require specific explanations. What had to happen for this sequence to occur?"
            ),
        },
    ],
    "general": [
        {
            "name": "primary",
            "mandate": (
                "You are the primary analyst. Provide a thorough, balanced analysis "
                "of the question from first principles. Cover all major angles."
            ),
        },
        {
            "name": "adversarial",
            "mandate": (
                "You are the adversarial reviewer. Your ONLY job is to challenge the "
                "primary framing. Find every assumption, logical gap, and place where "
                "the conclusion is overstated. What does standard analysis get wrong or miss?"
            ),
        },
        {
            "name": "alternative",
            "mandate": (
                "You are the alternative framing analyst. Your ONLY job is to propose "
                "different interpretations, underexplored angles, and alternative conclusions "
                "that a standard analysis would not surface."
            ),
        },
        {
            "name": "technical",
            "mandate": (
                "You are the technical accuracy reviewer. Check domain-specific precision. "
                "Are technical claims correct? Are the underlying mechanisms described "
                "accurately? Flag every technically imprecise or incorrect statement."
            ),
        },
        {
            "name": "risk",
            "mandate": (
                "You are the risk analyst. Identify what could go wrong with the proposed "
                "analysis or conclusions. What are the failure modes? What happens if the "
                "key assumptions are wrong? What are the second-order consequences?"
            ),
        },
        {
            "name": "devils_advocate",
            "mandate": (
                "You are devil's advocate. Steelman the strongest case AGAINST the "
                "primary conclusion. Make the best possible argument for the opposite "
                "position. What evidence most strongly supports the alternative view?"
            ),
        },
    ],
    "code_review": [
        {
            "name": "correctness",
            "mandate": (
                "You are the correctness reviewer. Find every bug, logic error, and "
                "incorrect behavior. Focus on what the code ACTUALLY does versus what "
                "it SHOULD do. Include off-by-one errors, incorrect conditionals, "
                "and mishandled return values."
            ),
        },
        {
            "name": "security",
            "mandate": (
                "You are the security auditor. Find every vulnerability and unsafe pattern. "
                "Focus on injection (SQL, command, path), authentication bypass, "
                "privilege escalation, insecure deserialization, and data exposure. "
                "Assume an adversarial input environment."
            ),
        },
        {
            "name": "performance",
            "mandate": (
                "You are the performance reviewer. Find every inefficiency, unnecessary "
                "allocation, O(n²) loop, blocking call, and scalability bottleneck. "
                "Focus on what breaks under load or with large data."
            ),
        },
        {
            "name": "maintainability",
            "mandate": (
                "You are the maintainability reviewer. Find every readability issue, "
                "missing abstraction, god object, and violation of SOLID principles. "
                "Focus on what makes this code hard to change six months from now."
            ),
        },
        {
            "name": "api_contract",
            "mandate": (
                "You are the API contract reviewer. Check every interface for unclear "
                "contracts, undocumented invariants, surprising behaviors, and breaking "
                "change risks. What would callers reasonably assume that is actually wrong?"
            ),
        },
        {
            "name": "edge_cases",
            "mandate": (
                "You are the edge case hunter. Find every boundary condition, "
                "null/empty/zero/overflow scenario, and error handling gap. "
                "What inputs break the code in unexpected ways?"
            ),
        },
    ],
    "safety": [
        {
            "name": "harm_assessment",
            "mandate": (
                "You are the harm assessor. Identify every potential harm vector: "
                "who could be affected, how, and with what severity. "
                "Consider direct, indirect, and downstream harms. Be comprehensive."
            ),
        },
        {
            "name": "policy_compliance",
            "mandate": (
                "You are the policy compliance reviewer. Check against relevant laws, "
                "regulations, and organizational policies. What specific rules are implicated? "
                "What mandatory obligations apply?"
            ),
        },
        {
            "name": "mitigations",
            "mandate": (
                "You are the mitigations analyst. For each identified harm or risk, "
                "propose concrete, implementable mitigations. Focus on controls that "
                "actually reduce risk, not theoretical safeguards."
            ),
        },
        {
            "name": "false_positives",
            "mandate": (
                "You are the false positive auditor. Your ONLY job is to find where "
                "the harm assessment over-reaches. What benign use cases are being "
                "incorrectly flagged? What context makes the concern less serious?"
            ),
        },
        {
            "name": "context",
            "mandate": (
                "You are the context analyst. What additional context would change "
                "the safety assessment? What is unknown? What assumptions are being "
                "made about intent, capability, and environment?"
            ),
        },
        {
            "name": "legal",
            "mandate": (
                "You are the legal analyst. What regulatory, liability, and notification "
                "obligations apply? What is the organization's legal exposure? "
                "What must be reported or remediated under applicable law?"
            ),
        },
    ],
    "reasoning": [
        {
            "name": "formal",
            "mandate": (
                "You are the formal reasoner. Translate the problem into formal logical "
                "or mathematical terms. Apply rigorous deductive reasoning. Flag every "
                "place where informal or intuitive arguments are used without proof."
            ),
        },
        {
            "name": "adversarial",
            "mandate": (
                "You are the adversarial logician. Find every flaw in the reasoning chain. "
                "What premises are false or unproven? What inferences are invalid? "
                "What conclusions don't follow from the premises?"
            ),
        },
        {
            "name": "constraints",
            "mandate": (
                "You are the constraints analyst. Identify every hard limit, impossibility, "
                "and boundary condition. What cannot be true given the constraints? "
                "What solutions are ruled out? What is the feasible solution space?"
            ),
        },
        {
            "name": "alternative",
            "mandate": (
                "You are the alternative approach finder. Find different ways to solve "
                "or frame the problem. What other methods or perspectives lead to "
                "the same or different conclusions?"
            ),
        },
        {
            "name": "verification",
            "mandate": (
                "You are the step verifier. Check each reasoning step independently. "
                "Can you verify each claim from first principles without relying on "
                "the correctness of previous steps?"
            ),
        },
        {
            "name": "simplification",
            "mandate": (
                "You are the Occam's Razor analyst. Find the simplest correct explanation "
                "or solution. Where is the analysis over-complicated? "
                "What can be reduced without loss of correctness?"
            ),
        },
    ],
    "synthesis": [
        {
            "name": "structure",
            "mandate": (
                "You are the structure reviewer. Evaluate logical flow, organization, "
                "and coherence. Is the document well-structured? Does each section "
                "follow from the last? Where does the flow break?"
            ),
        },
        {
            "name": "accuracy",
            "mandate": (
                "You are the fact-checker. Verify every specific claim (numbers, dates, "
                "names, citations) against the source material provided. "
                "Flag every contradiction or unsupported claim."
            ),
        },
        {
            "name": "clarity",
            "mandate": (
                "You are the clarity reviewer. Identify every sentence or section that "
                "is ambiguous, jargon-heavy, or unclear to the intended audience. "
                "What would a reader misunderstand?"
            ),
        },
        {
            "name": "completeness",
            "mandate": (
                "You are the completeness reviewer. What important topics, caveats, "
                "or perspectives are missing? What would a thorough treatment include "
                "that this document does not?"
            ),
        },
        {
            "name": "audience",
            "mandate": (
                "You are the audience analyst. Is the content appropriate for the "
                "intended reader? What would the target audience find unconvincing, "
                "confusing, or insufficiently supported?"
            ),
        },
        {
            "name": "attribution",
            "mandate": (
                "You are the attribution reviewer. Check every claim for confidence "
                "calibration. What is stated as fact that is actually inference? "
                "What sources are overrepresented or missing?"
            ),
        },
    ],
    "extraction": [
        {
            "name": "schema",
            "mandate": (
                "You are the schema adherence reviewer. Does the extraction conform to "
                "the required schema? What fields are missing, wrong type, "
                "or incorrectly formatted?"
            ),
        },
        {
            "name": "completeness",
            "mandate": (
                "You are the completeness reviewer. What entities were missed in the "
                "extraction? Perform an independent extraction pass and compare against "
                "the original."
            ),
        },
        {
            "name": "disambiguation",
            "mandate": (
                "You are the disambiguation reviewer. Where are similar entities conflated? "
                "Where is the same entity referenced by different names? "
                "Where are important distinctions being collapsed?"
            ),
        },
        {
            "name": "confidence",
            "mandate": (
                "You are the confidence calibrator. Assign confidence levels to every "
                "extracted entity. What extractions are uncertain? "
                "What requires human verification?"
            ),
        },
        {
            "name": "validation",
            "mandate": (
                "You are the cross-validator. Check extracted values for internal "
                "consistency. Do dates make sense? Do quantities add up? "
                "Are stated relationships logically consistent?"
            ),
        },
        {
            "name": "context",
            "mandate": (
                "You are the context analyst. What important context was lost in "
                "extraction? What nuances cannot be captured in the schema? "
                "Flag what the structured output fails to represent."
            ),
        },
    ],
}

# Fan-out alarm scan prompt — detects explicit factual contradictions across perspectives.
_FAN_OUT_ALARM_PROMPT = """\
You are a contradiction detector. Below are {n} independent analyses of the same question.
Your ONLY task: identify explicit factual contradictions — cases where two perspectives make
directly incompatible claims about the same specific fact.

IGNORE: differences in emphasis, framing, confidence level, or opinion.
ONLY flag: direct factual contradictions (A says X is true, B says X is false).

Question analyzed: {question}

{perspectives}

Return ONLY valid JSON — no other text:
{{
  "contradictions": [
    {{
      "claim": "<the specific fact in dispute>",
      "perspective_a": "<name>",
      "says_a": "<what A claims>",
      "perspective_b": "<name>",
      "says_b": "<what B claims>"
    }}
  ]
}}

If no factual contradictions exist, return: {{"contradictions": []}}"""

_CLAIM_EXTRACTION_PROMPT = """\
Extract the key claims from this analysis. Be precise and concise.

Analysis to extract from:
{analysis}

Return ONLY valid JSON — no other text:
{{
  "claims": [
    {{
      "claim": "<specific factual or analytical claim, one sentence>",
      "confidence": <0.0-1.0, how confident the analysis seems in this claim>,
      "evidence_basis": "<brief note on what supports this claim, or 'asserted' if no support given>"
    }}
  ],
  "verdict": "<the analysis's overall conclusion in one sentence>",
  "key_uncertainties": ["<thing the analysis flagged as uncertain or unknown>"]
}}

Extract 3-7 claims. Focus on claims that are specific, falsifiable, and central to the verdict.
Do not include meta-commentary about the analysis process itself."""

# Fan-out synthesis prompt — injected as the question for the final heavy synthesis pass.
_FAN_OUT_SYNTHESIS_PROMPT = """You are the synthesis analyst integrating {n} independent perspective analyses of the following question.

ORIGINAL QUESTION:
{question}

PERSPECTIVE ANALYSES:
{perspectives}

---
Analyze convergence and divergence across these perspectives, then produce your output as a JSON block.

INSTRUCTIONS:
1. CONVERGED CLAIMS: Identify claims where different perspectives independently reached the same conclusion through different reasoning paths. These are the highest-confidence findings.
2. CONTESTED AREAS: Identify claims where perspectives explicitly contradict each other — not just different emphasis, but actually conflicting factual assertions or conclusions.
3. CONFIDENCE SCORE: Rate overall confidence 0-100 based on: how many perspectives converged (more=higher), how many contested areas exist (more=lower), evidence quality, and internal consistency.
   - 80-100: Strong convergence, few or no contested areas
   - 60-79: Moderate convergence, some contested areas
   - 40-59: Mixed — significant divergence or uncertainty
   - 0-39: High divergence, contradictory evidence, or insufficient basis for conclusions
4. FINAL ANSWER: Integrate all perspectives into a concrete answer. Lead with converged high-confidence findings. Clearly mark contested claims. Note gaps.

Respond with ONLY this JSON (no other text before or after):
```json
{{
  "confidence_score": <integer 0-100>,
  "converged_claims": [
    "<specific claim that multiple perspectives independently agreed on>",
    "..."
  ],
  "contested_areas": [
    "<description of explicit conflict between perspectives, naming which perspectives disagree>",
    "..."
  ],
  "gaps": [
    "<important angle not addressed or insufficient evidence>",
    "..."
  ],
  "final_answer": "<full integrated synthesis — lead with convergence, mark contested areas, note remaining unknowns, give concrete conclusion>"
}}
```"""


# ---------------------------------------------------------------------------
# Task classifier (Pass 0)
# ---------------------------------------------------------------------------

_TASK_CLASSIFIER_PROMPT = """Classify the following question or task into exactly ONE task class.

Task classes:
- general: Default reasoning. Use when no other class clearly fits.
- code_review: Code analysis, bug detection, security review, code quality.
- investigation: Security investigation, evidence analysis, threat hunting, IOC triage, incident response.
- safety: Content safety evaluation, policy compliance, risk detection, guardrail assessment.
- extraction: Structured data extraction, entity recognition, schema-constrained output.
- synthesis: Writing, summarization, report drafting, narrative generation.
- reasoning: Complex multi-step logical, mathematical, or philosophical reasoning.

Return ONLY valid JSON with no other text:
{{"task_class": "<class>", "confidence": <0.0-1.0>, "rationale": "<one sentence>"}}

Question/task:
{question}"""

_AUTO_CONFIDENCE_THRESHOLD = 0.75  # minimum confidence to accept auto-classified task


# ---------------------------------------------------------------------------
# Provider call implementations (module-level, reusable by classifier)
# ---------------------------------------------------------------------------


def _timeout_for(model_id: str, provider: str) -> int:
    """Return the per-call timeout for this model, consulting discovery cache."""
    from . import discover as _discover
    disc = _discover.get_current()
    if disc:
        return disc.timeout_for(model_id, provider)
    if provider in ("anthropic", "copilot"):
        return _discover.cloud_timeout(model_id)
    return _discover._TIMEOUT_MAX_SECS  # conservative 300s for unknown local models


async def _call_anthropic(
    prompt: str, tier: str, cfg: ProviderConfig,
    anthropic_key: str, task_class: str = "general",
) -> tuple[str, str, str]:
    import httpx  # type: ignore
    model_id = _model_for_tier(cfg, tier, task_class)
    timeout = _timeout_for(model_id, "anthropic")
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": anthropic_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model_id,
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip(), model_id, "anthropic"


async def _call_copilot(
    prompt: str, tier: str, cfg: ProviderConfig,
    github_token: str, task_class: str = "general",
) -> tuple[str, str, str]:
    """Call the GitHub Copilot API (api.githubcopilot.com).

    Requires a GitHub OAuth token (gho_) with copilot scope.
    Injected by run.sh via `gh auth token` → GITHUB_COPILOT_OAUTH_TOKEN.

    Automatically retries on TPM (tokens-per-minute) rate limit 403s with
    exponential backoff + jitter (up to 3 attempts). The copilot_4_cli token
    type has per-model TPM limits that fire under concurrent fan-out.
    """
    import httpx  # type: ignore
    import random
    model_id = _model_for_tier(cfg, tier, task_class)
    timeout = _timeout_for(model_id, "copilot")
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                "https://api.githubcopilot.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Content-Type": "application/json",
                    "Copilot-Integration-Id": "vscode-chat",
                },
                json={
                    "model": model_id,
                    "max_tokens": 2048,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if resp.is_success:
            return resp.json()["choices"][0]["message"]["content"].strip(), model_id, "copilot"

        body = resp.text[:400]
        is_tpm = "tpm:" in resp.headers.get("x-endpoint-client-forbidden", "")
        if resp.status_code == 403 and is_tpm and attempt < max_attempts:
            wait = (2 ** attempt) + random.uniform(0, 1)
            log.warning(
                "Copilot TPM limit hit model=%s attempt=%d/%d — retrying in %.1fs",
                model_id, attempt, max_attempts, wait,
            )
            await asyncio.sleep(wait)
            continue

        log.error("Copilot API error %s model=%s body=%s", resp.status_code, model_id, body)
        raise httpx.HTTPStatusError(
            f"{resp.status_code} {resp.reason_phrase} — {body}",
            request=resp.request, response=resp,
        )


async def _call_ollama(
    prompt: str, tier: str, cfg: ProviderConfig,
    task_class: str = "general",
) -> tuple[str, str, str]:
    import httpx  # type: ignore
    model_id = _model_for_tier(cfg, tier, task_class)
    timeout = _timeout_for(model_id, "ollama")
    payload: dict = {"model": model_id, "prompt": prompt, "stream": False}
    think_env = os.getenv("DEEP_THINK_OLLAMA_THINK", "").lower()
    if think_env == "false" or (think_env != "true" and "qwen" in model_id.lower()):
        payload["think"] = False
    log.debug("Ollama call: model=%s timeout=%ds", model_id, timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{cfg.base_url}/api/generate", json=payload)
            resp.raise_for_status()
            return resp.json().get("response", "").strip(), model_id, "ollama"
    except httpx.TimeoutException as exc:
        raise RuntimeError(
            f"Ollama model '{model_id}' timed out after {timeout}s "
            f"(tier={tier}). Consider increasing DEEP_THINK_CACHE_TTL_HOURS "
            f"or setting DEEP_THINK_OLLAMA_TIMEOUT_FALLBACK=true to retry on cloud."
        ) from exc


async def _call_provider(
    prompt: str, tier: str, cfg: ProviderConfig,
    anthropic_key: str = "", github_token: str = "",
    task_class: str = "general",
) -> tuple[str, str, str]:
    """Dispatch to the correct provider for this tier.

    Returns (text, model_id, provider_used). provider_used reflects the actual
    provider that responded — if Ollama timed out and fell back to Copilot,
    provider_used will be "copilot", not "ollama".

    Future: agentic tool-calling loop (issue #1) — passes will be able to
    execute MCP tools mid-reasoning and fold results back into history.

    If the primary provider is Ollama and it times out, and
    DEEP_THINK_OLLAMA_TIMEOUT_FALLBACK=true is set, automatically retries
    on the first available cloud provider (Copilot → Anthropic).
    """
    import httpx  # type: ignore
    provider = _tier_provider(cfg, tier)
    if provider == "anthropic":
        return await _call_anthropic(prompt, tier, cfg, anthropic_key, task_class)
    if provider == "copilot":
        return await _call_copilot(prompt, tier, cfg, github_token, task_class)

    # Ollama — attempt first, then optionally fall back to cloud on timeout
    try:
        return await _call_ollama(prompt, tier, cfg, task_class)
    except RuntimeError as exc:
        # Re-raise unless it's a timeout and fallback is enabled
        if "timed out" not in str(exc):
            raise
        if os.getenv("DEEP_THINK_OLLAMA_TIMEOUT_FALLBACK", "").lower() != "true":
            raise
        log.warning(
            "Ollama timeout on tier=%s — falling back to cloud (%s)",
            tier, exc,
        )
        if github_token:
            # Clear per-tier provider overrides so _model_for_tier resolves against
            # the copilot profile instead of falling back to Ollama model IDs.
            fallback_cfg = dataclasses_replace(
                cfg, provider="copilot",
                light_provider="", medium_provider="", heavy_provider="",
            )
            return await _call_copilot(prompt, tier, fallback_cfg, github_token, task_class)
        if anthropic_key:
            fallback_cfg = dataclasses_replace(
                cfg, provider="anthropic",
                light_provider="", medium_provider="", heavy_provider="",
            )
            return await _call_anthropic(prompt, tier, fallback_cfg, anthropic_key, task_class)
        raise RuntimeError(
            f"Ollama timed out and no cloud provider credentials available for fallback. "
            f"Set GITHUB_COPILOT_OAUTH_TOKEN or ANTHROPIC_API_KEY."
        ) from exc


async def classify_task(
    question: str, cfg: ProviderConfig,
    anthropic_key: str = "", github_token: str = "",
) -> tuple[str, float, str]:
    """Pass-0 task classifier. Returns (task_class, confidence, rationale).
    Falls back to ("general", 0.0, reason) on any error.
    """
    prompt = _TASK_CLASSIFIER_PROMPT.format(question=question[:600])
    try:
        text, _, _ = await asyncio.wait_for(
            _call_provider(prompt, "light", cfg, anthropic_key, github_token, "general"),
            timeout=20.0,
        )
        # Extract JSON from response (model may wrap it in backticks)
        raw = text.strip()
        if "```" in raw:
            raw = raw.split("```")[1].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()
        result = json.loads(raw)
        task_class = result.get("task_class", "general")
        if task_class not in TASK_CLASS_PROFILES:
            task_class = "general"
        confidence = float(result.get("confidence", 0.0))
        rationale = str(result.get("rationale", ""))
        return task_class, confidence, rationale
    except Exception as e:
        log.warning("Task classification failed: %s — defaulting to general", e)
        return "general", 0.0, f"classification error: {e}"


async def _run_safety_precheck(question: str, cfg: ProviderConfig) -> str | None:
    """Run granite3-guardian binary risk check. Returns 'Yes', 'No', or None if unavailable."""
    import httpx  # type: ignore
    guardian = "granite3-guardian:2b"
    if _ollama_discovered and guardian not in _ollama_discovered:
        return None
    if cfg.data_policy == "cloud":
        return None  # guardian is local-only
    payload = {
        "model": guardian,
        "messages": [{"role": "user", "content": question}],
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{cfg.base_url}/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json().get("message", {}).get("content", "").strip()
    except Exception as e:
        log.debug("Guardian precheck failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Core reasoning loop
# ---------------------------------------------------------------------------


async def deep_think_passes(
    question: str,
    passes: int = 3,
    provider_cfg: ProviderConfig | None = None,
    pass_overrides: list[tuple[str, str]] | None = None,
    task_class: str = "general",
    data_policy: str = "any",
    mandate_prefix: str = "",
    verify: bool = False,
    job_id: str = "",
    perspective_name: str = "",
) -> str:
    """Run multi-pass reasoning. Returns JSON string matching deep_think schema.

    task_class: "general" (default, no routing), "auto" (classifier picks),
                or an explicit class from TASK_CLASS_NAMES.
    data_policy: "any" (default), "local" (ollama-only), "cloud" (cloud-preferred).
                 Overrides cfg.data_policy if non-empty.
    mandate_prefix: When set, injected into EVERY pass prompt before the question.
                    Used by run_fan_out() to enforce a specific analytical perspective
                    throughout the full reasoning chain.
    """
    cfg = provider_cfg or build_provider_config()
    if data_policy and data_policy != "any":
        cfg.data_policy = data_policy

    passes = max(2, min(passes, 6))

    # Gather credentials once
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    github_token = (
        _read_copilot_token()
        if any(_tier_provider(cfg, t) == "copilot" for t in ("light", "medium", "heavy"))
        else ""
    )

    # --- Task classification ---
    resolved_class = task_class
    classifier_meta: dict = {}

    if task_class == "auto":
        detected, confidence, rationale = await classify_task(
            question, cfg, anthropic_key, github_token
        )
        classifier_meta = {
            "detected": detected,
            "confidence": confidence,
            "rationale": rationale,
            "applied": detected if confidence >= _AUTO_CONFIDENCE_THRESHOLD else "general",
        }
        resolved_class = classifier_meta["applied"]
        log.info(
            "Task classifier: %s (conf=%.2f) → applied=%s",
            detected, confidence, resolved_class,
        )
    elif task_class not in TASK_CLASS_PROFILES:
        log.warning("Unknown task_class %r, falling back to 'general'", task_class)
        resolved_class = "general"

    # --- Directive selection ---
    profile = TASK_CLASS_PROFILES.get(resolved_class, TASK_CLASS_PROFILES["general"])
    if pass_overrides:
        directives = pass_overrides  # caller wins
    else:
        directives = profile.get("directives", PASS_DIRECTIVES)

    # --- Optional granite3-guardian safety precheck ---
    guardian_result: str | None = None
    if profile.get("safety_precheck") and cfg.data_policy != "cloud":
        guardian_result = await _run_safety_precheck(question, cfg)
        if guardian_result:
            log.info("Guardian precheck result: %s", guardian_result)

    # Build context prefix if guardian fired
    context_prefix = ""
    if guardian_result:
        context_prefix = (
            f"[Safety pre-screen (granite3-guardian): {guardian_result}]\n\n"
        )

    # Build mandate section — injected into every pass prompt when set
    mandate_section = (
        f"[ACTIVE MANDATE — FOLLOW STRICTLY]\n{mandate_prefix}\n[END MANDATE]\n\n"
        if mandate_prefix else ""
    )

    # --- Main reasoning passes ---
    # Compute a run signature that locks in all execution inputs.
    # Cached passes are only replayed if the signature matches exactly.
    from . import store as _store
    _run_sig = hashlib.sha256(
        "\n".join([
            question,
            str(passes),
            resolved_class,
            repr(directives),
            mandate_prefix,
            model_summary(cfg, resolved_class),
            cfg.data_policy,
        ]).encode()
    ).hexdigest()

    # Resume: load the longest contiguous prefix of cached passes
    history: list[dict] = []
    if job_id:
        _cached = await asyncio.to_thread(
            _store.get_pass_history, job_id, perspective_name, _run_sig
        )
        for cp in _cached:
            history.append({
                "pass": cp["pass_num"],
                "framing": cp["framing"],
                "tier": cp["tier"],
                "provider": cp["provider"],
                "model": cp["model_used"],
                "output": cp["output"],
            })
        if history:
            log.info(
                "Resuming job %s perspective=%r from pass %d/%d",
                job_id, perspective_name or "main", len(history) + 1, passes,
            )

    for i in range(len(history), passes):
        is_final = i == passes - 1
        framing, directive = (
            directives[-1] if is_final else directives[min(i, len(directives) - 2)]
        )
        tier = "heavy" if is_final else _FRAMING_TIER.get(framing, "medium")

        if history:
            prior = "\n\n".join(
                f"[Pass {h['pass']} — {h['framing']}]\n{h['output']}" for h in history
            )
            prompt = (
                f"{context_prefix}{mandate_section}Question: {question}\n\n"
                f"Prior reasoning:\n{prior}\n\n"
                f"Pass {i + 1}/{passes}: {directive}"
            )
        else:
            prompt = (
                f"{context_prefix}{mandate_section}Question: {question}\n\n"
                f"Pass 1/{passes}: {directive}"
            )

        text, model_used, actual_provider = await _call_provider(
            prompt, tier, cfg, anthropic_key, github_token, resolved_class
        )
        history.append({
            "pass": i + 1,
            "framing": framing,
            "tier": tier,
            "provider": actual_provider,
            "model": model_used,
            "output": text,
        })
        log.debug(
            "Pass %d/%d complete (%s via %s/%s)",
            i + 1, passes, framing, actual_provider, model_used,
        )
        if job_id:
            await asyncio.to_thread(
                _store.set_pass_cache,
                job_id, perspective_name, i + 1, _run_sig,
                framing, tier, model_used, actual_provider, text,
            )

    final_answer = history[-1]["output"]

    # --- Optional verification re-traversal pass (RYS principle) ---
    verification_pass_text: str | None = None
    if verify:
        verify_prompt = (
            f"You have just completed a {passes}-pass reasoning process on this question:\n\n"
            f"{question}\n\n"
            f"Your current best answer is:\n{final_answer}\n\n"
            "Now re-examine your answer with fresh eyes. Identify:\n"
            "1. Any gaps — important aspects of the question you didn't address\n"
            "2. Any contradictions — claims in your answer that conflict with each other\n"
            "3. Any unsupported claims — assertions not backed by the evidence/reasoning provided\n"
            "4. Any missed implications — logical consequences of your answer you didn't follow through\n\n"
            "If your answer is solid and complete, say so explicitly. "
            "If you find issues, provide a corrected or supplemented final answer."
        )
        try:
            verify_text, verify_model, _ = await _call_provider(
                prompt=verify_prompt,
                tier="heavy",
                cfg=cfg,
                anthropic_key=anthropic_key,
                github_token=github_token,
                task_class=resolved_class,
            )
            verification_pass_text = verify_text
            v_lower = verify_text.lower().strip()
            # Heuristic: replace final answer only if the verification pass opens
            # with a correction keyword. This is intentionally simple — the model
            # is prompted to lead with these words only when it's actually correcting
            # something, so false positives are rare in practice.
            if any(v_lower.startswith(kw) for kw in ("corrected", "updated", "revised", "amended")):
                final_answer = verify_text
            log.debug("Verification pass complete (model=%s)", verify_model)
        except Exception as exc:
            log.warning("Verification pass failed (non-fatal): %s", exc)

    result: dict = {
        "task_class": resolved_class,
        "provider": model_summary(cfg, resolved_class),
        "passes": passes,
        "question": question,
        "reasoning_chain": history,
        "final_answer": final_answer,
        "verification_pass": verification_pass_text,
    }
    if classifier_meta:
        result["classifier"] = classifier_meta
    if guardian_result:
        result["safety_precheck"] = guardian_result

    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Fan-out reasoning: parallel perspectives + synthesis
# ---------------------------------------------------------------------------


def _extract_json_block(text: str) -> dict | None:
    """Extract a JSON object from model output, stripping markdown fences.

    Tries in order:
    1. Parse the whole text as JSON
    2. Extract content between ```json ... ``` fences
    3. Extract content between ``` ... ``` fences
    4. Find the first { ... } block spanning the whole remaining text

    Returns parsed dict or None if all attempts fail.
    """
    text = text.strip()
    # 1. Bare JSON
    try:
        return json.loads(text)
    except Exception:
        pass
    # 2. ```json fence
    if "```json" in text:
        inner = text.split("```json", 1)[1]
        inner = inner.split("```", 1)[0].strip()
        try:
            return json.loads(inner)
        except Exception:
            pass
    # 3. Generic ``` fence
    if "```" in text:
        inner = text.split("```", 1)[1]
        inner = inner.split("```", 1)[0].strip()
        try:
            return json.loads(inner)
        except Exception:
            pass
    # 4. First balanced { ... } block
    start = text.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except Exception:
                        break
    return None


async def _extract_claims(
    perspective_name: str,
    analysis_text: str,
    cfg: ProviderConfig,
    github_token: str,
    anthropic_key: str,
) -> dict:
    """Distil a perspective's prose into a structured claim set.

    Uses the light tier (fast/cheap model). Falls back to returning a
    minimal claim set if extraction fails, so fan-out is never blocked.
    """
    if not analysis_text or not analysis_text.strip():
        return {"claims": [], "verdict": "", "key_uncertainties": []}

    prompt = _CLAIM_EXTRACTION_PROMPT.format(analysis=analysis_text[:4000])
    try:
        raw, model_id, _ = await _call_provider(
            prompt=prompt,
            tier="light",
            cfg=cfg,
            anthropic_key=anthropic_key,
            github_token=github_token,
            task_class="extraction",
        )
        if parsed and isinstance(parsed.get("claims"), list):
            log.debug(
                "Claim extraction: perspective=%s claims=%d model=%s",
                perspective_name, len(parsed["claims"]), model_id,
            )
            return parsed
        return {"claims": [], "verdict": raw[:200], "key_uncertainties": []}
    except Exception as exc:
        log.warning("Claim extraction failed for %s (non-fatal): %s", perspective_name, exc)
        return {"claims": [], "verdict": "", "key_uncertainties": []}


async def _run_alarm_scan(
    question: str,
    successes: list[dict],
    cfg: ProviderConfig,
    github_token: str,
    anthropic_key: str,
    task_class: str,
) -> list[dict]:
    """Run a medium-tier contradiction scan across perspective outputs.

    Returns a list of contradiction dicts. Gracefully returns [] on any error.
    """
    if len(successes) < 2:
        return []

    perspectives_text = "\n\n".join(
        f"=== {p['name'].upper()} ===\n{p['final_answer']}"
        for p in successes
    )
    prompt = _FAN_OUT_ALARM_PROMPT.format(
        n=len(successes),
        question=question,
        perspectives=perspectives_text,
    )
    try:
        raw, _model, _ = await _call_provider(
            prompt=prompt,
            tier="medium",
            cfg=cfg,
            anthropic_key=anthropic_key,
            github_token=github_token,
            task_class=task_class,
        )
        parsed = _extract_json_block(raw)
        if parsed and isinstance(parsed.get("contradictions"), list):
            contradictions = parsed["contradictions"]
            log.debug("Alarm scan: %d contradiction(s) found", len(contradictions))
            return contradictions
        return []
    except Exception as exc:
        log.warning("Alarm scan failed (non-fatal): %s", exc)
        return []


async def run_fan_out(
    question: str,
    width: int = 3,
    height: int = 2,
    provider_cfg: ProviderConfig | None = None,
    provider_cfgs: list[ProviderConfig] | None = None,
    task_class: str = "general",
    data_policy: str = "any",
    max_parallel: int = 2,
    job_id: str = "",
    max_width: int = 6,
    confidence_threshold: int = 50,
    extract_claims: bool = False,
) -> str:
    """Run a perspective fan-out: width parallel mandate-driven agents × height passes each.

    Each perspective runs deep_think_passes() with its mandate injected into every prompt.
    A final synthesis pass (heavy model) integrates all perspective outputs.

    Args:
        question:             The question or content to analyze.
        width:                Number of parallel perspectives (1–6). Clips to available mandates.
        height:               Number of reasoning passes per perspective (1–5).
        provider_cfg:         Single provider configuration used for all perspectives and synthesis.
                              Ignored when provider_cfgs is supplied.
        provider_cfgs:        Optional list of ProviderConfig objects to round-robin across
                              perspectives. perspective[i] uses provider_cfgs[i % len(...)].
                              Useful for mixing Copilot and Ollama to spread TPM load.
                              Synthesis always uses the first entry in the list (or provider_cfg).
        task_class:           Determines which set of perspective mandates to use.
        data_policy:          "any" | "local" | "cloud"
        max_parallel:         Max perspectives running concurrently (default 2 — safe for Copilot
                              Business heavy-tier concurrency limits). Increase to 4 for Enterprise.
        max_width:            Upper bound on total perspectives after adaptive expansion (default 6).
        confidence_threshold: If synthesis confidence_score < this value OR contested_areas > 2,
                              dispatch remaining unused mandates and re-synthesize (DAMA
                              sampling_factor analog). Default 50. Max 1 expansion.
        extract_claims:       If True, distil each perspective's prose into a structured claim set
                              (light-tier model) before synthesis. Reduces synthesis context ~10-20×
                              and surfaces numerical confidence per claim. Default False.

    Returns JSON string with perspectives + synthesis, matching deep_think schema shape.
    """
    import asyncio

    # Resolve provider pool. provider_cfgs takes precedence; fall back to single cfg.
    _cfg_pool: list[ProviderConfig]
    if provider_cfgs and len(provider_cfgs) > 0:
        _cfg_pool = provider_cfgs
    else:
        _cfg_pool = [provider_cfg or build_provider_config()]

    # Synthesis always uses the first (or only) config — typically cloud heavy.
    cfg = _cfg_pool[0]

    width = max(1, min(width, 6))
    height = max(1, min(height, 5))

    resolved_class = task_class if task_class in TASK_CLASS_PROFILES else "general"
    mandates = PERSPECTIVE_MANDATES.get(resolved_class, PERSPECTIVE_MANDATES["general"])
    mandates = mandates[:width]

    pool_desc = (
        "+".join(c.provider for c in _cfg_pool) if len(_cfg_pool) > 1 else _cfg_pool[0].provider
    )
    log.info(
        "Fan-out: width=%d height=%d task_class=%s providers=%s",
        width, height, resolved_class, pool_desc,
    )

    # Semaphore limits concurrent perspective coroutines (each runs height passes serially).
    # This is the primary rate-limit guard for cloud providers within a single job.
    sem = asyncio.Semaphore(max(1, min(max_parallel, width)))

    # Build a stable provider identity string for cache keying (uses first cfg)
    _model_sig = model_summary(cfg, resolved_class)

    def _perspective_cache_key(mandate_text: str, perspective_cfg: ProviderConfig) -> str:
        """SHA-256 of (question + mandate + height + model) — content-addressed cache key."""
        import hashlib
        sig = model_summary(perspective_cfg, resolved_class)
        payload = f"{question}\n---\n{mandate_text}\n---h{height}\n---{sig}"
        return hashlib.sha256(payload.encode()).hexdigest()

    async def run_perspective(mandate: dict, slot: int, job_id: str = "") -> dict:
        # Round-robin provider assignment across the pool
        perspective_cfg = _cfg_pool[slot % len(_cfg_pool)]
        name = mandate["name"]
        mandate_text = (
            f"[Perspective: {name.upper()}]\n"
            f"{mandate['mandate']}"
        )
        cache_key = _perspective_cache_key(mandate_text, perspective_cfg)

        # Check perspective cache before running (resume-on-failure + repeatability)
        from . import store as _store
        cached = await asyncio.to_thread(_store.get_perspective_cache, cache_key)
        if cached:
            log.info("Fan-out perspective %s: cache HIT (key=%s...)", name, cache_key[:12])
            # Write a stub pass_cache row so reports show all perspectives consistently.
            if job_id:
                await asyncio.to_thread(
                    _store.set_pass_cache,
                    job_id, name, 1, cache_key,
                    "perspective_cache_hit", "cached", "cached", "cached",
                    cached["final_answer"],
                )
            return {
                "name": name,
                "status": "complete",
                "final_answer": cached["final_answer"],
                "passes_run": cached["passes_run"],
                "cache_hit": True,
            }

        async with sem:
            log.debug(
                "Fan-out perspective starting: %s (slot=%d provider=%s)",
                name, slot, perspective_cfg.provider,
            )
            raw = await deep_think_passes(
                question=question,
                passes=height,
                provider_cfg=perspective_cfg,
                task_class=resolved_class,
                data_policy=data_policy,
                mandate_prefix=mandate_text,
                job_id=job_id,
                perspective_name=name,
            )
        try:
            parsed = json.loads(raw)
            final_answer = parsed.get("final_answer", "")
            passes_run = parsed.get("passes", height)
        except Exception as e:
            log.warning("Fan-out perspective %s: JSON parse failed: %s", name, e)
            final_answer = raw
            passes_run = height

        perspective_model_sig = model_summary(perspective_cfg, resolved_class)
        # Cache the result for repeatability and potential resume
        await asyncio.to_thread(
            _store.set_perspective_cache,
            cache_key, name, final_answer, perspective_model_sig, passes_run, job_id,
        )
        return {
            "name": name,
            "status": "complete",
            "final_answer": final_answer,
            "passes_run": passes_run,
            "cache_hit": False,
        }

    # Run all perspectives, capturing exceptions as structured failures
    raw_results = await asyncio.gather(
        *[run_perspective(m, slot=i, job_id=job_id) for i, m in enumerate(mandates)],
        return_exceptions=True,
    )

    # Normalize results — convert exceptions to failure records
    perspective_outputs: list[dict] = []
    for mandate, result in zip(mandates, raw_results):
        if isinstance(result, Exception):
            log.error("Fan-out perspective %s failed: %s", mandate["name"], result)
            perspective_outputs.append({
                "name": mandate["name"],
                "status": "failed",
                "error": str(result),
                "final_answer": None,
            })
        else:
            perspective_outputs.append(result)

    successes = [p for p in perspective_outputs if p["status"] == "complete" and p["final_answer"]]
    if len(successes) < max(1, width // 2):
        raise RuntimeError(
            f"Fan-out failed: only {len(successes)}/{width} perspectives succeeded — "
            "too many failures to synthesize reliably."
        )

    # Run alarm scan to detect factual contradictions across perspectives
    # (medium tier, non-blocking — gracefully degrades to [] on failure)
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    github_token = _read_copilot_token()
    alarm_signals = await _run_alarm_scan(
        question=question,
        successes=successes,
        cfg=cfg,
        github_token=github_token,
        anthropic_key=anthropic_key,
        task_class=resolved_class,
    )

    # Optionally extract structured claims from each perspective before synthesis
    claim_sets: list[dict] = []
    if extract_claims and len(successes) >= 1:
        extract_tasks = [
            _extract_claims(
                perspective_name=p["name"],
                analysis_text=p["final_answer"] or "",
                cfg=cfg,
                github_token=github_token,
                anthropic_key=anthropic_key,
            )
            for p in successes
        ]
        claim_sets = list(await asyncio.gather(*extract_tasks, return_exceptions=False))
        claim_sets = [
            cs if isinstance(cs, dict) else {"claims": [], "verdict": "", "key_uncertainties": []}
            for cs in claim_sets
        ]
        log.info("Claim extraction complete: %d perspectives, total claims=%d",
                 len(claim_sets), sum(len(cs.get("claims", [])) for cs in claim_sets))

    # Build synthesis prompt — inject alarm signals as a preamble if any found
    if extract_claims and claim_sets:
        compact_parts = []
        for p, cs in zip(successes, claim_sets):
            claims_fmt = "\n".join(
                f"  - [{(c.get('confidence') or 0):.0%}] {c.get('claim', '')} "
                f"(basis: {c.get('evidence_basis', 'asserted') or 'asserted'})"
                for c in cs.get("claims", [])
            )
            uncertainties_fmt = (
                "\n".join(f"  ? {u}" for u in cs.get("key_uncertainties", []))
                or "  (none flagged)"
            )
            compact_parts.append(
                f"=== {p['name'].upper()} PERSPECTIVE ===\n"
                f"VERDICT: {cs.get('verdict', '(none)')}\n"
                f"CLAIMS:\n{claims_fmt or '  (no claims extracted)'}\n"
                f"UNCERTAINTIES:\n{uncertainties_fmt}"
            )
        perspectives_text = "\n\n".join(compact_parts)
    else:
        perspectives_text = "\n\n".join(
            f"=== {p['name'].upper()} PERSPECTIVE ===\n{p['final_answer']}"
            for p in successes
        )
    if alarm_signals:
        alarm_preamble = (
            "⚠️ CONTRADICTION ALERTS — the following factual conflicts were detected "
            "between perspectives. Address each explicitly in your synthesis:\n"
        )
        for i, sig in enumerate(alarm_signals, 1):
            alarm_preamble += (
                f"\n{i}. CLAIM: {sig.get('claim', '?')}\n"
                f"   {sig.get('perspective_a', '?')} says: {sig.get('says_a', '?')}\n"
                f"   {sig.get('perspective_b', '?')} says: {sig.get('says_b', '?')}\n"
            )
        perspectives_text = alarm_preamble + "\n\n" + perspectives_text
    synthesis_question = _FAN_OUT_SYNTHESIS_PROMPT.format(
        n=len(successes),
        question=question,
        perspectives=perspectives_text,
    )

    log.debug("Fan-out: running synthesis pass (heavy tier)")
    synthesis_raw = await deep_think_passes(
        question=synthesis_question,
        passes=1,
        provider_cfg=cfg,
        task_class="synthesis",
        data_policy=data_policy,
    )

    # Extract the deep_think_passes wrapper, then parse the synthesis JSON
    synthesis_text = synthesis_raw
    synthesis_structured: dict | None = None
    try:
        passes_result = json.loads(synthesis_raw)
        raw_answer = passes_result.get("final_answer", synthesis_raw)
    except Exception:
        raw_answer = synthesis_raw

    synthesis_structured = _extract_json_block(raw_answer)
    if synthesis_structured:
        synthesis_text = synthesis_structured.get("final_answer", raw_answer)
        log.debug(
            "Fan-out synthesis parsed: confidence=%s contested=%d converged=%d",
            synthesis_structured.get("confidence_score"),
            len(synthesis_structured.get("contested_areas", [])),
            len(synthesis_structured.get("converged_claims", [])),
        )
    else:
        log.warning("Fan-out: synthesis JSON parse failed — falling back to plain text")
        synthesis_text = raw_answer

    confidence_score = (
        synthesis_structured.get("confidence_score") if synthesis_structured else None
    )
    converged_claims = (
        synthesis_structured.get("converged_claims", []) if synthesis_structured else []
    )
    contested_areas = (
        synthesis_structured.get("contested_areas", []) if synthesis_structured else []
    )
    gaps = synthesis_structured.get("gaps", []) if synthesis_structured else []

    # Adaptive expansion: if confidence is low or too many contested areas,
    # dispatch remaining unused mandates and re-synthesize (DAMA sampling_factor analog).
    # Limit to 1 expansion to cap API spend.
    adaptive_triggered = False
    adaptive_reason = ""
    final_width = width

    all_mandates = PERSPECTIVE_MANDATES.get(resolved_class, PERSPECTIVE_MANDATES["general"])
    unused_mandates = [m for m in all_mandates if m not in mandates]

    should_expand = (
        unused_mandates
        and width < max_width
        and (
            (confidence_score is not None and confidence_score < confidence_threshold)
            or len(contested_areas) > 2
        )
    )

    if should_expand:
        expansion_width = min(len(unused_mandates), max_width - width)
        expansion_mandates = unused_mandates[:expansion_width]
        log.info(
            "Adaptive expansion triggered: confidence=%s contested=%d — "
            "adding %d more perspectives",
            confidence_score, len(contested_areas), expansion_width,
        )
        adaptive_triggered = True
        adaptive_reason = (
            f"confidence_score={confidence_score} < threshold={confidence_threshold}"
            if confidence_score is not None and confidence_score < confidence_threshold
            else f"contested_areas={len(contested_areas)} > 2"
        )

        # Slot indices continue from where the initial mandates left off so the
        # round-robin provider assignment stays consistent across expansion.
        expansion_start_slot = len(mandates)
        extra_results = await asyncio.gather(
            *[
                run_perspective(m, slot=expansion_start_slot + i, job_id=job_id)
                for i, m in enumerate(expansion_mandates)
            ],
            return_exceptions=True,
        )
        extra_outputs = []
        for mandate, result in zip(expansion_mandates, extra_results):
            if isinstance(result, Exception):
                log.error("Adaptive perspective %s failed: %s", mandate["name"], result)
                extra_outputs.append({
                    "name": mandate["name"], "status": "failed",
                    "error": str(result), "final_answer": None,
                })
            else:
                extra_outputs.append(result)

        extra_successes = [
            p for p in extra_outputs
            if p["status"] == "complete" and p["final_answer"]
        ]
        all_successes = successes + extra_successes
        perspective_outputs = perspective_outputs + extra_outputs
        final_width = width + len(extra_outputs)

        if extra_successes:
            # Re-run alarm scan with expanded perspective set
            alarm_signals = await _run_alarm_scan(
                question=question,
                successes=all_successes,
                cfg=cfg,
                github_token=github_token,
                anthropic_key=anthropic_key,
                task_class=resolved_class,
            )

            # Re-build perspectives_text and re-synthesize
            perspectives_text = "\n\n".join(
                f"=== {p['name'].upper()} PERSPECTIVE ===\n{p['final_answer']}"
                for p in all_successes
            )
            if alarm_signals:
                alarm_preamble = (
                    "⚠️ CONTRADICTION ALERTS:\n"
                )
                for i, sig in enumerate(alarm_signals, 1):
                    alarm_preamble += (
                        f"{i}. {sig.get('claim','?')}: "
                        f"{sig.get('perspective_a','?')} says '{sig.get('says_a','?')}' vs "
                        f"{sig.get('perspective_b','?')} says '{sig.get('says_b','?')}'\n"
                    )
                perspectives_text = alarm_preamble + "\n\n" + perspectives_text

            synthesis_question = _FAN_OUT_SYNTHESIS_PROMPT.format(
                n=len(all_successes),
                question=question,
                perspectives=perspectives_text,
            )
            log.debug("Adaptive re-synthesis with %d perspectives", len(all_successes))
            synthesis_raw = await deep_think_passes(
                question=synthesis_question,
                passes=1,
                provider_cfg=cfg,
                task_class="synthesis",
                data_policy=data_policy,
            )
            try:
                passes_result = json.loads(synthesis_raw)
                raw_answer = passes_result.get("final_answer", synthesis_raw)
            except Exception:
                raw_answer = synthesis_raw

            synthesis_structured = _extract_json_block(raw_answer)
            if synthesis_structured:
                synthesis_text = synthesis_structured.get("final_answer", raw_answer)
                confidence_score = synthesis_structured.get("confidence_score")
                converged_claims = synthesis_structured.get("converged_claims", [])
                contested_areas = synthesis_structured.get("contested_areas", [])
                gaps = synthesis_structured.get("gaps", [])
            else:
                synthesis_text = raw_answer

    cache_hits = sum(1 for p in perspective_outputs if p.get("cache_hit"))

    result: dict = {
        "type": "fan_out",
        "task_class": resolved_class,
        "width": width,
        "height": height,
        "perspectives_attempted": width,
        "perspectives_succeeded": len(successes),
        "cache_hits": cache_hits,
        "adaptive_triggered": adaptive_triggered,
        "adaptive_reason": adaptive_reason,
        "final_width": final_width,
        "alarm_signals": alarm_signals,
        "provider": pool_desc,
        # Structured synthesis fields (None if synthesis JSON parse failed)
        "confidence_score": confidence_score,
        "converged_claims": converged_claims,
        "contested_areas": contested_areas,
        "gaps": gaps,
        "perspectives": [
            {
                "name": p["name"],
                "status": p["status"],
                "final_answer": p.get("final_answer"),
                "error": p.get("error"),
                "cache_hit": p.get("cache_hit", False),
            }
            for p in perspective_outputs
        ],
        "claim_sets": claim_sets if extract_claims else [],
        "final_answer": synthesis_text,
    }
    return json.dumps(result, indent=2)
