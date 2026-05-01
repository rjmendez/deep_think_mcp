"""Clean integration layer for deep_think_mcp.

Provides a unified public API surface that combines engine reasoning,
validation, and wiring between components.

This module is the primary entry point for:
  - Multi-pass reasoning (deep_think_passes, deep_think_fan_out)
  - Task classification and routing
  - Provider/model configuration and discovery
  - Ground truth validation

Imports are cleanly separated into:
  - .engine: reasoning engine, provider management, model selection
  - .validation: ground truth provider, claim validation, result types
"""

from typing import Any, Optional, Dict, List
import logging

# Engine module — reasoning engine and provider abstraction
from . import engine as _engine
from .engine import (
    ProviderConfig,
    build_provider_config,
    refresh_ollama_models,
    model_summary,
    deep_think_passes,
    run_fan_out,
    classify_task,
    TASK_CLASS_PROFILES,
    PERSPECTIVE_MANDATES,
)

# Validation module — ground truth and claim validation
from . import validation as _validation
from .validation import (
    Claim,
    SensorData,
    ValidationResult,
    PassValidationResult,
    ValidationMetrics,
    ClaimExtractor,
    extract_claims_from_pass_output,
    validate_claims,
    calculate_confidence_from_evidence,
    merge_validation_results,
    AbstractGroundTruthProvider,
    MQTTGroundTruthProvider,
    NovaGroundTruthProvider,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Public API Surface
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    # Engine exports
    "ProviderConfig",
    "build_provider_config",
    "refresh_ollama_models",
    "model_summary",
    "deep_think_passes",
    "run_fan_out",
    "classify_task",
    "TASK_CLASS_PROFILES",
    "PERSPECTIVE_MANDATES",
    # Validation exports
    "Claim",
    "SensorData",
    "ValidationResult",
    "PassValidationResult",
    "ValidationMetrics",
    "ClaimExtractor",
    "extract_claims_from_pass_output",
    "validate_claims",
    "calculate_confidence_from_evidence",
    "merge_validation_results",
    "AbstractGroundTruthProvider",
    "MQTTGroundTruthProvider",
    "NovaGroundTruthProvider",
    # Wiring and integration functions
    "get_engine",
    "get_validation",
]


def get_engine() -> Any:
    """Access the engine module directly for internal use.

    Returns the raw engine module for accessing internal helpers
    and functions not re-exported by core.py.

    Typical usage:
        engine = get_engine()
        cfg = engine.build_provider_config()
        tier = engine._tier_provider(cfg, "heavy")
    """
    return _engine


def get_validation() -> Any:
    """Access the validation module directly for internal use.

    Returns the raw validation module for accessing internal helpers
    and functions not re-exported by core.py.
    """
    return _validation


# ─────────────────────────────────────────────────────────────────────────────
# Integration Helpers (wiring between engine and validation)
# ─────────────────────────────────────────────────────────────────────────────


async def run_reasoning_with_validation(
    question: str,
    passes: int = 3,
    task_class: str = "general",
    data_policy: str = "any",
    provider_config: Optional[Dict[str, Any]] = None,
    verify: bool = False,
    ground_truth_provider: Optional[AbstractGroundTruthProvider] = None,
) -> Dict[str, Any]:
    """Run reasoning passes with optional ground truth validation.

    This is a high-level integration function that demonstrates how to
    wire together the engine (reasoning) and validation (ground truth) modules.

    Args:
        question: The question or problem to reason about.
        passes: Number of reasoning passes (2–6).
        task_class: Task routing hint (affects model selection and pass directives).
        data_policy: Controls provider usage ("any" | "local" | "cloud").
        provider_config: Per-call overrides for provider/model (no secrets).
        verify: If True, run RYS verification pass after reasoning.
        ground_truth_provider: Optional provider for validating claims.

    Returns:
        A dict with structure:
            {
                "final_answer": str,
                "passes": [pass_1, pass_2, ...],
                "validation": {...} if ground_truth_provider else None,
            }

    Raises:
        ValueError: If passes is out of range (2–6).
    """
    if not 2 <= passes <= 6:
        raise ValueError(f"passes must be 2–6, got {passes}")

    # Build provider configuration from overrides and environment
    cfg = build_provider_config(provider_config)

    # Run the reasoning engine (currently a pass-through to deep_think_passes)
    result = await deep_think_passes(
        question=question,
        passes=passes,
        task_class=task_class,
        cfg=cfg,
        verify=verify,
    )

    # If a ground truth provider is available, optionally validate claims
    # This is wiring demonstrating how validation integrates with reasoning.
    if ground_truth_provider is not None:
        log.debug("Ground truth provider available; validation can be applied post-reasoning")
        # Future: extract claims from result and validate them
        # claims = extract_claims_from_pass_output(result["final_answer"])
        # validation_results = await ground_truth_provider.validate_claims(claims)
        # result["validation"] = validation_results

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Re-exports with type hints and docstrings (documentation layer)
# ─────────────────────────────────────────────────────────────────────────────


def describe_providers() -> Dict[str, str]:
    """Describe available providers and their requirements.

    Returns:
        A dict mapping provider names to descriptions.
    """
    return {
        "anthropic": "Claude models via Anthropic API (requires ANTHROPIC_API_KEY)",
        "copilot": "GitHub Copilot API (requires GITHUB_COPILOT_OAUTH_TOKEN)",
        "ollama": "Local Ollama models (requires OLLAMA_BASE_URL)",
    }


def describe_task_classes() -> Dict[str, str]:
    """Describe available task routing profiles.

    Returns:
        A dict mapping task class names to descriptions.
    """
    return {
        "general": "Default reasoning (no routing).",
        "auto": "Run Pass-0 classifier; apply result only if confidence >= 0.75.",
        "code_review": "Bug detection, security review. Uses qwen2.5-coder / gpt-5.2-codex.",
        "investigation": "Evidence weighing, hypothesis testing, IOC triage.",
        "safety": "Risk detection, harm mapping. Runs granite3-guardian pre-check.",
        "extraction": "Structured JSON output, entity extraction.",
        "synthesis": "Writing, summarization, narrative generation.",
        "reasoning": "Pure logical / mathematical reasoning.",
    }
