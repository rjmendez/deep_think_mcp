"""Ground truth validation package.

Provides claim extraction, validation, and result aggregation from ground truth sources.

Modules:
- types: Data structures (Claim, ValidationResult, etc.)
- claim_extractor: Extract claims from model outputs
- validator: Validation logic and metrics calculation
- providers: Ground truth provider implementations
  - base: Abstract provider interface
  - nova_provider: Nova/Great Library implementation
  - mqtt_provider: MQTT telemetry implementation
"""

from .claim_extractor import ClaimExtractor, extract_claims_from_pass_output
from .types import (
    Claim,
    SensorData,
    ValidationMetrics,
    ValidationResult,
    PassValidationResult,
)
from .validator import (
    validate_claims,
    calculate_confidence_from_evidence,
    merge_validation_results,
)
from .providers import (
    AbstractGroundTruthProvider,
    MQTTGroundTruthProvider,
    NovaGroundTruthProvider,
)

__all__ = [
    # Types
    "Claim",
    "SensorData",
    "ValidationResult",
    "PassValidationResult",
    "ValidationMetrics",
    # Claim extraction
    "ClaimExtractor",
    "extract_claims_from_pass_output",
    # Validation
    "validate_claims",
    "calculate_confidence_from_evidence",
    "merge_validation_results",
    # Providers
    "AbstractGroundTruthProvider",
    "MQTTGroundTruthProvider",
    "NovaGroundTruthProvider",
]
