"""MQTT data models and findings structures.

This module contains shared data classes used across the MQTT integration.
"""

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional
from datetime import datetime
from enum import Enum
import re
import hashlib
import uuid


class AnomalyType(Enum):
    """Enumeration of anomaly types detected in device telemetry."""
    STEP_DUPLICATION = "StepDuplication"
    TEMPERATURE_QUANTIZATION = "TemperatureQuantization"
    ZERO_ERROR_RATES = "ZeroErrorRates"
    STEP_CADENCE_CONTRADICTION = "StepCadenceContradiction"
    MEMORY_SATURATION = "MemorySaturation"


class ValidationError(Exception):
    """Raised when validation fails for models."""
    pass


def normalize_uuid(s: str) -> str:
    """Normalize UUID string to hex format (32 chars, no hyphens).
    
    Args:
        s: UUID string, either with or without hyphens
        
    Returns:
        Normalized UUID in hex format (32 characters, lowercase)
        
    Raises:
        ValidationError: If the string is not a valid UUID format
    """
    # Remove hyphens
    normalized = s.replace("-", "").lower()
    
    # Validate length
    if len(normalized) != 32:
        raise ValidationError(
            f"Invalid UUID: expected 32 hex characters, got {len(normalized)}"
        )
    
    # Validate hex characters
    if not re.match(r"^[0-9a-f]{32}$", normalized):
        raise ValidationError(
            f"Invalid UUID: contains non-hexadecimal characters"
        )
    
    return normalized


@dataclass
class Finding:
    """A finding extracted from deep_think reasoning results.
    
    Attributes:
        id: Normalized UUID in hex format (32 chars, no hyphens)
        device_id: Device identifier (e.g., 'ant_001')
        finding_type: Type of anomaly (AnomalyType enum)
        confidence: Confidence score (0.0-1.0)
        timestamp: ISO 8601 timestamp when finding was created
        expires_at: ISO 8601 timestamp when this finding expires (TTL)
        claim_ids: List of claim IDs that support this finding (optional, legacy)
        anomalies: List of anomaly descriptions (optional, legacy)
        severity: Severity level (optional, legacy)
        metadata: Additional context (optional, legacy)
    """
    id: str
    device_id: str
    finding_type: AnomalyType
    confidence: float
    timestamp: str
    expires_at: str
    claim_ids: List[str] = field(default_factory=list)
    anomalies: List[str] = field(default_factory=list)
    severity: str = "medium"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate finding after initialization."""
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationError(
                f"Confidence must be between 0.0 and 1.0, got {self.confidence}"
            )
        
        # Normalize UUID if it's in hyphenated format
        try:
            self.id = normalize_uuid(self.id)
        except ValidationError as e:
            raise ValidationError(f"Invalid finding ID: {e}")
        
        if not isinstance(self.finding_type, AnomalyType):
            raise ValidationError(
                f"finding_type must be an AnomalyType, got {type(self.finding_type)}"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        data["finding_type"] = self.finding_type.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Finding":
        """Create Finding from dictionary."""
        data_copy = data.copy()
        if isinstance(data_copy.get("finding_type"), str):
            data_copy["finding_type"] = AnomalyType(data_copy["finding_type"])
        return cls(**data_copy)


@dataclass
class Confirmation:
    """Feedback confirmation for a finding from a device.
    
    Attributes:
        finding_id: Normalized UUID of the finding being confirmed
        device_id: Device identifier confirming the finding
        confirmed: Whether the finding was confirmed (True) or rejected (False)
        evidence: Supporting evidence or reason for confirmation
        timestamp: ISO 8601 timestamp when confirmation was received
        confirmation_hash: Hash of (finding_id|device_id|confirmed|timestamp_bucket)
    """
    finding_id: str
    device_id: str
    confirmed: bool
    evidence: str
    timestamp: str
    confirmation_hash: Optional[str] = None

    def __post_init__(self) -> None:
        """Normalize UUID and generate confirmation hash."""
        # Normalize UUID if it's in hyphenated format
        try:
            self.finding_id = normalize_uuid(self.finding_id)
        except ValidationError as e:
            raise ValidationError(f"Invalid finding_id: {e}")
        
        # Generate confirmation hash if not provided
        if self.confirmation_hash is None:
            self.confirmation_hash = self._generate_hash()
    
    def _generate_hash(self) -> str:
        """Generate confirmation hash by bucketing timestamp to nearest minute.
        
        Returns:
            MD5 hash of concatenated (finding_id|device_id|confirmed|timestamp_bucket)
        """
        try:
            # Parse timestamp and bucket to nearest minute
            ts = datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
            timestamp_bucket = ts.replace(second=0, microsecond=0).isoformat()
        except (ValueError, AttributeError):
            # Fallback: use timestamp as-is if parsing fails
            timestamp_bucket = self.timestamp
        
        hash_input = f"{self.finding_id}|{self.device_id}|{self.confirmed}|{timestamp_bucket}"
        return hashlib.md5(hash_input.encode()).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Confirmation":
        """Create Confirmation from dictionary."""
        return cls(**data)
