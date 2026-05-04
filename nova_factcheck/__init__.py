"""Nova fact-checking / verification pipeline for deep-think reasoning.

Public API::

    from deep_think_mcp.nova_factcheck import VerificationPipeline

    pipeline = VerificationPipeline()
    enriched = await pipeline.run(result, job_id="abc")
"""
from .escalation import EscalationItem, HumanEscalationQueue, get_escalation_queue
from .extractor import ClaimExtractor, ExtractedClaim
from .nova_client import (
    ClaimVerificationResult,
    NovaVerificationClient,
    VerificationStatus,
)
from .pipeline import VerificationPipeline
from .recalculator import ConfidenceRecalculator, RecalculationResult

__all__ = [
    "ClaimExtractor",
    "ExtractedClaim",
    "NovaVerificationClient",
    "ClaimVerificationResult",
    "VerificationStatus",
    "ConfidenceRecalculator",
    "RecalculationResult",
    "VerificationPipeline",
    "HumanEscalationQueue",
    "EscalationItem",
    "get_escalation_queue",
]
