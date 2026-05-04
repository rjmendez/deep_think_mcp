"""Deep Think MCP Engine — Modular reasoning loop with multi-pass and fan-out support.

This package provides the refactored deep_think_mcp engine, broken into focused modules:

- engine.types: Core dataclasses (ProviderConfig, PassResult, ValidationData)
- engine.directives: Framing directives and task class profiles
- engine.provider: Provider abstraction and LLM call implementations
- engine.orchestrator: Main pass loop (deep_think_passes) and fan-out (run_fan_out)
- engine.proof_chain: Proof chain tracking for grounded reasoning
- engine.task_class_enforcer: Task class enforcement (adversarial/research safeguards)

Public API:
- deep_think_passes: Main reasoning loop
- run_fan_out: Parallel perspective reasoning with synthesis
- ProviderConfig: Provider configuration
- build_provider_config: Build provider config from overrides
- refresh_ollama_models: Discover available Ollama models
- model_summary: Get human-readable model summary
- classify_task: Auto-classify task to task class
- TASK_CLASS_PROFILES: Task class routing profiles
- PERSPECTIVE_MANDATES: Mandates per task class for fan-out

Security and enforcement:
- SecurityError: Raised when security policy is violated
- TaskClassViolation: Raised when task_class + provider/tool combination is blocked
- _validate_provider_is_local: Validate provider is Ollama when local-only mode
- _check_ollama_available: Check Ollama availability at startup
- _validate_and_enforce_local_models: Enforce local-only MQTT models
"""

from .types import ProviderConfig, PassResult, ValidationData
from .orchestrator import (
    deep_think_passes,
    run_fan_out,
    _extract_claims_from_pass_output,
    _validate_claims_against_ground_truth,
    _build_claim_data,
    _extract_subject_from_statement,
    _run_alarm_scan,
)
from .provider import (
    build_provider_config,
    refresh_ollama_models,
    model_summary,
    classify_task,
    SecurityError,
    _validate_provider_is_local,
    _check_ollama_available,
    _validate_and_enforce_local_models,
)
from .directives import TASK_CLASS_PROFILES, PERSPECTIVE_MANDATES
from .creative import (
    CreativeReasoningEngine,
    CreativePassResult,
    CreativeJobResult,
    CreativeMetricsLog,
    CREATIVE_MODES,
    CREATIVE_TEMPLATES,
    get_temperature,
    get_pass_template,
    extract_quality_metrics,
    get_metrics_snapshot,
)
from .proof_chain import ProofChain, ProofEntry, UncitedClaim
from .task_class_enforcer import (
    TaskClassViolation,
    enforce_task_class,
    check_research_tool_allowed,
    get_allowed_tools,
    filter_adversarial_output,
    is_abliteration_model,
    ABLITERATION_MODEL_PATTERNS,
    RESEARCH_ENABLED_TASK_CLASSES,
    RESEARCH_BLOCKED_TASK_CLASSES,
)

__all__ = [
    # Types
    "ProviderConfig",
    "PassResult",
    "ValidationData",
    # Main functions
    "deep_think_passes",
    "run_fan_out",
    # Claim processing (internal functions for testing/advanced use)
    "_extract_claims_from_pass_output",
    "_validate_claims_against_ground_truth",
    "_build_claim_data",
    "_extract_subject_from_statement",
    "_run_alarm_scan",
    # Provider functions
    "build_provider_config",
    "refresh_ollama_models",
    "model_summary",
    "classify_task",
    # Security
    "SecurityError",
    "TaskClassViolation",
    "_validate_provider_is_local",
    "_check_ollama_available",
    "_validate_and_enforce_local_models",
    # Constants
    "TASK_CLASS_PROFILES",
    "PERSPECTIVE_MANDATES",
    # Grounded reasoning
    "ProofChain",
    "ProofEntry",
    "UncitedClaim",
    "enforce_task_class",
    "check_research_tool_allowed",
    "get_allowed_tools",
    "filter_adversarial_output",
    "is_abliteration_model",
    "ABLITERATION_MODEL_PATTERNS",
    "RESEARCH_ENABLED_TASK_CLASSES",
    "RESEARCH_BLOCKED_TASK_CLASSES",
    # Creative reasoning
    "CreativeReasoningEngine",
    "CreativePassResult",
    "CreativeJobResult",
    "CreativeMetricsLog",
    "CREATIVE_MODES",
    "CREATIVE_TEMPLATES",
    "get_temperature",
    "get_pass_template",
    "extract_quality_metrics",
    "get_metrics_snapshot",
]
