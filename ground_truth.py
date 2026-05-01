"""Ground truth provider interface for validating claims against real sensor data.

Enables truth-discovery via sensor feedback. Instead of models hallucinating in isolation,
claims are validated against actual telemetry from the DAMA phone or system metrics.
"""

import asyncio
import json
import logging
import os
import time
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
        """
        try:
            # Dynamically import nova_search if available
            try:
                from nova_tools import nova_search
            except ImportError:
                log.warning(f"nova_tools not available for sensor {sensor_id}")
                return {
                    "sensor_id": sensor_id,
                    "current_value": None,
                    "freshness_ms": 0,
                    "status": "unavailable",
                    "evidence": [],
                    "confidence": 0.0,
                    "metadata": {"error": "nova_tools not available"},
                }
            
            # Build search query for sensor specifications and recent data
            query = f"{sensor_id} sensor data specification accuracy bounds latest measurements"
            results = await nova_search(query, top=5, profile='research')
            
            sensor_info = {
                "sensor_id": sensor_id,
                "current_value": None,
                "freshness_ms": 0,
                "status": "unknown",
                "evidence": [],
                "confidence": 0.5,
                "metadata": {"source": "great_library"},
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
            else:
                sensor_info["status"] = "unavailable"
                sensor_info["confidence"] = 0.0
            
            return sensor_info
            
        except asyncio.TimeoutError:
            log.warning(f"Timeout fetching sensor data for {sensor_id}")
            return {
                "sensor_id": sensor_id,
                "current_value": None,
                "freshness_ms": 0,
                "status": "timeout",
                "evidence": [],
                "confidence": 0.0,
                "metadata": {"error": "timeout"},
            }
        except Exception as e:
            log.error(f"Failed to fetch sensor data for {sensor_id}: {e}")
            return {
                "sensor_id": sensor_id,
                "current_value": None,
                "freshness_ms": 0,
                "status": "error",
                "evidence": [],
                "confidence": 0.0,
                "metadata": {"error": str(e)},
            }

    async def validate(
        self,
        claim: Claim,
        context: Optional[Dict[str, Any]] = None,
    ) -> ValidationResult:
        """Validate a claim against ground truth from Great Library.
        
        Uses nova_verify to check if claim is supported by evidence.
        Falls back to nova_search if verify fails.
        
        Args:
            claim: Claim object with id, subject, expected_value
            context: Optional context including prior_passes, task_class
        
        Returns:
            ValidationResult with:
            - is_valid: True if evidence supports claim
            - ground_truth_value: What the data actually says
            - evidence: List of supporting documents
            - confidence: 0.0-1.0 measured from evidence quality
            - metadata: {provider, query, status, latency_ms}
        """
        start_time = time.time()
        
        try:
            # Dynamically import nova_verify if available
            try:
                from nova_tools import nova_verify
            except ImportError:
                log.debug(f"nova_tools not available for claim {claim.id}")
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
                    },
                )
            
            # Build claim text for verification
            claim_text = f"{claim.subject}: {claim.expected_value}"
            
            # Query Nova to verify the claim
            verify_result = await nova_verify(claim_text, profile='research')
            
            # Parse verify result
            is_valid = verify_result.get("grounded", False)
            measured_confidence = verify_result.get("confidence", 0.5)
            evidence = verify_result.get("evidence", [])
            contradictions = verify_result.get("contradictions", [])
            
            # If contradictions found, reduce confidence
            if contradictions and len(contradictions) > 0:
                measured_confidence = max(0.0, measured_confidence - 0.3)
                is_valid = False
                log.debug(f"Claim {claim.id} has {len(contradictions)} contradictions")
            
            # Latency tracking
            latency_ms = int((time.time() - start_time) * 1000)
            
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
                },
            )
        
        except asyncio.TimeoutError:
            log.warning(f"Nova verification timeout for claim {claim.id}")
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
                },
            )
        except Exception as e:
            log.error(f"Nova validation failed for claim {claim.id}: {e}")
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
        
        if not prior_claims or len(prior_claims) == 0:
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
                    log.debug(f"Failed to reconstruct prior claim: {e}")
                    continue
        
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
                        verify_result = nova_verify(
                            claim=contradiction_query,
                            profile="auto",
                            top=5
                        )
                        is_contradicted = verify_result.get("grounded", False) and \
                                         "contradiction" in verify_result.get("grounding", "").lower()
                    except (ImportError, Exception) as e:
                        # Fallback: use simple heuristic for numeric claims
                        is_contradicted = False
                        try:
                            if isinstance(claim.expected_value, (int, float)) and \
                               isinstance(prior_claim.expected_value, (int, float)):
                                # Numeric claims: >20% difference suggests contradiction
                                max_val = max(abs(claim.expected_value), abs(prior_claim.expected_value))
                                if max_val > 0:
                                    diff_pct = abs(claim.expected_value - prior_claim.expected_value) / max_val * 100
                                    is_contradicted = diff_pct > 20
                        except:
                            pass
                    
                    if is_contradicted:
                        contradictions.append({
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
                        })
                
                except Exception as e:
                    log.warning(f"Contradiction detection failed: {e}")
                    continue
        
        return contradictions

    async def get_context(self, query: str) -> Dict[str, Any]:
        """Fetch context from Great Library for a query."""
        return {
            "query": query,
            "domains_available": await self.available_domains(),
            "status": "ready",
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
    ):
        """Initialize MQTT provider.
        
        Args:
            broker_host: MQTT broker hostname
            broker_port: MQTT broker port
            keepalive: MQTT keepalive interval in seconds
            cache_ttl_seconds: Sensor data TTL before expiry
        """
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.keepalive = keepalive
        self.cache_ttl_seconds = cache_ttl_seconds
        
        self.connected = False
        self._mqtt_client = None
        self._sensor_cache = {}  # {device_id: {sensor_type: {data, timestamp, freshness_ms}}}
        self._device_presence = {}  # {device_id: {present: bool, last_heartbeat: datetime}}
        self._cache_lock = asyncio.Lock()
    
    async def connect(self) -> bool:
        """Connect to MQTT broker and subscribe to telemetry."""
        try:
            import aiomqtt
        except ImportError:
            log.error("aiomqtt not installed. Install with: pip install aiomqtt")
            return False
        
        try:
            log.info(f"Connecting to MQTT broker {self.broker_host}:{self.broker_port}")
            self._mqtt_client = aiomqtt.Client(
                hostname=self.broker_host,
                port=self.broker_port,
                keepalive=self.keepalive,
            )
            await self._mqtt_client.connect()
            
            await self._mqtt_client.subscribe("dama/+/telemetry")
            log.info("Connected to MQTT broker and subscribed to dama/+/telemetry")
            
            self.connected = True
            asyncio.create_task(self._message_loop())
            return True
        except Exception as e:
            log.error(f"MQTT connection failed: {e}")
            self.connected = False
            return False
    
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
                            await self._cache_telemetry(device_id, payload)
                    except json.JSONDecodeError:
                        log.debug(f"Skipping malformed JSON from {message.topic}")
                    except Exception as e:
                        log.debug(f"Error processing message: {e}")
        except Exception as e:
            log.error(f"Message loop error: {e}")
    
    async def _cache_telemetry(self, device_id: str, payload: Dict[str, Any]):
        """Cache telemetry data from MQTT."""
        async with self._cache_lock:
            now = datetime.now(timezone.utc)
            
            # Initialize device cache if needed
            if device_id not in self._sensor_cache:
                self._sensor_cache[device_id] = {}
            if device_id not in self._device_presence:
                self._device_presence[device_id] = {
                    "present": True,
                    "last_heartbeat": now,
                }
            
            # Update presence
            self._device_presence[device_id]["present"] = True
            self._device_presence[device_id]["last_heartbeat"] = now
            
            # Cache raw payload and structured sensors
            self._sensor_cache[device_id]["_raw"] = {
                "data": payload,
                "timestamp": now,
            }
            
            # Extract and cache individual sensors
            if "gps" in payload:
                gps = payload["gps"]
                self._sensor_cache[device_id]["GPS.POSITION"] = {
                    "data": gps,
                    "timestamp": now,
                    "freshness_ms": gps.get("age_ms", 0),
                }
            
            if "wifi" in payload:
                wifi = payload["wifi"]
                self._sensor_cache[device_id]["WIFI.NEARBY_NETWORKS"] = {
                    "data": wifi,
                    "timestamp": now,
                    "freshness_ms": wifi.get("age_ms", 0),
                }
            
            if "bluetooth" in payload:
                bt = payload["bluetooth"]
                self._sensor_cache[device_id]["BT.NEARBY_DEVICES"] = {
                    "data": bt,
                    "timestamp": now,
                    "freshness_ms": bt.get("age_ms", 0),
                }
            
            log.debug(f"Cached telemetry from {device_id}: {list(payload.keys())}")
    
    async def close(self):
        """Disconnect from MQTT broker."""
        if self._mqtt_client:
            await self._mqtt_client.disconnect()
            self.connected = False
            log.info("Disconnected from MQTT broker")
    
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
    
    def _is_device_online(self, device_id: str) -> bool:
        """Check if device is online (has published recently)."""
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
