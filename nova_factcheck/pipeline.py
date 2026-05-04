"""VerificationPipeline — orchestrates claim extraction → Nova verification →
confidence recalculation → escalation.

This is the primary integration point.  Call run() after deep_think_passes
completes to enrich the result dict with verification data.

Result enrichment:
    result["verification_results"]       — list of per-claim dicts
    result["adjusted_final_confidence"]  — confidence after applying boosts/penalties
    result["verification_summary"]       — counts and net adjustment
    result["escalated_claim_ids"]        — claim IDs routed to human review

Usage::

    from deep_think_mcp.nova_factcheck.pipeline import VerificationPipeline

    pipeline = VerificationPipeline()
    enriched = await pipeline.run(result, job_id="abc123")
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .escalation import (
    EscalationItem,
    HumanEscalationQueue,
    LOW_CONFIDENCE_THRESHOLD,
    get_escalation_queue,
)
from .extractor import ClaimExtractor, ExtractedClaim
from .nova_client import (
    ClaimVerificationResult,
    NovaVerificationClient,
    VerificationStatus,
)
from .recalculator import ConfidenceRecalculator

log = logging.getLogger(__name__)


class VerificationPipeline:
    """End-to-end Nova fact-checking pipeline.

    Args:
        extractor: ClaimExtractor instance (uses default if None).
        nova_client: NovaVerificationClient instance (uses default settings if None).
            Pass a pre-constructed client to reuse an existing aiohttp session.
        recalculator: ConfidenceRecalculator instance (uses default if None).
        escalation_queue: HumanEscalationQueue (uses global singleton if None).
        max_claims_per_run: Cap on claims extracted per pipeline run (default 20).
        enabled: If False, run() returns the result dict unchanged.  Allows
            toggling verification at runtime via env var without code changes.
    """

    def __init__(
        self,
        extractor: Optional[ClaimExtractor] = None,
        nova_client: Optional[NovaVerificationClient] = None,
        recalculator: Optional[ConfidenceRecalculator] = None,
        escalation_queue: Optional[HumanEscalationQueue] = None,
        max_claims_per_run: int = 20,
        enabled: bool = True,
    ) -> None:
        self._extractor = extractor or ClaimExtractor(max_claims=max_claims_per_run)
        self._nova_client = nova_client  # None → created per run
        self._recalculator = recalculator or ConfidenceRecalculator()
        self._escalation_queue = escalation_queue or get_escalation_queue()
        self.max_claims_per_run = max_claims_per_run
        self.enabled = enabled

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        result: Dict[str, Any],
        job_id: str = "",
    ) -> Dict[str, Any]:
        """Enrich a deep_think_passes result dict with verification data.

        If Nova is unreachable or disabled, the result is returned untouched
        except that ``verification_results`` is set to an empty list.

        Args:
            result: The dict returned by deep_think_passes or run_fan_out.
            job_id: Job ID for logging / escalation attribution.

        Returns:
            The same dict, mutated in place, with added verification fields.
        """
        if not self.enabled:
            log.debug("VerificationPipeline disabled; skipping for job %s", job_id)
            return result

        # Collect text to verify from all reasoning passes
        all_text = self._collect_text(result)
        if not all_text.strip():
            result.setdefault("verification_results", [])
            return result

        # 1. Extract claims
        claims: List[ExtractedClaim] = self._extractor.extract(all_text)
        if not claims:
            log.debug("No verifiable claims extracted for job %s", job_id)
            result["verification_results"] = []
            return result

        log.info("Extracted %d claims for job %s; verifying with Nova...", len(claims), job_id)

        # 2. Verify with Nova
        vr_list: List[ClaimVerificationResult] = await self._verify_claims(claims)

        # 3. Recalculate confidence
        original_conf = float(result.get("confidence", 0.5))
        recalc = self._recalculator.recalculate(original_conf, vr_list)

        # 4. Route unverifiable claims to escalation queue
        escalated_ids = await self._maybe_escalate(claims, vr_list, job_id)

        # 5. Build structured verification_results list
        verification_results = _build_verification_results(claims, vr_list, recalc.adjusted_final_confidence)

        # 6. Mutate result dict
        result["verification_results"] = verification_results
        result["adjusted_final_confidence"] = recalc.adjusted_final_confidence
        result["verification_summary"] = {
            "total_claims": recalc.total_claims,
            "true_count": recalc.true_count,
            "false_count": recalc.false_count,
            "uncertain_count": recalc.uncertain_count,
            "error_count": recalc.error_count,
            "net_adjustment": recalc.net_adjustment,
            "original_confidence": recalc.original_confidence,
        }
        if escalated_ids:
            result["escalated_claim_ids"] = escalated_ids

        log.info(
            "Verification complete for job %s: %d TRUE, %d FALSE, %d UNCERTAIN "
            "— confidence %.3f → %.3f",
            job_id,
            recalc.true_count,
            recalc.false_count,
            recalc.uncertain_count + recalc.error_count,
            recalc.original_confidence,
            recalc.adjusted_final_confidence,
        )
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _collect_text(self, result: Dict[str, Any]) -> str:
        """Gather all reasoning text from the result for claim extraction."""
        parts: List[str] = []

        # Standard deep_think_passes result
        final_answer = result.get("final_answer", "")
        if final_answer:
            parts.append(str(final_answer))

        pass_outputs = result.get("pass_outputs", [])
        for po in pass_outputs:
            if isinstance(po, str) and po:
                parts.append(po)

        # Fan-out synthesis text
        synthesis = result.get("synthesis", "")
        if synthesis and isinstance(synthesis, str):
            parts.append(synthesis)

        return "\n\n".join(parts)

    async def _verify_claims(
        self,
        claims: List[ExtractedClaim],
    ) -> List[ClaimVerificationResult]:
        """Verify all claims using Nova.  Creates a client if not injected."""
        pairs = [(c.claim_id, c.text) for c in claims]

        if self._nova_client is not None:
            # Caller owns the session lifecycle
            return await self._nova_client.verify_batch(pairs)

        # Create a short-lived client for this run
        async with NovaVerificationClient() as client:
            return await client.verify_batch(pairs)

    async def _maybe_escalate(
        self,
        claims: List[ExtractedClaim],
        vr_list: List[ClaimVerificationResult],
        job_id: str,
    ) -> List[str]:
        """Route claims that cannot be verified to the human escalation queue."""
        by_id = {vr.claim_id: vr for vr in vr_list}
        escalated: List[str] = []

        for claim in claims:
            vr = by_id.get(claim.claim_id)
            if vr is None:
                continue

            reason: Optional[str] = None

            if vr.status == VerificationStatus.ERROR:
                reason = "Nova verification unavailable (network/auth error)"
            elif (
                vr.status == VerificationStatus.UNCERTAIN
                and claim.confidence_in_text < LOW_CONFIDENCE_THRESHOLD
            ):
                reason = (
                    f"Low-confidence claim (model={claim.confidence_in_text:.2f}) "
                    f"and Nova returned UNCERTAIN"
                )

            if reason:
                item = EscalationItem(
                    claim_id=claim.claim_id,
                    claim_text=claim.text,
                    claim_type=claim.claim_type,
                    nova_status=vr.status.value,
                    nova_confidence=vr.nova_confidence,
                    confidence_in_text=claim.confidence_in_text,
                    reason=reason,
                    job_id=job_id,
                )
                await self._escalation_queue.put(item)
                escalated.append(claim.claim_id)

        return escalated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_verification_results(
    claims: List[ExtractedClaim],
    vr_list: List[ClaimVerificationResult],
    adjusted_final_confidence: float,
) -> List[Dict[str, Any]]:
    """Combine extracted claims and verification results into a clean list."""
    by_id = {vr.claim_id: vr for vr in vr_list}
    rows: List[Dict[str, Any]] = []

    for claim in claims:
        vr = by_id.get(claim.claim_id)
        row: Dict[str, Any] = {
            "claim_id": claim.claim_id,
            "claim_text": claim.text,
            "claim_type": claim.claim_type,
            "confidence_in_text": claim.confidence_in_text,
            "source_pass": claim.source_pass,
            "verification_status": vr.status.value if vr else "UNKNOWN",
            "nova_confidence": vr.nova_confidence if vr else 0.0,
            "nova_reasoning": vr.reasoning if vr else "",
            "adjusted_final_confidence": adjusted_final_confidence,
        }
        rows.append(row)

    return rows
