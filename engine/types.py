"""Type definitions and dataclasses for the deep_think engine.

Provides:
- ProviderConfig: Configuration for provider selection and model assignment
- PassResult: Result of a single reasoning pass
- ValidationData: Output from ground truth validation
"""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ProviderConfig:
    """Configuration for provider selection, model assignment, and data policy.
    
    Provider selection priority (per tier):
      1. data_policy="local"  → always "ollama" regardless of anything else
      2. {tier}_provider — per-tier call override
      3. DEEP_THINK_{TIER}_PROVIDER env var — per-tier env override
      4. provider — default provider for all tiers
      5. Auto-detected from credentials
    
    Model selection priority (per tier: light / medium / heavy):
      1. model — single override for all tiers
      2. light / medium / heavy — explicit per-tier call override
      3. DEEP_THINK_{PROVIDER}_{TIER} env var — per-tier env override
      4. Task class profile recommendation
      5. Dynamically-discovered tier assignment
      6. Built-in provider default
    """
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


@dataclass
class PassResult:
    """Result of a single reasoning pass.
    
    Attributes:
        pass_num: Pass number (1-indexed)
        framing: Name of the framing/directive used
        tier: Tier used (light / medium / heavy)
        provider: Provider used (anthropic / copilot / ollama)
        model: Model ID that produced this output
        output: The full text output from the model
        validation: Optional validation results from ground truth provider
        measured_confidence: Optional confidence score from validation
    """
    pass_num: int
    framing: str
    tier: str
    provider: str
    model: str
    output: str
    validation: Optional[dict] = None
    measured_confidence: Optional[float] = None


@dataclass
class ValidationData:
    """Output from ground truth validation of a pass's claims.
    
    Attributes:
        claims: List of Claim objects extracted from the pass output
        validation_results: List of ValidationResult objects
        hallucination_count: Number of invalid/hallucinated claims
        overall_confidence: Mean confidence across validated claims
        contradictions: List of contradictions with prior passes
        hallucination_details: Details on each hallucination found
    """
    claims: list[Any] = field(default_factory=list)
    validation_results: list[Any] = field(default_factory=list)
    hallucination_count: int = 0
    overall_confidence: float = 0.5
    contradictions: list[Any] = field(default_factory=list)
    hallucination_details: list[dict] = field(default_factory=list)
