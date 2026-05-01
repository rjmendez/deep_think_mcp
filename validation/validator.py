"""Validation logic and result processing.

Handles claim validation against ground truth providers, tolerance windows,
confidence calculation, and contradiction detection.
"""

import logging
from typing import Any, Dict, List, Optional

from .types import Claim, ValidationMetrics, ValidationResult, PassValidationResult

log = logging.getLogger(__name__)


async def validate_claims(
    claims: List[Claim],
    provider: Any,  # AbstractGroundTruthProvider
    context: Optional[Dict[str, Any]] = None,
) -> PassValidationResult:
    """Validate a batch of claims against ground truth.
    
    Args:
        claims: List of claims to validate
        provider: Ground truth provider instance
        context: Optional context (pass_num, task_class, etc.)
    
    Returns:
        PassValidationResult with validation details and metrics
    """
    context = context or {}
    pass_num = context.get("pass_num", 0)
    
    if not claims:
        return PassValidationResult(
            pass_num=pass_num,
            claims_extracted=[],
            validation_results=[],
            hallucination_count=0,
            hallucination_details=[],
            overall_confidence=0.0,
            contradiction_with_prior=[],
        )
    
    # Validate claims against provider
    validation_results = await provider.validate_batch(claims, context)
    
    # Detect contradictions with prior claims
    prior_claims = context.get("prior_claims", [])
    contradictions = await provider.detect_contradictions(claims, prior_claims)
    
    # Calculate metrics
    metrics = _calculate_validation_metrics(validation_results, contradictions)
    
    return PassValidationResult(
        pass_num=pass_num,
        claims_extracted=claims,
        validation_results=validation_results,
        hallucination_count=metrics.invalid_claims,
        hallucination_details=_build_hallucination_details(validation_results),
        overall_confidence=metrics.average_confidence,
        contradiction_with_prior=contradictions,
    )


def _calculate_validation_metrics(
    validation_results: List[ValidationResult],
    contradictions: List[Dict],
) -> ValidationMetrics:
    """Calculate validation metrics from results.
    
    Args:
        validation_results: List of validation results
        contradictions: List of detected contradictions
    
    Returns:
        ValidationMetrics with aggregated stats
    """
    total = len(validation_results)
    valid = sum(1 for r in validation_results if r.is_valid)
    invalid = total - valid
    
    avg_confidence = (
        sum(r.confidence for r in validation_results) / total
        if total > 0
        else 0.0
    )
    
    return ValidationMetrics(
        total_claims=total,
        valid_claims=valid,
        invalid_claims=invalid,
        hallucination_count=invalid,
        average_confidence=avg_confidence,
        contradictions_found=len(contradictions),
    )


def _build_hallucination_details(
    validation_results: List[ValidationResult],
) -> List[Dict]:
    """Build detailed hallucination information from validation results.
    
    Args:
        validation_results: List of validation results
    
    Returns:
        List of hallucination details
    """
    hallucinations = []
    
    for result in validation_results:
        if not result.is_valid:
            hallucinations.append({
                "claim_id": result.claim_id,
                "type": "contradiction" if result.contradiction_source else "unsupported",
                "contradiction": result.contradiction_source,
                "confidence": result.confidence,
                "expected": None,  # Placeholder
                "ground_truth": result.ground_truth_value,
            })
    
    return hallucinations


def calculate_confidence_from_evidence(
    evidence: List[Dict],
    base_confidence: float = 0.5,
) -> float:
    """Calculate confidence score from supporting evidence.
    
    Args:
        evidence: List of evidence data points
        base_confidence: Starting confidence value
    
    Returns:
        Confidence score (0.0-1.0)
    """
    if not evidence:
        return base_confidence
    
    # Average evidence relevance/quality if available
    relevances = []
    for item in evidence:
        if isinstance(item, dict):
            if "relevance" in item:
                relevances.append(item["relevance"])
            elif "confidence" in item:
                relevances.append(item["confidence"])
    
    if relevances:
        avg_relevance = sum(relevances) / len(relevances)
        # Weight with base confidence
        return min(1.0, (base_confidence + avg_relevance) / 2.0)
    
    return base_confidence


def merge_validation_results(
    results_per_provider: Dict[str, List[ValidationResult]],
) -> List[ValidationResult]:
    """Merge validation results from multiple providers.
    
    If multiple providers validate the same claim, aggregate their results
    by averaging confidence scores.
    
    Args:
        results_per_provider: {provider_name: [ValidationResult, ...]}
    
    Returns:
        Merged validation results
    """
    merged = {}
    
    # Group results by claim_id
    for provider_name, results in results_per_provider.items():
        for result in results:
            claim_id = result.claim_id
            
            if claim_id not in merged:
                merged[claim_id] = {
                    "claim_id": claim_id,
                    "is_valid_votes": 0,
                    "total_votes": 0,
                    "confidences": [],
                    "evidences": [],
                    "providers": [],
                }
            
            merged[claim_id]["total_votes"] += 1
            merged[claim_id]["confidences"].append(result.confidence)
            merged[claim_id]["evidences"].extend(result.evidence)
            merged[claim_id]["providers"].append(provider_name)
            
            if result.is_valid:
                merged[claim_id]["is_valid_votes"] += 1
    
    # Create merged ValidationResults
    merged_results = []
    for claim_id, data in merged.items():
        avg_confidence = (
            sum(data["confidences"]) / len(data["confidences"])
            if data["confidences"]
            else 0.0
        )
        
        # Majority vote on validity (if >50% valid, consider it valid)
        is_valid = data["is_valid_votes"] / data["total_votes"] > 0.5
        
        merged_results.append(
            ValidationResult(
                claim_id=claim_id,
                is_valid=is_valid,
                ground_truth_value=None,  # Placeholder
                evidence=data["evidences"],
                confidence=avg_confidence,
                metadata={
                    "merged": True,
                    "provider_count": len(data["providers"]),
                    "providers": data["providers"],
                    "votes_for_valid": data["is_valid_votes"],
                },
            )
        )
    
    return merged_results
