"""MQTT data models and findings structures.

This module contains shared data classes used across the MQTT integration.
"""

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List
from datetime import datetime


@dataclass
class Finding:
    """A finding extracted from deep_think reasoning results.
    
    Attributes:
        device_id: Device identifier (e.g., 'ant_001')
        claim_ids: List of claim IDs that support this finding
        anomalies: List of anomaly descriptions
        confidence: Confidence score (0.0-1.0)
        severity: Severity level ('low', 'medium', 'high', 'critical')
        timestamp: ISO 8601 timestamp when finding was created
        metadata: Additional context (contradictions, hallucinations, etc.)
    """
    device_id: str
    claim_ids: List[str]
    anomalies: List[str]
    confidence: float
    severity: str
    timestamp: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Finding":
        """Create Finding from dictionary."""
        return cls(**data)
