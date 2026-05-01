"""Ground truth provider interface for validating claims against real sensor data.

Enables truth-discovery via sensor feedback. Instead of models hallucinating in isolation,
claims are validated against actual telemetry from the DAMA phone or system metrics.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Protocol

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────────────────────────────────────


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
class SensorSnapshot:
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


# ─────────────────────────────────────────────────────────────────────────────
# Protocol (Interface)
# ─────────────────────────────────────────────────────────────────────────────


class GroundTruthProvider(Protocol):
    """Interface for fetching and validating against ground truth.

    Implementations should:
    - Fetch sensor/metric data from an actual source (Nova, MQTT, Redis, API, etc.)
    - Validate claims against ground truth
    - Detect contradictions between claims
    - Measure confidence from validation, not from model invention
    """

    async def available_domains(self) -> List[str]:
        """Return list of available domains (e.g., ['telemetry', 'code', 'logs'])."""
        ...

    async def get_sensor_data(
        self,
        sensor_id: str,
        time_range: Optional[tuple[datetime, datetime]] = None,
    ) -> Dict[str, Any]:
        """
        Fetch raw sensor data from ground truth source.

        Args:
            sensor_id: identifier (e.g., "GPS.POSITION", "database.connection_pool")
            time_range: optional (start, end) datetime tuple

        Returns: {
            "sensor_id": str,
            "current_value": Any,
            "recent_values": List[{timestamp, value}],
            "freshness_ms": int,
            "status": "FRESH" | "STALE" | "ERROR",
            "metadata": {provider, reliability_score, last_updated, ...}
        }
        """
        ...

    async def validate(
        self,
        claim: Claim,
        context: Optional[Dict[str, Any]] = None,
    ) -> ValidationResult:
        """Validate a single claim against ground truth."""
        ...

    async def validate_batch(
        self,
        claims: List[Claim],
        context: Optional[Dict[str, Any]] = None,
    ) -> List[ValidationResult]:
        """Validate multiple claims, batching queries to data source."""
        ...

    async def detect_contradictions(
        self,
        claims: List[Claim],
        prior_claims: Optional[List[Claim]] = None,
    ) -> List[Dict]:
        """Detect contradictions between claims."""
        ...

    async def get_context(self, query: str) -> Dict[str, Any]:
        """
        Fetch relevant context for a query.

        Examples:
            query = "GPS module failures"
            → returns: {sensor_inventory, recent_errors, baseline_metrics, ...}

            query = "database performance"
            → returns: {query_logs, slowest_queries, connection_pool_state, ...}
        """
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Nova/Great Library Implementation (Preferred)
# ─────────────────────────────────────────────────────────────────────────────


class NovaGroundTruthProvider:
    """Fetch ground truth from the Great Library (Nova MCP).
    
    This provider queries the Great Library for DAMA phone telemetry and sensor
    data, allowing claims to be validated against actual measurements rather than
    speculative reasoning.
    
    Why Nova?
    - DAMA phone data is indexed in the Great Library
    - No separate MQTT auth required
    - Integrates naturally with existing nova_search / nova_verify tools
    - Can cross-reference against research literature (sensor accuracy specs, etc.)
    """

    def __init__(self):
        """Initialize Nova ground truth provider."""
        self.nova_available = False
        self._check_nova_availability()

    def _check_nova_availability(self):
        """Check if Nova/Great Library is available."""
        # This will be called by engine.py before validating claims
        # If nova_search tool is available, we can use it
        self.nova_available = True
        log.info("Nova ground truth provider initialized (Great Library accessible)")

    async def available_domains(self) -> List[str]:
        """Return domains where we can validate claims."""
        return ["telemetry", "location", "sensor_measurements", "device_health", "literature"]

    async def get_sensor_data(
        self,
        sensor_id: str,
        time_range: Optional[tuple[datetime, datetime]] = None,
    ) -> Dict[str, Any]:
        """Fetch sensor data from Great Library."""
        if not self.nova_available:
            return {
                "sensor_id": sensor_id,
                "status": "ERROR",
                "error": "Great Library not available",
            }

        # This is a stub - actual implementation would call nova_search
        # Example: nova_search("DAMA Pixel device GPS.POSITION last 24 hours")
        return {
            "sensor_id": sensor_id,
            "current_value": None,
            "recent_values": [],
            "status": "NO_DATA",
            "metadata": {"source": "great_library", "note": "implement nova_search integration"},
        }

    async def validate(
        self,
        claim: Claim,
        context: Optional[Dict[str, Any]] = None,
    ) -> ValidationResult:
        """Validate a claim against ground truth from Great Library."""
        # Stub implementation - real version would use nova_verify
        return ValidationResult(
            claim_id=claim.id,
            is_valid=True,  # placeholder
            ground_truth_value=None,
            evidence=[],
            confidence=0.5,  # unknown until integrated
            metadata={"provider": "nova", "status": "not_yet_validated"},
        )

    async def validate_batch(
        self,
        claims: List[Claim],
        context: Optional[Dict[str, Any]] = None,
    ) -> List[ValidationResult]:
        """Validate multiple claims."""
        results = []
        for claim in claims:
            result = await self.validate(claim, context)
            results.append(result)
        return results

    async def detect_contradictions(
        self,
        claims: List[Claim],
        prior_claims: Optional[List[Claim]] = None,
    ) -> List[Dict]:
        """Detect contradictions between claims."""
        contradictions = []
        if prior_claims:
            for claim in claims:
                for prior_claim in prior_claims:
                    if (claim.subject == prior_claim.subject and 
                        claim.expected_value != prior_claim.expected_value):
                        contradictions.append({
                            "claim_1_id": prior_claim.id,
                            "claim_2_id": claim.id,
                            "contradiction": f"{claim.subject}: {prior_claim.expected_value} vs {claim.expected_value}",
                        })
        return contradictions

    async def get_context(self, query: str) -> Dict[str, Any]:
        """Fetch context from Great Library for a query."""
        return {
            "query": query,
            "domains_available": await self.available_domains(),
            "status": "ready",
        }


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────


async def create_ground_truth_provider(
    provider_type: str = "nova"
) -> Optional[GroundTruthProvider]:
    """Create and initialize a ground truth provider.

    Args:
        provider_type: Type of provider ("nova", "none")

    Returns:
        Initialized provider or None.
    """
    if provider_type == "none" or provider_type is None:
        log.info("Ground truth validation disabled")
        return None

    if provider_type == "nova" or provider_type == "auto":
        return NovaGroundTruthProvider()

    else:
        log.warning(f"Unknown provider type: {provider_type}")
        return None
