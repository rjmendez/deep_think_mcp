"""Data types for ground truth validation.

Defines claim, sensor snapshot, and validation result structures.
"""

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class Claim:
    """Atomic assertion extracted from model output."""

    id: str  # unique identifier
    statement: str  # the claim text
    claim_type: str  # "telemetry_staleness", "gps_position", "code_defect", etc.
    subject: str  # what it's about (e.g., "GPS.POSITION", "database.connection")
    expected_value: Any  # what the claim says should be true
    confidence_model: float = 0.5  # model's own confidence (extracted from output)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        return asdict(self)


@dataclass
class SensorData:
    """Snapshot of a sensor/metric at a point in time."""

    sensor_id: str
    current_value: Any
    freshness_ms: int  # milliseconds since last update
    timestamp_utc: datetime
    metadata: Dict[str, Any]  # source, reliability_score, etc.

    def is_fresh(self, threshold_ms: int = 5000) -> bool:
        """Check if data is fresh relative to threshold."""
        return self.freshness_ms <= threshold_ms

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        d = asdict(self)
        d["timestamp_utc"] = self.timestamp_utc.isoformat()
        return d


@dataclass
class ValidationResult:
    """Result of validating a claim against ground truth."""

    claim_id: str
    is_valid: bool  # True if claim matches ground truth
    ground_truth_value: Any  # actual value from ground truth
    evidence: List[Dict]  # supporting data points
    confidence: float  # measured from validation (0-1)
    contradiction_source: Optional[str] = None  # what contradicts it (if invalid)
    metadata: Dict[str, Any] = None  # source reliability, freshness, etc.

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        return asdict(self)


@dataclass
class PassValidationResult:
    """Result of validating all claims in a pass output."""

    pass_num: int
    claims_extracted: List[Claim]
    validation_results: List[ValidationResult]
    hallucination_count: int
    hallucination_details: List[Dict]  # {claim_id, type, contradiction}
    overall_confidence: float  # mean confidence of validated claims
    contradiction_with_prior: List[Dict]  # contradicts earlier passes?

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        d = asdict(self)
        d["claims_extracted"] = [c.to_dict() for c in self.claims_extracted]
        d["validation_results"] = [v.to_dict() for v in self.validation_results]
        return d


@dataclass
class ValidationMetrics:
    """Metrics from validation operations."""

    total_claims: int
    valid_claims: int
    invalid_claims: int
    hallucination_count: int
    average_confidence: float
    contradictions_found: int

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        return asdict(self)
