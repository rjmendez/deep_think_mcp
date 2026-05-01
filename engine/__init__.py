"""Deep Think MCP Engine — Modular reasoning loop with multi-pass and fan-out support.

This package provides the refactored deep_think_mcp engine, broken into focused modules:

- engine.types: Core dataclasses (ProviderConfig, PassResult, ValidationData)
- engine.directives: Framing directives and task class profiles
- engine.provider: Provider abstraction and LLM call implementations
- engine.orchestrator: Main pass loop (deep_think_passes) and fan-out (run_fan_out)

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
- _validate_provider_is_local: Validate provider is Ollama when local-only mode
- _check_ollama_available: Check Ollama availability at startup
- _validate_and_enforce_local_models: Enforce local-only MQTT models
"""

from .types import ProviderConfig, PassResult, ValidationData
from .orchestrator import deep_think_passes, run_fan_out
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

__all__ = [
    # Types
    "ProviderConfig",
    "PassResult",
    "ValidationData",
    # Main functions
    "deep_think_passes",
    "run_fan_out",
    # Provider functions
    "build_provider_config",
    "refresh_ollama_models",
    "model_summary",
    "classify_task",
    # Security
    "SecurityError",
    "_validate_provider_is_local",
    "_check_ollama_available",
    "_validate_and_enforce_local_models",
    # Constants
    "TASK_CLASS_PROFILES",
    "PERSPECTIVE_MANDATES",
]
