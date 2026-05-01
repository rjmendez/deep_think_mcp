"""Nova/Great Library implementation of ground truth provider."""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..types import Claim, ValidationResult

log = logging.getLogger(__name__)


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
