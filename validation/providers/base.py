"""Abstract ground truth provider interface."""

from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol

from ..types import Claim, ValidationResult


class AbstractGroundTruthProvider(Protocol):
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
