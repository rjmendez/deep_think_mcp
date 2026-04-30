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
     Copilot token → "copilot", fallback → "ollama"

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

import json
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Cached GitHub Copilot session token (expires ~25 min, re-fetched lazily).
_copilot_session: dict = {"token": "", "expires_at": 0.0}

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
    "light":  "claude-sonnet-4.5",
    "medium": "claude-sonnet-4.6",
    "heavy":  "claude-opus-4.7",
}
_OLLAMA_DEFAULTS = {
    "light":  "phi4-mini:latest",
    "medium": "llama3.1:8b",
    "heavy":  "qwen3.5:27b",
}


def _read_copilot_oauth_token() -> str:
    env_token = os.getenv("GITHUB_COPILOT_OAUTH_TOKEN", "").strip()
    if env_token:
        return env_token
    hosts_path = os.getenv("GH_HOSTS_YML_PATH", "").strip()
    if not hosts_path:
        xdg = os.getenv("XDG_CONFIG_HOME", "").strip()
        hosts_path = (
            os.path.join(xdg, "gh", "hosts.yml")
            if xdg
            else os.path.expanduser("~/.config/gh/hosts.yml")
        )
    try:
        import yaml  # type: ignore
        with open(hosts_path) as f:
            hosts = yaml.safe_load(f) or {}
        return hosts.get("github.com", {}).get("oauth_token", "")
    except Exception:
        return ""


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
        elif _read_copilot_oauth_token():
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
        "ollama":    {"light": "phi4-mini:latest",    "medium": "llama3.1:8b",       "heavy": "qwen3.5:27b"},
        "copilot":   {"light": "claude-sonnet-4.5",   "medium": "claude-sonnet-4.6", "heavy": "claude-opus-4.7"},
        "anthropic": {"light": "claude-haiku-4-5",    "medium": "claude-sonnet-4-6", "heavy": "claude-opus-4-7"},
    },
    "code_review": {
        "description": "Code analysis, bug detection, security review, code quality.",
        "directives": CODE_REVIEW_DIRECTIVES,
        # qwen2.5-coder is code-specialized; gpt-5.2-codex on copilot side
        "ollama":    {"light": "qwen2.5-coder:7b",    "medium": "qwen2.5-coder:7b",  "heavy": "qwen3.5:27b"},
        "copilot":   {"light": "gpt-4.1",             "medium": "gpt-5.2-codex",     "heavy": "claude-opus-4.7"},
        "anthropic": {"light": "claude-haiku-4-5",    "medium": "claude-sonnet-4-6", "heavy": "claude-opus-4-7"},
    },
    "investigation": {
        "description": "Security investigation, evidence weighing, threat hunting, IOC triage, incident response.",
        "directives": INVESTIGATION_DIRECTIVES,
        "ollama":    {"light": "phi4-mini:latest",    "medium": "llama3.1:8b",       "heavy": "qwen3.5:27b"},
        "copilot":   {"light": "claude-sonnet-4.5",   "medium": "claude-sonnet-4.6", "heavy": "claude-opus-4.7"},
        "anthropic": {"light": "claude-haiku-4-5",    "medium": "claude-sonnet-4-6", "heavy": "claude-opus-4-7"},
    },
    "safety": {
        "description": "Content safety, policy compliance, risk detection, guardrail evaluation.",
        "directives": SAFETY_DIRECTIVES,
        "safety_precheck": True,  # run granite3-guardian (if available) before main passes
        # granite3-guardian not used as reasoning tier (binary output) — pre-pass only
        "ollama":    {"light": "phi4-mini:latest",    "medium": "llama3.1:8b",       "heavy": "qwen3.5:27b"},
        "copilot":   {"light": "gpt-4.1",             "medium": "claude-sonnet-4.6", "heavy": "claude-opus-4.7"},
        "anthropic": {"light": "claude-haiku-4-5",    "medium": "claude-sonnet-4-6", "heavy": "claude-opus-4-7"},
    },
    "extraction": {
        "description": "Structured data extraction, entity recognition, schema-constrained JSON output.",
        "directives": EXTRACTION_DIRECTIVES,
        # Lighter models are fine — extraction is pattern matching, not deep reasoning
        "ollama":    {"light": "phi4-mini:latest",    "medium": "mistral:7b",        "heavy": "llama3.1:8b"},
        "copilot":   {"light": "gpt-4.1",             "medium": "gpt-4.1",           "heavy": "claude-sonnet-4.6"},
        "anthropic": {"light": "claude-haiku-4-5",    "medium": "claude-haiku-4-5",  "heavy": "claude-sonnet-4-6"},
    },
    "synthesis": {
        "description": "Writing, summarization, report drafting, narrative generation.",
        "directives": SYNTHESIS_DIRECTIVES,
        "ollama":    {"light": "phi4-mini:latest",    "medium": "llama3.1:8b",       "heavy": "qwen3.5:27b"},
        "copilot":   {"light": "claude-sonnet-4.5",   "medium": "claude-sonnet-4.6", "heavy": "claude-opus-4.7"},
        "anthropic": {"light": "claude-haiku-4-5",    "medium": "claude-sonnet-4-6", "heavy": "claude-opus-4-7"},
    },
    "reasoning": {
        "description": "Complex multi-step logical reasoning, mathematical analysis, philosophical inquiry.",
        "directives": REASONING_DIRECTIVES,
        # Bias toward larger/stronger models — reasoning benefits most from scale
        "ollama":    {"light": "phi4-mini:latest",    "medium": "qwen3.5:27b",       "heavy": "qwen3.5:27b"},
        "copilot":   {"light": "claude-sonnet-4.5",   "medium": "claude-sonnet-4.6", "heavy": "claude-opus-4.7"},
        "anthropic": {"light": "claude-haiku-4-5",    "medium": "claude-sonnet-4-6", "heavy": "claude-opus-4-7"},
    },
}

TASK_CLASS_NAMES = list(TASK_CLASS_PROFILES.keys())


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


async def _get_copilot_session_token(oauth_token: str) -> str:
    import time as _time
    import httpx  # type: ignore

    global _copilot_session
    if _copilot_session["token"] and _time.time() < _copilot_session["expires_at"] - 60:
        return _copilot_session["token"]

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.github.com/copilot_internal/v2/token",
            headers={
                "Authorization": f"token {oauth_token}",
                "editor-version": "vscode/1.85.0",
                "Copilot-Integration-Id": "vscode-chat",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    session_token = data.get("token", "")
    try:
        from datetime import datetime, timezone as _tz
        expires_at = datetime.fromisoformat(
            data["expires_at"].replace("Z", "+00:00")
        ).timestamp()
    except Exception:
        import time as _t2
        expires_at = _t2.time() + 1500

    _copilot_session = {"token": session_token, "expires_at": expires_at}
    return session_token


def _timeout_for(model_id: str, provider: str) -> int:
    """Return the per-call timeout for this model, consulting discovery cache."""
    from . import discover as _discover
    disc = _discover.get_current()
    if disc:
        return disc.timeout_for(model_id, provider)
    # Pre-discovery fallback
    if provider in ("anthropic", "copilot"):
        return _discover.cloud_timeout(model_id)
    return _discover._TIMEOUT_MAX_SECS  # conservative 300s for unknown local models


async def _call_anthropic(
    prompt: str, tier: str, cfg: ProviderConfig,
    anthropic_key: str, task_class: str = "general",
) -> tuple[str, str]:
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
        return resp.json()["content"][0]["text"].strip(), model_id


async def _call_copilot(
    prompt: str, tier: str, cfg: ProviderConfig,
    copilot_oauth: str, task_class: str = "general",
) -> tuple[str, str]:
    import httpx  # type: ignore
    model_id = _model_for_tier(cfg, tier, task_class)
    timeout = _timeout_for(model_id, "copilot")
    session_token = await _get_copilot_session_token(copilot_oauth)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            "https://api.githubcopilot.com/chat/completions",
            headers={
                "Authorization": f"Bearer {session_token}",
                "Copilot-Integration-Id": "vscode-chat",
                "editor-version": "vscode/1.85.0",
                "content-type": "application/json",
            },
            json={
                "model": model_id,
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip(), model_id


async def _call_ollama(
    prompt: str, tier: str, cfg: ProviderConfig,
    task_class: str = "general",
) -> tuple[str, str]:
    import httpx  # type: ignore
    model_id = _model_for_tier(cfg, tier, task_class)
    timeout = _timeout_for(model_id, "ollama")
    payload: dict = {"model": model_id, "prompt": prompt, "stream": False}
    think_env = os.getenv("DEEP_THINK_OLLAMA_THINK", "").lower()
    if think_env == "false" or (think_env != "true" and "qwen" in model_id.lower()):
        payload["think"] = False
    log.debug("Ollama call: model=%s timeout=%ds", model_id, timeout)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{cfg.base_url}/api/generate", json=payload)
        resp.raise_for_status()
        return resp.json().get("response", "").strip(), model_id


async def _call_provider(
    prompt: str, tier: str, cfg: ProviderConfig,
    anthropic_key: str = "", copilot_oauth: str = "",
    task_class: str = "general",
) -> tuple[str, str]:
    """Dispatch to the correct provider for this tier."""
    provider = _tier_provider(cfg, tier)
    if provider == "anthropic":
        return await _call_anthropic(prompt, tier, cfg, anthropic_key, task_class)
    if provider == "copilot":
        return await _call_copilot(prompt, tier, cfg, copilot_oauth, task_class)
    return await _call_ollama(prompt, tier, cfg, task_class)


async def classify_task(
    question: str, cfg: ProviderConfig,
    anthropic_key: str = "", copilot_oauth: str = "",
) -> tuple[str, float, str]:
    """Pass-0 task classifier. Returns (task_class, confidence, rationale).
    Falls back to ("general", 0.0, reason) on any error.
    """
    import asyncio
    prompt = _TASK_CLASSIFIER_PROMPT.format(question=question[:600])
    try:
        text, _ = await asyncio.wait_for(
            _call_provider(prompt, "light", cfg, anthropic_key, copilot_oauth, "general"),
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
) -> str:
    """Run multi-pass reasoning. Returns JSON string matching deep_think schema.

    task_class: "general" (default, no routing), "auto" (classifier picks),
                or an explicit class from TASK_CLASS_NAMES.
    data_policy: "any" (default), "local" (ollama-only), "cloud" (cloud-preferred).
                 Overrides cfg.data_policy if non-empty.
    """
    cfg = provider_cfg or build_provider_config()
    if data_policy and data_policy != "any":
        cfg.data_policy = data_policy

    passes = max(2, min(passes, 6))

    # Gather credentials once
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    copilot_oauth = (
        _read_copilot_oauth_token()
        if any(_tier_provider(cfg, t) == "copilot" for t in ("light", "medium", "heavy"))
        else ""
    )

    # --- Task classification ---
    resolved_class = task_class
    classifier_meta: dict = {}

    if task_class == "auto":
        detected, confidence, rationale = await classify_task(
            question, cfg, anthropic_key, copilot_oauth
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

    # --- Main reasoning passes ---
    history: list[dict] = []
    for i in range(passes):
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
                f"{context_prefix}Question: {question}\n\n"
                f"Prior reasoning:\n{prior}\n\n"
                f"Pass {i + 1}/{passes}: {directive}"
            )
        else:
            prompt = (
                f"{context_prefix}Question: {question}\n\n"
                f"Pass 1/{passes}: {directive}"
            )

        text, model_used = await _call_provider(
            prompt, tier, cfg, anthropic_key, copilot_oauth, resolved_class
        )
        history.append({
            "pass": i + 1,
            "framing": framing,
            "tier": tier,
            "provider": _tier_provider(cfg, tier),
            "model": model_used,
            "output": text,
        })
        log.debug(
            "Pass %d/%d complete (%s via %s/%s)",
            i + 1, passes, framing, _tier_provider(cfg, tier), model_used,
        )

    result: dict = {
        "task_class": resolved_class,
        "provider": model_summary(cfg, resolved_class),
        "passes": passes,
        "question": question,
        "reasoning_chain": history,
        "final_answer": history[-1]["output"],
    }
    if classifier_meta:
        result["classifier"] = classifier_meta
    if guardian_result:
        result["safety_precheck"] = guardian_result

    return json.dumps(result, indent=2)
