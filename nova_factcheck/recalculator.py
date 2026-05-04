"""ConfidenceRecalculator — adjust deep-think confidence based on Nova verification.

Adjustment rules (per claim):
    TRUE  (SUPPORTED)       → +0.1
    FALSE (CONTRADICTED)    → -0.3
    UNCERTAIN               → -0.05
    ERROR                   → -0.05  (treat as UNCERTAIN)

Final confidence is clamped to [0.0, 1.0].

The recalculator works on the *average* adjustment across all verified claims,
so many TRUE claims can counterbalance a few FALSE ones.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .nova_client import ClaimVerificationResult, VerificationStatus


# Per-verdict adjustment constants
BOOST_TRUE: float = 0.10
PENALTY_FALSE: float = -0.30
PENALTY_UNCERTAIN: float = -0.05
PENALTY_ERROR: float = -0.05   # same weight as UNCERTAIN

_ADJUSTMENTS = {
    VerificationStatus.TRUE: BOOST_TRUE,
    VerificationStatus.FALSE: PENALTY_FALSE,
    VerificationStatus.UNCERTAIN: PENALTY_UNCERTAIN,
    VerificationStatus.ERROR: PENALTY_ERROR,
}


@dataclass
class RecalculationResult:
    """Output of a confidence recalculation."""

    original_confidence: float
    adjusted_final_confidence: float
    total_claims: int
    true_count: int
    false_count: int
    uncertain_count: int
    error_count: int
    net_adjustment: float   # sum of all per-claim adjustments


class ConfidenceRecalculator:
    """Calculate adjusted confidence from a set of claim verification results.

    The recalculation strategy:
    1. For each verified claim, look up its adjustment delta.
    2. Sum the deltas and divide by the number of claims to get a mean adjustment.
       (This makes the adjustment proportional regardless of how many claims
       were extracted.)
    3. Add the mean adjustment to the original confidence.
    4. Clamp the result to [0.0, 1.0].
    """

    def recalculate(
        self,
        original_confidence: float,
        verification_results: List[ClaimVerificationResult],
    ) -> RecalculationResult:
        """Recalculate confidence given verification outcomes.

        Args:
            original_confidence: Pre-verification deep-think confidence (0-1).
            verification_results: List of ClaimVerificationResult objects.

        Returns:
            RecalculationResult with adjusted_final_confidence and per-status counts.
        """
        original_confidence = max(0.0, min(1.0, float(original_confidence)))

        if not verification_results:
            return RecalculationResult(
                original_confidence=original_confidence,
                adjusted_final_confidence=original_confidence,
                total_claims=0,
                true_count=0,
                false_count=0,
                uncertain_count=0,
                error_count=0,
                net_adjustment=0.0,
            )

        counts = {
            VerificationStatus.TRUE: 0,
            VerificationStatus.FALSE: 0,
            VerificationStatus.UNCERTAIN: 0,
            VerificationStatus.ERROR: 0,
        }
        total_delta = 0.0

        for vr in verification_results:
            status = vr.status
            # Guard against unknown statuses
            if status not in _ADJUSTMENTS:
                status = VerificationStatus.UNCERTAIN
            counts[status] += 1
            total_delta += _ADJUSTMENTS[status]

        n = len(verification_results)
        mean_delta = total_delta / n
        adjusted = max(0.0, min(1.0, original_confidence + mean_delta))

        return RecalculationResult(
            original_confidence=original_confidence,
            adjusted_final_confidence=round(adjusted, 4),
            total_claims=n,
            true_count=counts[VerificationStatus.TRUE],
            false_count=counts[VerificationStatus.FALSE],
            uncertain_count=counts[VerificationStatus.UNCERTAIN],
            error_count=counts[VerificationStatus.ERROR],
            net_adjustment=round(mean_delta, 4),
        )
