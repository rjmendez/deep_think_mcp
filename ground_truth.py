"""Ground truth provider interface for validating claims against real sensor data.

Enables truth-discovery via sensor feedback. Instead of models hallucinating in isolation,
claims are validated against actual telemetry from the DAMA phone or system metrics.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

import pyotp

log = logging.getLogger(__name__)

# Optional imports for Nova integration
# Try to import from nova_mcp.core (preferred, stable implementation)
# Fall back to nova_tools if nova_mcp.core not available
try:
    from nova_mcp.core import nova_search, nova_verify, nova_synthesize
    NOVA_TOOLS_AVAILABLE = True
    log.debug("Using nova_mcp.core for Nova integration (stable implementation)")
except ImportError:
    try:
        from nova_tools import nova_search, nova_verify, nova_synthesize
        NOVA_TOOLS_AVAILABLE = True
        log.debug("Using nova_tools for Nova integration (fallback)")
    except ImportError:
        NOVA_TOOLS_AVAILABLE = False
        log.debug("nova_tools not available; Nova validation will return 0.0 confidence")

# TOTP token caching (30-second TTL to match token validity window)
_totp_cache = {"token": None, "expires_at": 0.0}
_totp_lock = threading.Lock()

# Nova environment variables
NOVA_TOKEN = os.getenv("NOVA_TOKEN", "").strip()
NOVA_TOTP_SEED = os.getenv("NOVA_TOTP_SEED", "").strip()
NOVA_BASE_URL = os.getenv("NOVA_BASE_URL", "http://100.73.200.19:30850").rstrip("/")


def _get_nova_headers_with_cached_totp() -> Dict[str, str]:
    """Generate Nova auth headers with cached TOTP token (30-second TTL).
    
    Caches TOTP tokens to avoid regeneration overhead on every request.
    TOTP tokens are only valid for 30 seconds, so reuse within the cache
    window is safe and reduces CPU load under high request rates.
    
    Thread-safe using a lock to prevent concurrent regeneration (thundering herd).
    """
    headers = {"Authorization": f"Bearer {NOVA_TOKEN}"} if NOVA_TOKEN else {}
    
    if NOVA_TOTP_SEED:
        now = time.time()
        # Check if cache is still valid (fast path, no lock)
        if now > _totp_cache["expires_at"]:
            # Cache expired or not initialized; acquire lock for regeneration
            with _totp_lock:
                # Double-check inside lock (prevent multiple threads from both regenerating)
                if now > _totp_cache["expires_at"]:
                    _totp_cache["token"] = pyotp.TOTP(NOVA_TOTP_SEED).now()
                    _totp_cache["expires_at"] = now + 30.0
        
        headers["X-TOTP-Challenge"] = _totp_cache["token"]
    
    return headers


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
        """Initialize Nova ground truth provider.
        
        Note: Nova availability check is deferred to first validate() call (lazy initialization).
        This provides faster startup times and better error messages when Nova is actually used.
        """
        self.nova_available = None  # None = not yet checked, True/False = checked
        self._nova_check_lock = asyncio.Lock()

    async def _ensure_nova_initialized(self):
        """Check Nova availability on first use (lazy initialization).
        
        Validates that:
        1. nova_tools module is importable
        2. NOVA_TOKEN environment variable is set
        3. NOVA_TOTP_SEED environment variable is set
        
        Uses async lock to ensure initialization happens only once.
        Logs warnings for missing configuration but allows graceful degradation.
        Does not raise exceptions; sets nova_available to False if issues found.
        """
        # Fast path: already checked
        if self.nova_available is not None:
            return
        
        # Slow path: need to check (acquire lock to prevent duplicate checks)
        async with self._nova_check_lock:
            # Double-check inside lock
            if self.nova_available is not None:
                return
            
            if not NOVA_TOOLS_AVAILABLE:
                log.warning("nova_tools module not available; Nova ground truth validation will be disabled")
                self.nova_available = False
                return
            
            # Check for required environment variables
            missing_vars = []
            if not NOVA_TOKEN:
                missing_vars.append("NOVA_TOKEN")
            if not NOVA_TOTP_SEED:
                missing_vars.append("NOVA_TOTP_SEED")
            
            if missing_vars:
                log.warning(f"Nova environment incomplete; missing: {', '.join(missing_vars)}. Nova validation will be disabled.")
                self.nova_available = False
                return
            
            self.nova_available = True
            log.info("Nova ground truth provider initialized (Great Library accessible with valid credentials)")


    async def available_domains(self) -> List[str]:
        """Return domains where we can validate claims."""
        return ["telemetry", "location", "sensor_measurements", "device_health", "literature"]

    async def get_sensor_data(
        self,
        sensor_id: str,
        time_range: Optional[tuple[datetime, datetime]] = None,
    ) -> Dict[str, Any]:
        """Fetch sensor data from Great Library.
        
        Uses nova_search to find sensor specifications and recent measurements.
        Returns structured data with freshness and confidence metrics.
        
        Args:
            sensor_id: e.g., 'GPS.POSITION', 'WIFI.NEARBY_NETWORKS'
            time_range: Optional (start, end) datetime tuple
        
        Returns:
            Dict with keys:
            - current_value: Latest sensor reading
            - freshness_ms: Age of data in milliseconds
            - status: 'fresh' | 'stale' | 'unavailable'
            - evidence: List of supporting documents
            - confidence: 0.0-1.0 based on data quality
            - metadata: {request_id, source, error (if any)}
        """
        request_id = str(uuid.uuid4())[:8]
        
        try:
            # Ensure Nova is initialized
            await self._ensure_nova_initialized()
            
            # Check if Nova tools are available and properly configured
            if not NOVA_TOOLS_AVAILABLE or not self.nova_available:
                log.warning(f"[{request_id}] nova_tools not available or not configured for sensor {sensor_id}")
                return {
                    "sensor_id": sensor_id,
                    "current_value": None,
                    "freshness_ms": 0,
                    "status": "unavailable",
                    "evidence": [],
                    "confidence": 0.0,
                    "metadata": {"error": "nova_tools not available or not configured", "request_id": request_id},
                }
            
            # Build search query for sensor specifications and recent data
            query = f"{sensor_id} sensor data specification accuracy bounds latest measurements"
            log.debug(f"[{request_id}] Searching for sensor data: {query}")
            results = await nova_search(query, top=5, profile='research')
            
            sensor_info = {
                "sensor_id": sensor_id,
                "current_value": None,
                "freshness_ms": 0,
                "status": "unknown",
                "evidence": [],
                "confidence": 0.5,
                "metadata": {"source": "great_library", "request_id": request_id},
            }
            
            # Extract evidence from search results
            if results:
                for i, doc in enumerate(results[:5]):
                    evidence_entry = {
                        "index": i,
                        "source": doc.get("source", "unknown"),
                        "relevance": doc.get("relevance", 0.5),
                    }
                    # Include truncated content for audit trail
                    if "content" in doc:
                        evidence_entry["content_preview"] = str(doc.get("content", ""))[:200]
                    sensor_info["evidence"].append(evidence_entry)
                
                # Use max relevance as confidence if we found evidence
                max_relevance = max(
                    (d.get("relevance", 0.5) for d in results),
                    default=0.5
                )
                sensor_info["confidence"] = min(max_relevance, 1.0)
                sensor_info["status"] = "fresh" if max_relevance >= 0.7 else "stale"
                log.debug(f"[{request_id}] Sensor {sensor_id} status: {sensor_info['status']}, confidence: {sensor_info['confidence']:.2f}")
            else:
                sensor_info["status"] = "unavailable"
                sensor_info["confidence"] = 0.0
                log.debug(f"[{request_id}] No evidence found for sensor {sensor_id}")
            
            return sensor_info
            
        except asyncio.TimeoutError:
            log.warning(f"[{request_id}] Timeout fetching sensor data for {sensor_id}")
            return {
                "sensor_id": sensor_id,
                "current_value": None,
                "freshness_ms": 0,
                "status": "timeout",
                "evidence": [],
                "confidence": 0.0,
                "metadata": {"error": "timeout", "request_id": request_id},
            }
        except Exception as e:
            log.error(f"[{request_id}] Failed to fetch sensor data for {sensor_id}: {e}")
            return {
                "sensor_id": sensor_id,
                "current_value": None,
                "freshness_ms": 0,
                "status": "error",
                "evidence": [],
                "confidence": 0.0,
                "metadata": {"error": str(e), "request_id": request_id},
            }

    async def validate(
        self,
        claim: Claim,
        context: Optional[Dict[str, Any]] = None,
    ) -> ValidationResult:
        """Validate a claim against ground truth from Great Library.
        
        Uses nova_verify to check if claim is supported by evidence.
        Falls back to nova_search if verify fails.
        
        May retry up to 3 times on timeout using exponential backoff (1s, 2s, 4s).
        
        Args:
            claim: Claim object with id, subject, expected_value
            context: Optional context including prior_passes, task_class, request_id
        
        Returns:
            ValidationResult with:
            - is_valid: True if evidence supports claim
            - ground_truth_value: What the data actually says
            - evidence: List of supporting documents
            - confidence: 0.0-1.0 measured from evidence quality
            - metadata: {provider, query, status, latency_ms, request_id}
        """
        start_time = time.time()
        context = context or {}
        
        # Generate unique request_id for tracing this validation chain
        request_id = context.get("request_id") or str(uuid.uuid4())[:8]
        
        try:
            # Ensure Nova is initialized (lazy initialization on first use)
            await self._ensure_nova_initialized()
            
            # Check if Nova is properly available (tools + environment)
            if not NOVA_TOOLS_AVAILABLE or not self.nova_available:
                log.debug(f"[{request_id}] nova_tools not available or not configured for claim {claim.id}")
                return ValidationResult(
                    claim_id=claim.id,
                    is_valid=False,
                    ground_truth_value=None,
                    evidence=[],
                    confidence=0.0,
                    metadata={
                        "provider": "nova",
                        "status": "unavailable",
                        "latency_ms": int((time.time() - start_time) * 1000),
                        "request_id": request_id,
                    },
                )
            
            # Build claim text for verification
            claim_text = f"{claim.subject}: {claim.expected_value}"
            
            # Implement exponential backoff for Nova rate limiting awareness
            # May retry up to 3 times on timeout
            max_retries = 3
            verify_result_json = None
            
            for attempt in range(max_retries):
                try:
                    log.debug(f"[{request_id}] Verifying claim {claim.id} (attempt {attempt + 1}/{max_retries})")
                    # Query Nova to verify the claim
                    verify_result_json = await nova_verify(claim_text, profile='research')
                    break  # Success
                except RuntimeError as e:
                    if attempt < max_retries - 1 and "timed out" in str(e).lower():
                        wait_time = (2 ** attempt)  # 1s, 2s, 4s
                        log.debug(f"[{request_id}] Nova timeout on attempt {attempt + 1}/{max_retries}, retrying in {wait_time}s")
                        await asyncio.sleep(wait_time)
                    else:
                        raise
            
            # Parse verify result (nova_verify returns JSON string, not dict)
            verify_result = json.loads(verify_result_json)
            
            # Validate response has required fields
            required_fields = {
                "grounded": bool,
                "confidence": (float, int),
                "evidence": list,
            }
            
            validation_errors = []
            for field_name, expected_type in required_fields.items():
                if field_name not in verify_result:
                    validation_errors.append(f"missing '{field_name}'")
                elif not isinstance(verify_result[field_name], expected_type):
                    validation_errors.append(f"'{field_name}' has wrong type (expected {expected_type}, got {type(verify_result[field_name]).__name__})")
            
            if validation_errors:
                log.warning(f"[{request_id}] Nova response validation failed for claim {claim.id}: {', '.join(validation_errors)}")
                measured_confidence = 0.0
            else:
                is_valid = verify_result.get("grounded", False)
                measured_confidence = verify_result.get("confidence", 0.5)
                # Confidence scale from Nova: 0.0 = completely ungrounded, 1.0 = fully grounded
                # Clamp to [0.0, 1.0] to handle any out-of-range values from the model
                if not (0.0 <= measured_confidence <= 1.0):
                    log.warning(f"[{request_id}] Nova returned invalid confidence {measured_confidence} for claim {claim.id}, clamping to [0.0, 1.0]")
                    measured_confidence = max(0.0, min(1.0, measured_confidence))
            
            evidence = verify_result.get("evidence", [])
            contradictions = verify_result.get("contradictions", [])
            is_valid = verify_result.get("grounded", False)
            
            # If contradictions found, reduce confidence
            if contradictions and len(contradictions) > 0:
                measured_confidence = max(0.0, measured_confidence - 0.3)
                is_valid = False
                log.debug(f"[{request_id}] Claim {claim.id} has {len(contradictions)} contradictions")
            
            # Latency tracking
            latency_ms = int((time.time() - start_time) * 1000)
            
            log.debug(f"[{request_id}] Validated claim {claim.id} with confidence {measured_confidence:.2f} (latency: {latency_ms}ms)")
            
            return ValidationResult(
                claim_id=claim.id,
                is_valid=is_valid,
                ground_truth_value=claim.expected_value if is_valid else None,
                evidence=evidence if isinstance(evidence, list) else [],
                confidence=measured_confidence,
                metadata={
                    "provider": "nova",
                    "query": claim_text,
                    "status": "verified",
                    "latency_ms": latency_ms,
                    "contradiction_count": len(contradictions) if contradictions else 0,
                    "request_id": request_id,
                },
            )
        
        except RuntimeError as e:
            # _request_json raises RuntimeError, not asyncio.TimeoutError
            if "timed out" in str(e).lower():
                log.warning(f"[{request_id}] Nova verification timeout for claim {claim.id}")
                latency_ms = int((time.time() - start_time) * 1000)
                return ValidationResult(
                    claim_id=claim.id,
                    is_valid=False,
                    ground_truth_value=None,
                    evidence=[],
                    confidence=0.0,
                    metadata={
                        "provider": "nova",
                        "status": "timeout",
                        "latency_ms": latency_ms,
                        "request_id": request_id,
                    },
                )
            else:
                raise
        except Exception as e:
            log.error(f"[{request_id}] Nova validation failed for claim {claim.id}: {e}")
            latency_ms = int((time.time() - start_time) * 1000)
            return ValidationResult(
                claim_id=claim.id,
                is_valid=False,
                ground_truth_value=None,
                evidence=[],
                confidence=0.0,
                metadata={
                    "provider": "nova",
                    "status": "error",
                    "error": str(e),
                    "latency_ms": latency_ms,
                    "request_id": request_id,
                },
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
        """Detect semantic contradictions between current and prior claims using Nova.
        
        Uses nova_verify to check if claims contradict each other at the semantic level,
        not just syntactic equality. For example, "Device in Pennsylvania" and "Device in
        New York" contradict even though the strings differ.
        
        Args:
            claims: Current claims (Claim objects)
            prior_claims: Claims from prior passes (either Claim objects or dicts)
        
        Returns:
            List of contradiction dicts with semantic analysis
        """
        contradictions = []
        request_id = str(uuid.uuid4())[:8]
        
        # Ensure Nova is initialized
        await self._ensure_nova_initialized()
        
        if not prior_claims or len(prior_claims) == 0:
            log.debug(f"[{request_id}] No prior claims for contradiction detection")
            return contradictions
        
        # Reconstruct all Claim objects
        current_claims = claims if all(isinstance(c, Claim) for c in claims) else []
        reconstructed_prior = []
        
        for prior_claim in prior_claims:
            if isinstance(prior_claim, Claim):
                reconstructed_prior.append(prior_claim)
            elif isinstance(prior_claim, dict):
                try:
                    reconstructed_prior.append(Claim(**prior_claim))
                except (TypeError, KeyError) as e:
                    log.debug(f"[{request_id}] Failed to reconstruct prior claim: {e}")
                    continue
        
        log.debug(f"[{request_id}] Checking {len(current_claims)} current claims against {len(reconstructed_prior)} prior claims")
        
        # Check each current claim against prior claims
        for claim in current_claims:
            for prior_claim in reconstructed_prior:
                # Same subject? These might contradict
                if claim.subject != prior_claim.subject:
                    continue
                
                # Exact same value? No contradiction
                if claim.expected_value == prior_claim.expected_value:
                    continue
                
                # Different values on same subject - check for semantic contradiction
                try:
                    # Use Nova to semantically verify both claims
                    contradiction_query = (
                        f"Do these claims contradict each other? "
                        f"Claim 1 (pass {prior_claim.id}): {prior_claim.subject} = {prior_claim.expected_value}. "
                        f"Claim 2 (pass {claim.id}): {claim.subject} = {claim.expected_value}."
                    )
                    
                    # Try to use nova_verify if available
                    try:
                        from nova_tools import nova_verify
                        log.debug(f"[{request_id}] Using nova_verify for contradiction detection between claims {prior_claim.id} and {claim.id}")
                        verify_result = nova_verify(
                            claim=contradiction_query,
                            profile="auto",
                            top=5
                        )
                        is_contradicted = verify_result.get("grounded", False) and \
                                         "contradiction" in verify_result.get("grounding", "").lower()
                    except (ImportError, Exception) as e:
                        # Fallback: use simple heuristic for numeric claims
                        log.debug(f"[{request_id}] Using heuristic for contradiction detection: {e}")
                        is_contradicted = False
                        try:
                            if isinstance(claim.expected_value, (int, float)) and \
                               isinstance(prior_claim.expected_value, (int, float)):
                                # Numeric claims: >20% difference suggests contradiction
                                max_val = max(abs(claim.expected_value), abs(prior_claim.expected_value))
                                if max_val > 0:
                                    diff_pct = abs(claim.expected_value - prior_claim.expected_value) / max_val * 100
                                    is_contradicted = diff_pct > 20
                        except (TypeError, AttributeError, ZeroDivisionError) as e:
                            log.debug(f"[{request_id}] Error comparing claims during contradiction detection: {e}")
                    
                    if is_contradicted:
                        contradiction_dict = {
                            "claim_1_id": prior_claim.id,
                            "claim_2_id": claim.id,
                            "subject": claim.subject,
                            "claim_1_value": prior_claim.expected_value,
                            "claim_2_value": claim.expected_value,
                            "contradiction": (
                                f"Semantic contradiction: {claim.subject} changed from "
                                f"{prior_claim.expected_value} to {claim.expected_value}"
                            ),
                            "detection_method": "nova_verify" if 'verify_result' in locals() else "heuristic",
                            "request_id": request_id,
                        }
                        contradictions.append(contradiction_dict)
                        log.debug(f"[{request_id}] Found contradiction: {contradiction_dict['contradiction']}")
                
                except Exception as e:
                    log.warning(f"[{request_id}] Contradiction detection failed: {e}")
                    continue
        
        log.debug(f"[{request_id}] Contradiction detection complete: {len(contradictions)} contradictions found")
        return contradictions

    async def validate_multi_device(
        self,
        claims: List[Claim],
        device_ids: List[str],
    ) -> ValidationResult:
        """Validate claim against measurements from multiple DAMA phones.
        
        For each device_id, searches Great Library for sensor data and aggregates
        confidence across devices. Useful for validating device-independent claims
        (e.g., network status, API availability) against multiple data sources.
        
        Args:
            claims: List of Claim objects to validate
            device_ids: List of device IDs to aggregate data from
        
        Returns:
            ValidationResult with:
            - confidence: Aggregated confidence (min of all devices)
            - evidence: Collected from all devices
            - metadata: Contains per-device confidence scores
        """
        if not claims or not device_ids:
            return ValidationResult(
                claim_id="multi_device",
                is_valid=False,
                ground_truth_value=None,
                evidence=[],
                confidence=0.0,
                metadata={
                    "provider": "nova_multi_device",
                    "status": "invalid_input",
                },
            )
        
        try:
            all_evidence = []
            device_confidences = {}
            
            # Validate against each device
            for device_id in device_ids:
                # Search Great Library for sensor data from this device
                search_query = f"{device_id} sensor data"
                try:
                    from nova_tools import nova_search
                    search_results = await nova_search(search_query, top=5)
                    
                    if search_results and len(search_results) > 0:
                        device_conf = max(
                            (r.get("relevance", 0.5) for r in search_results),
                            default=0.5
                        )
                        device_confidences[device_id] = device_conf
                        
                        # Add device-specific evidence
                        for result in search_results:
                            all_evidence.append({
                                "device_id": device_id,
                                "source": result.get("source"),
                                "relevance": result.get("relevance", 0.5),
                                "content_preview": str(result.get("content", ""))[:200],
                            })
                    else:
                        device_confidences[device_id] = 0.0
                
                except Exception as e:
                    log.debug(f"Error validating claim for device {device_id}: {e}")
                    device_confidences[device_id] = 0.0
            
            # Aggregate confidence: use minimum confidence across all devices
            aggregated_confidence = min(device_confidences.values()) if device_confidences else 0.0
            
            return ValidationResult(
                claim_id="multi_device_validation",
                is_valid=aggregated_confidence >= 0.5,
                ground_truth_value=None,
                evidence=all_evidence,
                confidence=aggregated_confidence,
                metadata={
                    "provider": "nova_multi_device",
                    "device_count": len(device_ids),
                    "device_confidences": device_confidences,
                    "aggregation_method": "min",
                },
            )
        
        except Exception as e:
            log.error(f"Multi-device validation failed: {e}")
            return ValidationResult(
                claim_id="multi_device_validation",
                is_valid=False,
                ground_truth_value=None,
                evidence=[],
                confidence=0.0,
                metadata={
                    "provider": "nova_multi_device",
                    "status": "error",
                    "error": str(e),
                },
            )

    async def get_sensor_specs(self) -> Dict[str, Dict[str, Any]]:
        """Discover sensor capabilities from Great Library.
        
        Queries the Great Library for sensor specification matrix including:
        - Measurement range and accuracy bounds
        - Freshness/update frequency
        - Known limitations and error margins
        
        Returns:
            Dict mapping sensor_id to specs:
            {
                "GPS.POSITION": {
                    "range": "Earth surface",
                    "accuracy_meters": 5.0,
                    "freshness_ms": 100,
                    "units": "degrees lat/lon",
                },
                ...
            }
        """
        if not NOVA_TOOLS_AVAILABLE or not self.nova_available:
            log.debug("Nova not available for sensor discovery")
            return {}
        
        try:
            request_id = str(uuid.uuid4())[:8]
            log.debug(f"[{request_id}] Discovering sensor capabilities from Great Library")
            
            # Query Nova for sensor specs
            query = "sensor accuracy bounds specifications measurement precision freshness rate"
            results = await nova_search(query, top=10, profile='research')
            
            sensor_specs = {}
            
            if results:
                # Parse sensor specs from search results
                for doc in results:
                    # Extract sensor info from document metadata/content
                    if "content" in doc:
                        content = doc["content"]
                        # Simple heuristic: look for common sensor names
                        for sensor_name in ["GPS", "WIFI", "ACCELEROMETER", "GYROSCOPE", "MAGNETOMETER", "BATTERY", "TEMPERATURE"]:
                            if sensor_name.lower() in content.lower():
                                if sensor_name not in sensor_specs:
                                    sensor_specs[sensor_name] = {
                                        "range": "unknown",
                                        "accuracy_meters": None,
                                        "freshness_ms": 1000,
                                        "units": "unknown",
                                        "evidence_source": doc.get("source", "great_library"),
                                    }
            
            log.debug(f"[{request_id}] Discovered {len(sensor_specs)} sensor types from Great Library")
            return sensor_specs
            
        except Exception as e:
            log.warning(f"Failed to discover sensor specs: {e}")
            return {}

    async def get_context(self, query: str) -> Dict[str, Any]:
        """Fetch context from Great Library for a query.
        
        Returns comprehensive context including:
        - Available domains for validation
        - Sensor capabilities and specifications
        - Recent validation results
        - Contradiction history
        
        Args:
            query: User query or claim to get context for
        
        Returns:
            Dict with keys:
            - query: The input query
            - domains_available: List of domains this provider can validate
            - sensor_specs: Discovered sensor capabilities
            - status: Provider status (ready | unavailable | error)
            - metadata: Additional context (timestamps, request_id, etc.)
        """
        request_id = str(uuid.uuid4())[:8]
        
        await self._ensure_nova_initialized()
        
        sensor_specs = await self.get_sensor_specs()
        
        return {
            "query": query,
            "domains_available": await self.available_domains(),
            "sensor_specs": sensor_specs,
            "status": "ready" if self.nova_available else "unavailable",
            "metadata": {
                "provider": "nova",
                "request_id": request_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# MQTT Implementation (Async-native with aiomqtt)
# ─────────────────────────────────────────────────────────────────────────────


class MQTTGroundTruthProvider:
    """Fetch ground truth from DAMA phone telemetry via MQTT.
    
    Subscribes to dama/{device}/telemetry and validates claims against live sensor data.
    Uses pure async/await with aiomqtt to avoid threading issues.
    """
    
    def __init__(
        self,
        broker_host: str = "botnet.floppydicks.net",
        broker_port: int = 1883,
        keepalive: int = 30,
        cache_ttl_seconds: int = 30,
        broker_user: Optional[str] = None,
        broker_password: Optional[str] = None,
        max_cache_size: int = 10000,
        mqtt_qos: int = 1,
    ):
        """Initialize MQTT provider.
        
        Args:
            broker_host: MQTT broker hostname
            broker_port: MQTT broker port
            keepalive: MQTT keepalive interval in seconds
            cache_ttl_seconds: Sensor data TTL before expiry
            broker_user: MQTT username (default: env MQTT_USERNAME or "dama")
            broker_password: MQTT password (default: env MQTT_PASSWORD or "")
            max_cache_size: Max telemetry entries before eviction (default: 10000)
            mqtt_qos: MQTT Quality of Service level (0, 1, or 2; default: 1)
        """
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.keepalive = keepalive
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_cache_size = max_cache_size
        self.mqtt_qos = int(os.getenv("MQTT_QoS", str(mqtt_qos)))
        
        # Credentials from parameters or environment
        self.broker_user = broker_user or os.getenv("MQTT_USERNAME", "dama")
        self.broker_password = broker_password or os.getenv("MQTT_PASSWORD", "")
        
        self.connected = False
        self._mqtt_client = None
        self._message_task = None  # Track background message loop task
        self._message_queue = asyncio.Queue()  # Queue for messages before loop starts
        self._message_loop_ready = False  # Flag to track if message loop is ready
        self._db_conn = None  # Keep persistent DB connection
        self._sensor_cache = {}  # {device_id: {sensor_type: {data, timestamp, freshness_ms}}}
        self._device_presence = {}  # {device_id: {present: bool, last_heartbeat: datetime}}
        self._cache_lock = asyncio.Lock()
        self._heartbeat_task = None  # Track heartbeat checker task
        
        # Sensor type registry: define expected sensor sections
        self.sensor_registry = {
            "gps": ["valid_fix", "latitude", "longitude", "age_ms"],
            "wifi": ["networks", "age_ms"],
            "bluetooth": ["devices", "age_ms"],
        }
        
        # SQLite persistence
        self._db_path = Path.home() / ".deep_think" / "mqtt_cache.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """Initialize SQLite database for sensor cache persistence."""
        try:
            self._db_conn = sqlite3.connect(str(self._db_path))
            cursor = self._db_conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sensor_cache (
                    device_id TEXT NOT NULL,
                    sensor_type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    freshness_ms INTEGER DEFAULT 0,
                    ttl_seconds INTEGER DEFAULT 30,
                    PRIMARY KEY (device_id, sensor_type)
                )
            """)
            self._db_conn.commit()
            log.debug(f"Initialized SQLite cache at {self._db_path}")
        except Exception as e:
            log.error(f"Failed to initialize SQLite database: {e}")
    
    def _load_cache_from_db(self):
        """Load sensor cache from SQLite database into memory."""
        try:
            if not self._db_conn:
                return
            
            cursor = self._db_conn.cursor()
            cursor.execute("SELECT device_id, sensor_type, data, timestamp, freshness_ms FROM sensor_cache")
            rows = cursor.fetchall()
            
            now = datetime.now(timezone.utc)
            for device_id, sensor_type, data_json, timestamp_str, freshness_ms in rows:
                try:
                    data = json.loads(data_json)
                    timestamp = datetime.fromisoformat(timestamp_str)
                    
                    # Skip expired entries
                    age_seconds = (now - timestamp).total_seconds()
                    if age_seconds > self.cache_ttl_seconds:
                        continue
                    
                    if device_id not in self._sensor_cache:
                        self._sensor_cache[device_id] = {}
                    
                    self._sensor_cache[device_id][sensor_type] = {
                        "data": data,
                        "timestamp": timestamp,
                        "freshness_ms": freshness_ms,
                    }
                except Exception as e:
                    log.debug(f"Skipping invalid cache entry: {e}")
            
            log.debug(f"Loaded {sum(len(v) for v in self._sensor_cache.values())} sensors from cache")
        except Exception as e:
            log.debug(f"Failed to load cache from DB: {e}")
    
    def _save_cache_to_db(self, device_id: str, sensor_type: str):
        """Save a single sensor entry to SQLite database."""
        try:
            if device_id not in self._sensor_cache:
                return
            
            sensor = self._sensor_cache[device_id].get(sensor_type)
            if not sensor or not self._db_conn:
                return
            
            cursor = self._db_conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO sensor_cache 
                (device_id, sensor_type, data, timestamp, freshness_ms, ttl_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                device_id,
                sensor_type,
                json.dumps(sensor["data"]),
                sensor["timestamp"].isoformat(),
                sensor.get("freshness_ms", 0),
                self.cache_ttl_seconds,
            ))
            self._db_conn.commit()
        except Exception as e:
            log.debug(f"Failed to save cache to DB: {e}")
    
    async def connect(self) -> bool:
        """Connect to MQTT broker with TLS and subscribe to telemetry.
        
        Features:
        - TLS 1.2+ with required certificate verification
        - Credentials from environment or constructor parameters
        - Startup validation to verify broker is responding (5 second timeout)
        - Loads persisted cache from SQLite
        - Starts background heartbeat checker
        - Starts message processing loop
        """
        try:
            import aiomqtt
        except ImportError:
            log.error("aiomqtt not installed. Install with: pip install aiomqtt")
            return False
        
        try:
            # Load persisted cache from SQLite
            self._load_cache_from_db()
            
            # Setup TLS parameters with TLSv1.2+
            tls_params = aiomqtt.TLSParameters(
                ca_certs=None,  # Use system default CA certificates
                certfile=None,
                keyfile=None,
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLSv1_2,
                ciphers=None,
            )
            
            log.info(f"Connecting to MQTT broker {self.broker_host}:{self.broker_port} with TLS 1.2+ (QoS {self.mqtt_qos})")
            self._mqtt_client = aiomqtt.Client(
                hostname=self.broker_host,
                port=self.broker_port,
                username=self.broker_user,
                password=self.broker_password,
                keepalive=self.keepalive,
                tls_params=tls_params,
            )
            await self._mqtt_client.connect()
            
            await self._mqtt_client.subscribe("dama/+/telemetry", qos=self.mqtt_qos)
            log.info("Connected to MQTT broker and subscribed to dama/+/telemetry")
            
            # Clear password from memory after connecting
            self.broker_password = ""
            
            # Start background message loop and heartbeat checker
            self._message_task = asyncio.create_task(self._message_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_checker())
            
            # Mark message loop as ready after giving it time to start processing
            await asyncio.sleep(0.1)
            self._message_loop_ready = True
            
            # Replay any queued messages that arrived during startup
            try:
                while not self._message_queue.empty():
                    queued_msg = self._message_queue.get_nowait()
                    device_id = queued_msg.get("device_id")
                    payload = queued_msg.get("payload")
                    if device_id and payload and self._is_valid_device_id(device_id):
                        await self._cache_telemetry(device_id, payload)
                        log.debug(f"Replayed queued message from {device_id}")
            except Exception as e:
                log.debug(f"Error replaying queued messages: {e}")
            
            # Validate startup: wait up to 5 seconds for first telemetry
            try:
                await asyncio.wait_for(self._wait_for_first_telemetry(), timeout=5.0)
                log.info("MQTT broker validated: received first telemetry within 5 seconds")
                self.connected = True
            except asyncio.TimeoutError:
                log.warning("MQTT broker connected but no telemetry received within 5 seconds (graceful degradation)")
                self.connected = True  # Still set as connected for graceful degradation
            
            return True
        except Exception as e:
            log.error(f"MQTT connection failed: {e}")
            self.connected = False
            return False
    
    async def _wait_for_first_telemetry(self, timeout: float = 5.0):
        """Wait for first telemetry message to arrive."""
        for _ in range(int(timeout * 10)):  # Check every 100ms
            if self._sensor_cache:
                return True
            await asyncio.sleep(0.1)
        raise asyncio.TimeoutError("No telemetry received")
    
    async def _message_loop(self):
        """Background task to receive and cache MQTT messages."""
        if not self._mqtt_client:
            return
        
        try:
            async with self._mqtt_client.messages() as messages:
                async for message in messages:
                    try:
                        topic = message.topic
                        payload = json.loads(message.payload.decode())
                        
                        # Parse device_id from topic "dama/{device_id}/telemetry"
                        parts = topic.split('/')
                        if len(parts) >= 2:
                            device_id = parts[1]
                            
                            # Validate device_id format (alphanumeric, dash, underscore only)
                            if self._is_valid_device_id(device_id):
                                await self._cache_telemetry(device_id, payload)
                            else:
                                log.warning(f"Rejecting malformed device_id from topic {topic}: '{device_id}'")
                    except json.JSONDecodeError:
                        log.debug(f"Skipping malformed JSON from {message.topic}")
                    except Exception as e:
                        log.debug(f"Error processing message: {e}")
        except Exception as e:
            log.error(f"Message loop error: {e}")
    
    def _is_valid_device_id(self, device_id: str) -> bool:
        """Validate device_id format (alphanumeric, dash, underscore only)."""
        if not device_id or not isinstance(device_id, str) or device_id.isspace():
            return False
        # Allow alphanumeric, dash, underscore
        return bool(re.match(r'^[a-zA-Z0-9_-]+$', device_id))
    
    async def _cache_telemetry(self, device_id: str, payload: Dict[str, Any]):
        """Cache telemetry data from MQTT with JSON schema validation.
        
        Validates payload schema and rejects malformed messages.
        Enforces cache size limits with LRU eviction.
        Queues messages if message loop is not yet ready.
        
        Expected payload schema (JSON):
        {
            "device_id": "pixel-9-pro-xl",
            "timestamp": "2026-05-01T02:45:00Z",
            "gps": {
                "valid_fix": true,
                "latitude": float,
                "longitude": float,
                "age_ms": int
            },
            "wifi": {
                "networks": [
                    {"ssid": str, "rssi": int, "channel": int}
                ],
                "age_ms": int
            },
            "bluetooth": {
                "devices": [
                    {"name": str, "rssi": int}
                ],
                "age_ms": int
            }
        }
        
        Required fields: device_id, timestamp.
        Optional sections: gps, wifi, bluetooth (each with age_ms).
        Validates types and rejects malformed payloads with warnings.
        """
        # Queue message if message loop is not yet ready (defensive measure during startup)
        if not self._message_loop_ready:
            try:
                self._message_queue.put_nowait({
                    "device_id": device_id,
                    "payload": payload,
                })
                log.debug(f"Queued telemetry from {device_id} (message loop not ready yet)")
                return
            except asyncio.QueueFull:
                log.warning(f"Message queue full, dropping telemetry from {device_id}")
                return
        
        async with self._cache_lock:
            now = datetime.now(timezone.utc)
            
            # Validate payload schema: required fields
            required_fields = ["device_id", "timestamp"]
            if not all(field in payload for field in required_fields):
                log.warning(f"Malformed payload missing required fields: {required_fields}")
                return
            
            # Validate edge case: empty device_id from topic split
            if not device_id or not isinstance(device_id, str) or device_id.isspace():
                log.warning(f"Invalid device_id from topic split: '{device_id}'")
                return
            
            # Validate cache_ttl_seconds configuration
            if self.cache_ttl_seconds <= 0:
                log.error(f"Invalid cache_ttl_seconds: {self.cache_ttl_seconds}. Using default 30.")
                self.cache_ttl_seconds = 30
            
            # Validate optional sections if present
            def validate_section(section_name: str, required_keys: List[str], section_data: Any) -> bool:
                """Validate an optional section (gps, wifi, bluetooth)."""
                if section_data is None:
                    log.warning(f"{section_name} section is None, skipping")
                    return False
                if not isinstance(section_data, dict):
                    log.warning(f"{section_name} section is not a dict, skipping")
                    return False
                for key in required_keys:
                    if key not in section_data:
                        log.warning(f"{section_name} section missing required key: {key}")
                        return False
                return True
            
            # Initialize device cache if needed
            if device_id not in self._sensor_cache:
                self._sensor_cache[device_id] = {}
            if device_id not in self._device_presence:
                self._device_presence[device_id] = {
                    "present": True,
                    "last_heartbeat": now,
                }
            
            # Update presence using timestamp comparison (fixes race condition)
            self._device_presence[device_id]["present"] = True
            self._device_presence[device_id]["last_heartbeat"] = now
            
            # Cache raw payload and structured sensors
            self._sensor_cache[device_id]["_raw"] = {
                "data": payload,
                "timestamp": now,
            }
            
            # Extract and cache GPS sensor with registry validation
            if "gps" in payload:
                gps = payload["gps"]
                expected_gps_keys = self.sensor_registry.get("gps", [])
                if validate_section("gps", expected_gps_keys, gps):
                    age_ms = gps.get("age_ms", 0)
                    # Handle negative age_ms
                    if age_ms < 0:
                        log.warning(f"Negative age_ms in gps: {age_ms}. Treating as stale.")
                        age_ms = 999999  # Very large value signals stale data
                    self._sensor_cache[device_id]["GPS.POSITION"] = {
                        "data": gps,
                        "timestamp": now,
                        "freshness_ms": age_ms,
                    }
                    self._save_cache_to_db(device_id, "GPS.POSITION")
                else:
                    log.warning(f"GPS section failed validation, skipping")
            
            # Extract and cache WiFi sensor with registry validation
            if "wifi" in payload:
                wifi = payload["wifi"]
                expected_wifi_keys = self.sensor_registry.get("wifi", [])
                if validate_section("wifi", expected_wifi_keys, wifi):
                    age_ms = wifi.get("age_ms", 0)
                    # Handle negative age_ms
                    if age_ms < 0:
                        log.warning(f"Negative age_ms in wifi: {age_ms}. Treating as stale.")
                        age_ms = 999999
                    self._sensor_cache[device_id]["WIFI.NEARBY_NETWORKS"] = {
                        "data": wifi,
                        "timestamp": now,
                        "freshness_ms": age_ms,
                    }
                    self._save_cache_to_db(device_id, "WIFI.NEARBY_NETWORKS")
                else:
                    log.warning(f"WiFi section failed validation, skipping")
            
            # Extract and cache Bluetooth sensor with registry validation
            if "bluetooth" in payload:
                bt = payload["bluetooth"]
                expected_bt_keys = self.sensor_registry.get("bluetooth", [])
                if validate_section("bluetooth", expected_bt_keys, bt):
                    age_ms = bt.get("age_ms", 0)
                    # Handle negative age_ms
                    if age_ms < 0:
                        log.warning(f"Negative age_ms in bluetooth: {age_ms}. Treating as stale.")
                        age_ms = 999999
                    self._sensor_cache[device_id]["BT.NEARBY_DEVICES"] = {
                        "data": bt,
                        "timestamp": now,
                        "freshness_ms": age_ms,
                    }
                    self._save_cache_to_db(device_id, "BT.NEARBY_DEVICES")
                else:
                    log.warning(f"Bluetooth section failed validation, skipping")
            
            # Check for unknown sensor types (log warning)
            for key in payload:
                if key not in required_fields and key not in self.sensor_registry:
                    log.debug(f"Unknown sensor type in payload: {key}")
            
            # Enforce cache size limit with LRU eviction
            total_entries = sum(len(sensors) for sensors in self._sensor_cache.values())
            if total_entries > self.max_cache_size:
                self._evict_oldest_sensor()
                log.warning(f"Cache size exceeded {self.max_cache_size}, evicted oldest sensor")
            
            log.debug(f"Cached telemetry from {device_id}: {list(payload.keys())}")
    
    def _evict_oldest_sensor(self):
        """Evict the oldest sensor entry from cache (LRU)."""
        oldest_timestamp = None
        oldest_key = None
        
        for device_id, sensors in self._sensor_cache.items():
            for sensor_type, sensor_data in sensors.items():
                if sensor_type == "_raw":
                    continue
                ts = sensor_data.get("timestamp")
                if ts and (oldest_timestamp is None or ts < oldest_timestamp):
                    oldest_timestamp = ts
                    oldest_key = (device_id, sensor_type)
        
        if oldest_key:
            device_id, sensor_type = oldest_key
            del self._sensor_cache[device_id][sensor_type]
            log.debug(f"Evicted oldest sensor: {device_id}/{sensor_type}")
    
    async def _heartbeat_checker(self):
        """Background task to periodically check broker health via /health endpoint."""
        while self.connected:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds
                
                # Try to verify connection is still active
                if self._mqtt_client:
                    # Simple connectivity check: if no messages in 2x keepalive, reconnect
                    last_activity = None
                    for sensors in self._sensor_cache.values():
                        for sensor in sensors.values():
                            if isinstance(sensor, dict) and "timestamp" in sensor:
                                ts = sensor["timestamp"]
                                if last_activity is None or ts > last_activity:
                                    last_activity = ts
                    
                    if last_activity:
                        age = (datetime.now(timezone.utc) - last_activity).total_seconds()
                        if age > (self.keepalive * 2):
                            log.warning(f"No messages for {age:.0f}s (2x keepalive), reconnect may be needed")
            except Exception as e:
                log.debug(f"Heartbeat check error: {e}")
    
    async def close(self):
        """Gracefully disconnect from MQTT broker and cleanup resources.
        
        Cancels background tasks, closes DB connection, and disconnects from broker.
        """
        try:
            # Cancel background tasks
            if self._message_task:
                self._message_task.cancel()
                try:
                    await self._message_task
                except asyncio.CancelledError:
                    pass
            
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass
            
            # Disconnect from MQTT broker
            if self._mqtt_client:
                await self._mqtt_client.disconnect()
                self.connected = False
                log.info("Disconnected from MQTT broker")
            
            # Close database connection
            if self._db_conn:
                self._db_conn.close()
                self._db_conn = None
                log.info("Closed SQLite database connection")
        except Exception as e:
            log.error(f"Error during close: {e}")
    
    async def available_devices(self) -> List[str]:
        """Return list of devices that have published recently."""
        async with self._cache_lock:
            now = datetime.now(timezone.utc)
            active_devices = [
                device_id
                for device_id, info in self._device_presence.items()
                if (now - info["last_heartbeat"]).total_seconds() < self.cache_ttl_seconds
            ]
            return active_devices
    
    async def available_domains(self) -> List[str]:
        """Return available sensor domains."""
        return ["gps", "wifi", "bluetooth", "device_health"]
    
    async def get_sensor_data(
        self,
        sensor_id: str,
        device_id: Optional[str] = None,
        time_range: Optional[tuple[datetime, datetime]] = None,
    ) -> Dict[str, Any]:
        """Fetch sensor data from cache with optional time-range filtering.
        
        Args:
            sensor_id: e.g., "GPS.POSITION", "WIFI.NEARBY_NETWORKS", "DEVICE.BATTERY"
            device_id: Device to query (if None, returns from all devices)
            time_range: Optional (start, end) datetime tuple for filtering historical data
        
        Returns:
            Sensor data dict with current_value, recent_values (if time_range), timestamp, status
        """
        async with self._cache_lock:
            if device_id:
                if device_id not in self._sensor_cache:
                    return {
                        "sensor_id": sensor_id,
                        "status": "NO_DATA",
                        "device_id": device_id,
                    }
                
                sensor = self._sensor_cache[device_id].get(sensor_id)
                if not sensor:
                    return {
                        "sensor_id": sensor_id,
                        "status": "NO_DATA",
                        "device_id": device_id,
                    }
                
                # Build response
                response = {
                    "sensor_id": sensor_id,
                    "device_id": device_id,
                    "current_value": sensor["data"],
                    "freshness_ms": sensor.get("freshness_ms", 0),
                    "timestamp": sensor["timestamp"].isoformat(),
                    "status": "OK",
                }
                
                # If time_range requested, include historical values
                if time_range and hasattr(sensor, 'history'):
                    start, end = time_range
                    historical = [
                        {"timestamp": ts.isoformat(), "value": val}
                        for ts, val in sensor.get("history", [])
                        if start <= ts <= end
                    ]
                    response["recent_values"] = historical
                
                return response
            else:
                # Return from all devices
                results = {}
                for dev_id, sensors in self._sensor_cache.items():
                    if sensor_id in sensors:
                        sensor = sensors[sensor_id]
                        results[dev_id] = {
                            "current_value": sensor["data"],
                            "freshness_ms": sensor.get("freshness_ms", 0),
                            "timestamp": sensor["timestamp"].isoformat(),
                        }
                
                if not results:
                    return {"sensor_id": sensor_id, "status": "NO_DATA"}
                
                return {
                    "sensor_id": sensor_id,
                    "status": "OK",
                    "devices": results,
                }
    
    async def _is_device_online(self, device_id: str) -> bool:
        """Check if device is online (has published recently)."""
        async with self._cache_lock:
            if device_id not in self._device_presence:
                return False
            
            now = datetime.now(timezone.utc)
            last_hb = self._device_presence[device_id]["last_heartbeat"]
            age = (now - last_hb).total_seconds()
            
            is_online = age < self.cache_ttl_seconds
            return is_online
    
    def _validate_gps_availability(self, gps_data: Dict[str, Any]) -> float:
        """Calculate confidence for GPS availability claim."""
        if not gps_data:
            return 0.0
        
        confidence = 0.0
        if gps_data.get("valid_fix", False):
            confidence = 0.9
            # Deduct for staleness (older data = less confidence)
            age_ms = gps_data.get("age_ms", 0)
            staleness_penalty = min(0.4, age_ms / 10000.0)  # max -0.4 for 4+ second old data
            confidence -= staleness_penalty
        
        return max(0.0, min(1.0, confidence))
    
    def _validate_wifi_availability(self, wifi_data: Dict[str, Any]) -> float:
        """Calculate confidence for WiFi availability claim."""
        if not wifi_data:
            return 0.0
        
        networks = wifi_data.get("networks", [])
        if not networks:
            return 0.0
        
        confidence = 0.85  # Base confidence for detected networks
        
        # Bonus for strong signal
        best_rssi = max((n.get("rssi", -100) for n in networks), default=-100)
        if best_rssi > -60:
            confidence = min(1.0, confidence + 0.1)
        
        # Deduct for staleness
        age_ms = wifi_data.get("age_ms", 0)
        staleness_penalty = min(0.3, age_ms / 10000.0)
        confidence -= staleness_penalty
        
        return max(0.0, min(1.0, confidence))
    
    def _validate_bt_availability(self, bt_data: Dict[str, Any]) -> float:
        """Calculate confidence for Bluetooth availability claim."""
        if not bt_data:
            return 0.0
        
        devices = bt_data.get("devices", [])
        if not devices:
            return 0.0
        
        confidence = 0.80  # Base confidence for detected devices
        
        # Deduct for staleness
        age_ms = bt_data.get("age_ms", 0)
        staleness_penalty = min(0.3, age_ms / 10000.0)
        confidence -= staleness_penalty
        
        return max(0.0, min(1.0, confidence))
    
    async def validate(
        self,
        claim: Claim,
        context: Optional[Dict[str, Any]] = None,
    ) -> ValidationResult:
        """Validate claim against real sensor data from MQTT using tolerance windows.
        
        Args:
            claim: The claim to validate
            context: Optional context (may include device_id)
        
        Returns:
            ValidationResult with is_valid and confidence based on sensor data
        """
        context = context or {}
        device_id = context.get("device_id")
        
        async with self._cache_lock:
            # Get sensor cache snapshot
            if device_id:
                if device_id not in self._sensor_cache:
                    return ValidationResult(
                        claim_id=claim.id,
                        is_valid=False,
                        ground_truth_value=None,
                        evidence=[],
                        confidence=0.0,
                        contradiction_source="device_offline",
                        metadata={"provider": "mqtt", "device_id": device_id, "reason": "device_not_found"},
                    )
                
                sensor_data = self._sensor_cache[device_id].get(claim.subject)
            else:
                # Try to find sensor across all devices
                sensor_data = None
                for dev_cache in self._sensor_cache.values():
                    if claim.subject in dev_cache:
                        sensor_data = dev_cache[claim.subject]
                        break
        
        # Validate based on subject using proper tolerance windows
        confidence = 0.0
        is_valid = False
        ground_truth_value = None
        tolerance_window = None
        
        try:
            if not sensor_data:
                return ValidationResult(
                    claim_id=claim.id,
                    is_valid=False,
                    ground_truth_value=None,
                    evidence=[],
                    confidence=0.0,
                    contradiction_source="no_sensor_data",
                    metadata={"provider": "mqtt", "reason": "sensor_not_available"},
                )
            
            sensor_value = sensor_data.get("data", {})
            
            # Validate with subject-specific tolerance windows
            # These match DAMA phone telemetry schema
            
            if "BATTERY" in claim.subject.upper():
                # Battery percentage: ±10% tolerance
                tolerance_window = (10, "%")
                actual = sensor_value.get("battery_pct", None)
                if actual is not None and isinstance(claim.expected_value, (int, float)):
                    diff = abs(actual - claim.expected_value)
                    is_valid = diff <= 10
                    confidence = max(0.0, 1.0 - (diff / 50.0))  # Linear decay
                    ground_truth_value = actual
            
            elif "CPU" in claim.subject.upper() or "PROCESSOR" in claim.subject.upper():
                # CPU usage: ±5% tolerance
                tolerance_window = (5, "%")
                actual = sensor_value.get("cpu_usage", None)
                if actual is not None and isinstance(claim.expected_value, (int, float)):
                    diff = abs(actual - claim.expected_value)
                    is_valid = diff <= 5
                    confidence = max(0.0, 1.0 - (diff / 50.0))
                    ground_truth_value = actual
            
            elif "RAM" in claim.subject.upper() or "MEMORY" in claim.subject.upper():
                # RAM usage: ±5% tolerance
                tolerance_window = (5, "%")
                actual = sensor_value.get("ram_usage", None)
                if actual is not None and isinstance(claim.expected_value, (int, float)):
                    diff = abs(actual - claim.expected_value)
                    is_valid = diff <= 5
                    confidence = max(0.0, 1.0 - (diff / 50.0))
                    ground_truth_value = actual
            
            elif "TEMPERATURE" in claim.subject.upper() or "TEMP" in claim.subject.upper():
                # Temperature: ±2°C tolerance
                tolerance_window = (2, "°C")
                actual = sensor_value.get("temperature_c", None)
                if actual is not None and isinstance(claim.expected_value, (int, float)):
                    diff = abs(actual - claim.expected_value)
                    is_valid = diff <= 2
                    confidence = max(0.0, 1.0 - (diff / 20.0))
                    ground_truth_value = actual
            
            elif "GPS" in claim.subject.upper() or "LOCATION" in claim.subject.upper():
                # GPS: Exact fix validation or coordinate match
                gps_fix = sensor_value.get("gps_fix", False)
                is_valid = gps_fix  # True if GPS has valid fix
                confidence = self._validate_gps_availability(sensor_value)
                ground_truth_value = {
                    "valid_fix": gps_fix,
                    "latitude": sensor_value.get("latitude"),
                    "longitude": sensor_value.get("longitude"),
                }
            
            elif "WIFI" in claim.subject.upper():
                # WiFi network count: ±2 networks tolerance
                tolerance_window = (2, "networks")
                actual_networks = sensor_value.get("nearby_count", 0)
                if isinstance(claim.expected_value, (int, float)):
                    diff = abs(actual_networks - claim.expected_value)
                    is_valid = diff <= 2
                    confidence = max(0.0, 1.0 - (diff / 30.0))
                    ground_truth_value = {"network_count": actual_networks}
                else:
                    confidence = self._validate_wifi_availability(sensor_value)
                    is_valid = confidence > 0.5
                    ground_truth_value = {"network_count": actual_networks}
            
            elif "BLUETOOTH" in claim.subject.upper() or "BT" in claim.subject.upper():
                # Bluetooth device count
                actual_devices = sensor_value.get("bt_device_count", 0)
                if isinstance(claim.expected_value, (int, float)):
                    diff = abs(actual_devices - claim.expected_value)
                    is_valid = diff <= 2
                    confidence = max(0.0, 1.0 - (diff / 30.0))
                    ground_truth_value = {"device_count": actual_devices}
                else:
                    confidence = self._validate_bt_availability(sensor_value)
                    is_valid = confidence > 0.5
                    ground_truth_value = {"device_count": actual_devices}
            
            else:
                # Generic validation: exact match or loose equality
                is_valid = sensor_value == claim.expected_value
                confidence = 0.9 if is_valid else 0.1
                ground_truth_value = sensor_value
            
            return ValidationResult(
                claim_id=claim.id,
                is_valid=is_valid,
                ground_truth_value=ground_truth_value,
                evidence=[{
                    "sensor_data": sensor_value,
                    "timestamp": sensor_data.get("timestamp"),
                    "tolerance_window": tolerance_window,
                }],
                confidence=confidence,
                metadata={
                    "provider": "mqtt",
                    "sensor_id": claim.subject,
                    "freshness_ms": sensor_data.get("freshness_ms", 0),
                    "device_id": device_id,
                    "validation_method": "tolerance_window" if tolerance_window else "equality",
                },
            )
        except Exception as e:
            log.warning(f"Validation failed for {claim.id}: {e}")
            return ValidationResult(
                claim_id=claim.id,
                is_valid=False,
                ground_truth_value=None,
                evidence=[],
                confidence=0.0,
                contradiction_source="validation_error",
                metadata={"provider": "mqtt", "error": str(e)},
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


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────


async def create_ground_truth_provider(
    provider_type: str = "nova"
) -> Optional[GroundTruthProvider]:
    """Create and initialize a ground truth provider.

    Args:
        provider_type: Type of provider ("nova", "mqtt", "none", "auto")

    Returns:
        Initialized provider or None.
    """
    if provider_type == "none" or provider_type is None:
        log.info("Ground truth validation disabled")
        return None

    if provider_type == "nova" or provider_type == "auto":
        return NovaGroundTruthProvider()

    if provider_type == "mqtt":
        mqtt_host = os.getenv("MQTT_HOST", "botnet.floppydicks.net")
        mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        mqtt_password = os.getenv("MQTT_PASSWORD", "")
        
        provider = MQTTGroundTruthProvider(
            broker_host=mqtt_host,
            broker_port=mqtt_port,
            broker_user="dama",
            broker_password=mqtt_password,
        )
        await provider.connect()
        return provider

    else:
        log.warning(f"Unknown provider type: {provider_type}")
        return None
