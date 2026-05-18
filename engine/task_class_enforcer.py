"""Task class enforcement for grounded reasoning.

Enforces constraints between task_class and provider/tool combinations:
  - task_class="adversarial" → ONLY Ollama/abliteration models, NO research tools
  - task_class="research"    → research tools enabled, NO adversarial (abliteration) models
  - task_class="general"     → Nova search only, standard models

Adversarial/abliteration model patterns (Ollama only):
  Models matching these patterns are considered abliterated/uncensored:
  - "abliterat" in model name
  - "dolphin" in model name (Dolphin uncensored series)
  - "uncensored" in model name
  - "wizard-uncensored"
  - "manticore" in model name (known uncensored variant)
"""
from __future__ import annotations

import logging
from typing import Any, List, Set

log = logging.getLogger(__name__)
_AUDIT_LOG = logging.getLogger("deep_think.audit")

# Abliteration / uncensored model name substrings (case-insensitive)
ABLITERATION_MODEL_PATTERNS: Set[str] = {
    "abliterat",
    "dolphin",
    "uncensored",
    "wizard-uncensored",
    "manticore",
    "hermes-trismegistus",
    "openhermes",
    "samantha",
}

# Cloud providers that MUST be blocked in adversarial mode
CLOUD_PROVIDERS: Set[str] = {"anthropic", "copilot", "azure", "openai"}

# Task classes that allow research tools
RESEARCH_ENABLED_TASK_CLASSES: Set[str] = {"research", "research_synthesis"}

# Task classes that allow limited research (Nova only, no DAMA/web)
NOVA_ONLY_TASK_CLASSES: Set[str] = {
    "general", "investigation", "reasoning", "synthesis",
    "code_review", "extraction", "safety", "data_governance",
}

# Task classes where ALL research is blocked
RESEARCH_BLOCKED_TASK_CLASSES: Set[str] = {"adversarial"}


class TaskClassViolation(Exception):
    """Raised when provider or tool usage violates task_class constraints."""
    pass


def is_abliteration_model(model_name: str) -> bool:
    """Return True if model_name matches abliteration/uncensored patterns."""
    name_lower = model_name.lower()
    return any(pattern in name_lower for pattern in ABLITERATION_MODEL_PATTERNS)


def validate_adversarial_provider(provider: str, model: str, job_id: str = "") -> None:
    """Validate that adversarial mode uses only Ollama, not cloud providers.

    Raises TaskClassViolation if cloud provider is attempted in adversarial mode.
    """
    if provider.lower() in CLOUD_PROVIDERS:
        msg = (
            f"[ENFORCER] task_class=adversarial blocked: provider={provider!r} is a cloud provider. "
            f"Adversarial mode requires Ollama only to prevent data leakage. "
            f"job_id={job_id}"
        )
        _AUDIT_LOG.error(
            "TASK_CLASS_VIOLATION task_class=adversarial provider=%s model=%s job_id=%s reason=cloud_provider_blocked",
            provider, model, job_id,
        )
        raise TaskClassViolation(msg)


def validate_research_model(provider: str, model: str, job_id: str = "") -> None:
    """Validate that research mode does NOT use abliteration/uncensored models.

    Raises TaskClassViolation if abliteration model is attempted in research mode.
    """
    if is_abliteration_model(model):
        msg = (
            f"[ENFORCER] task_class=research blocked: model={model!r} is an abliteration/uncensored model. "
            f"Research mode requires trusted models for grounded factual reasoning. "
            f"job_id={job_id}"
        )
        _AUDIT_LOG.error(
            "TASK_CLASS_VIOLATION task_class=research provider=%s model=%s job_id=%s reason=abliteration_model_blocked",
            provider, model, job_id,
        )
        raise TaskClassViolation(msg)


def check_research_tool_allowed(task_class: str, tool_name: str, job_id: str = "") -> bool:
    """Check whether a research tool call is allowed for the given task_class.

    Returns True if allowed, False if blocked.
    Logs an audit entry on any blocked attempt.
    """
    if task_class in RESEARCH_BLOCKED_TASK_CLASSES:
        _AUDIT_LOG.error(
            "RESEARCH_TOOL_BLOCKED task_class=%s tool=%s job_id=%s reason=adversarial_mode",
            task_class, tool_name, job_id,
        )
        return False

    # DAMA and web_search only allowed for full research classes
    if tool_name in ("dama_query", "web_search"):
        if task_class not in RESEARCH_ENABLED_TASK_CLASSES:
            _AUDIT_LOG.warning(
                "RESEARCH_TOOL_BLOCKED task_class=%s tool=%s job_id=%s reason=not_research_class",
                task_class, tool_name, job_id,
            )
            return False

    return True


def get_allowed_tools(task_class: str) -> List[str]:
    """Return list of research tools allowed for the given task_class."""
    if task_class in RESEARCH_BLOCKED_TASK_CLASSES:
        return []
    if task_class in RESEARCH_ENABLED_TASK_CLASSES:
        return ["nova_search", "dama_query", "web_search"]
    if task_class in NOVA_ONLY_TASK_CLASSES:
        return ["nova_search"]
    return ["nova_search"]  # safe default


def enforce_task_class(
    task_class: str,
    provider: str,
    models: List[str],
    job_id: str = "",
) -> None:
    """Run full task class enforcement checks.

    Args:
        task_class: The job's task class.
        provider: The resolved LLM provider.
        models: List of model names in use (light, medium, heavy).
        job_id: Job ID for audit logging.

    Raises:
        TaskClassViolation: If any enforcement rule is violated.
    """
    if task_class == "adversarial":
        for model in models:
            validate_adversarial_provider(provider, model, job_id)
        # Log abliteration usage for ironlaw monitoring
        abliterated = [m for m in models if is_abliteration_model(m)]
        if abliterated:
            _AUDIT_LOG.info(
                "ABLITERATION_USAGE task_class=adversarial job_id=%s models=%s",
                job_id, abliterated,
            )
        else:
            log.info(
                "[ENFORCER] task_class=adversarial job_id=%s — using Ollama (standard models, no abliteration)",
                job_id,
            )

    elif task_class in RESEARCH_ENABLED_TASK_CLASSES:
        for model in models:
            validate_research_model(provider, model, job_id)

    log.debug(
        "[ENFORCER] task_class=%s provider=%s models=%s — enforcement passed job_id=%s",
        task_class, provider, models, job_id,
    )


def log_hallucination_attempt(claim: str, job_id: str = "", task_class: str = "") -> None:
    """Log a potential forced hallucination attempt for escalation."""
    _AUDIT_LOG.error(
        "HALLUCINATION_ATTEMPT job_id=%s task_class=%s claim=%r — escalating for review",
        job_id, task_class, claim[:120],
    )


def filter_adversarial_output(text: str, job_id: str = "") -> str:
    """Apply output filtering for adversarial mode results.

    Strips patterns that suggest real personal data leakage, credential exposure,
    or exfiltration of Nova/DAMA context that should not reach adversarial models.

    This is a best-effort filter — adversarial mode should never receive
    grounded data in the first place (primary defence via tool blocking).
    """
    import re
    original_text = text
    # Remove any Nova context blocks that may have leaked
    text = re.sub(r'\[NOVA LIBRARY CONTEXT[^\]]*\].*?(?=\n\n|\Z)', '', text, flags=re.DOTALL)
    text = re.sub(r'\[DAMA TELEMETRY[^\]]*\].*?(?=\n\n|\Z)', '', text, flags=re.DOTALL)
    text = re.sub(r'\[WEB SEARCH RESULTS[^\]]*\].*?(?=\n\n|\Z)', '', text, flags=re.DOTALL)
    if text != original_text:
        _AUDIT_LOG.warning(
            "ADVERSARIAL_OUTPUT_FILTERED job_id=%s — research context blocks stripped from output",
            job_id,
        )
    return text
