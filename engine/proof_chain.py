"""Proof chain tracking for grounded reasoning.

Tracks citations made during reasoning passes. Each cite() call records a
claim → source mapping. Uncited claims are flagged for Nova verification.

Usage::

    chain = ProofChain(job_id="abc123")
    chain.cite("Python was created in 1991", "nova", "doc_42", confidence=0.95)
    chain.flag_uncited("The sky is green")
    result = chain.to_dict()

The proof chain is returned with the final deep_think result and used by the
Nova verification pipeline to adjust confidence on uncited claims.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)
_AUDIT_LOG = logging.getLogger("deep_think.audit")

# Source types for citation tracking
SOURCE_TYPE_NOVA = "nova"
SOURCE_TYPE_DAMA = "dama"
SOURCE_TYPE_WEB = "web"
SOURCE_TYPE_INTERNAL = "internal"  # derived from prior reasoning passes
VALID_SOURCE_TYPES = {SOURCE_TYPE_NOVA, SOURCE_TYPE_DAMA, SOURCE_TYPE_WEB, SOURCE_TYPE_INTERNAL}


@dataclass
class ProofEntry:
    """A single citation in the proof chain."""
    claim: str
    source_type: str        # nova | dama | web | internal
    source_id: str          # document ID, node_id, URL, or pass reference
    confidence: float       # 0.0–1.0
    timestamp: float        # epoch seconds
    pass_num: int = 0       # which reasoning pass produced this claim
    flagged: bool = False   # True if flagged for verification

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim": self.claim,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "confidence": round(self.confidence, 4),
            "timestamp": self.timestamp,
            "pass_num": self.pass_num,
            "flagged": self.flagged,
        }


@dataclass
class UncitedClaim:
    """A claim detected in reasoning output without a corresponding citation."""
    claim: str
    pass_num: int
    timestamp: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim": self.claim,
            "pass_num": self.pass_num,
            "timestamp": self.timestamp,
            "requires_verification": True,
        }


class ProofChain:
    """Accumulates citations and uncited claims across reasoning passes.

    Thread-safe for asyncio — single-threaded access expected.

    Args:
        job_id: Job identifier for audit logging.
        task_class: Task class for audit logging.
    """

    def __init__(self, job_id: str = "", task_class: str = "") -> None:
        self.job_id = job_id
        self.task_class = task_class
        self._entries: List[ProofEntry] = []
        self._uncited: List[UncitedClaim] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def cite(
        self,
        claim: str,
        source_type: str,
        source_id: str,
        confidence: float = 0.8,
        pass_num: int = 0,
    ) -> ProofEntry:
        """Record a citation linking a claim to a grounded source.

        Args:
            claim: The factual claim being made.
            source_type: One of "nova", "dama", "web", "internal".
            source_id: Identifier for the source (document ID, URL, etc.).
            confidence: Confidence in the citation (0.0–1.0).
            pass_num: Reasoning pass number (0 = pre-pass context).

        Returns:
            The created ProofEntry.
        """
        if source_type not in VALID_SOURCE_TYPES:
            log.warning(
                "ProofChain.cite: unknown source_type %r, defaulting to 'internal'",
                source_type,
            )
            source_type = SOURCE_TYPE_INTERNAL

        confidence = max(0.0, min(1.0, confidence))
        entry = ProofEntry(
            claim=claim[:512],
            source_type=source_type,
            source_id=source_id[:256],
            confidence=confidence,
            timestamp=time.time(),
            pass_num=pass_num,
            flagged=False,
        )
        self._entries.append(entry)
        _AUDIT_LOG.debug(
            "PROOF_CITE job_id=%s pass=%d source_type=%s source_id=%s confidence=%.2f claim=%r",
            self.job_id, pass_num, source_type, source_id[:60], confidence, claim[:80],
        )
        return entry

    def flag_uncited(self, claim: str, pass_num: int = 0) -> UncitedClaim:
        """Flag a claim as uncited — no grounded source provided.

        Uncited claims are escalated to Nova verification and logged for audit.

        Args:
            claim: The uncited claim text.
            pass_num: Reasoning pass number where claim appeared.

        Returns:
            The created UncitedClaim.
        """
        uncited = UncitedClaim(
            claim=claim[:512],
            pass_num=pass_num,
            timestamp=time.time(),
        )
        self._uncited.append(uncited)
        _AUDIT_LOG.warning(
            "UNCITED_CLAIM job_id=%s pass=%d claim=%r — flagging for Nova verification",
            self.job_id, pass_num, claim[:80],
        )
        return uncited

    def extract_citations_from_text(
        self,
        text: str,
        source_results: Optional[List[Dict[str, Any]]] = None,
        pass_num: int = 0,
    ) -> int:
        """Scan reasoning text for source references and auto-record citations.

        Looks for patterns like "[1]", "[Source: ...]", "(source: ...)" in text
        and cross-references against known source_results.

        Args:
            text: Reasoning pass output text.
            source_results: Known sources from research tools [{source, content, confidence}].
            pass_num: Reasoning pass number.

        Returns:
            Number of citations automatically recorded.
        """
        import re
        count = 0
        sources = source_results or []

        # Build source ID → confidence map
        source_map = {
            s.get("source", s.get("source_id", "")): s.get("confidence", s.get("score", 0.7))
            for s in sources
        }

        # Match [N] bracket citations (numbered references)
        bracket_refs = re.findall(r'\[(\d+)\]', text)
        for ref in set(bracket_refs):
            idx = int(ref) - 1
            if 0 <= idx < len(sources):
                src = sources[idx]
                src_id = src.get("source", src.get("source_id", f"ref_{ref}"))
                src_type = src.get("source_type", SOURCE_TYPE_NOVA)
                conf = float(src.get("confidence", src.get("score", 0.7)))
                # Extract surrounding claim context
                claim_ctx = f"[ref {ref} cited in pass {pass_num}]"
                self.cite(claim_ctx, src_type, src_id, conf, pass_num)
                count += 1

        # Match explicit Source: references
        explicit_refs = re.findall(r'[Ss]ource:\s*([^\n\]]{5,80})', text)
        for ref in explicit_refs:
            ref = ref.strip()
            if ref in source_map:
                self.cite(
                    f"[explicit source reference: {ref}]",
                    SOURCE_TYPE_NOVA, ref, source_map[ref], pass_num,
                )
                count += 1

        return count

    def build_citations_from_nova_results(
        self,
        nova_results: List[Any],
        pass_num: int = 0,
    ) -> None:
        """Pre-populate proof chain from all Nova search results used as context.

        Args:
            nova_results: List of SearchResult objects from nova_search().
            pass_num: Pass number (0 = pre-pass context injection).
        """
        for r in nova_results:
            src_id = getattr(r, "source", str(r))
            conf = getattr(r, "confidence", 0.7)
            snippet = getattr(r, "snippet", "")[:100]
            self.cite(
                f"[context: {snippet}]",
                SOURCE_TYPE_NOVA, src_id, float(conf), pass_num,
            )

    # ------------------------------------------------------------------
    # Summarization
    # ------------------------------------------------------------------

    @property
    def citation_count(self) -> int:
        return len(self._entries)

    @property
    def uncited_count(self) -> int:
        return len(self._uncited)

    @property
    def grounding_score(self) -> float:
        """Overall grounding score: ratio of cited to (cited + uncited) claims."""
        total = len(self._entries) + len(self._uncited)
        if total == 0:
            return 0.0
        return len(self._entries) / total

    @property
    def mean_confidence(self) -> float:
        """Mean confidence of all cited entries."""
        if not self._entries:
            return 0.0
        return sum(e.confidence for e in self._entries) / len(self._entries)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the full proof chain for inclusion in deep_think result."""
        return {
            "citations": [e.to_dict() for e in self._entries],
            "uncited_claims": [u.to_dict() for u in self._uncited],
            "citation_count": self.citation_count,
            "uncited_count": self.uncited_count,
            "grounding_score": round(self.grounding_score, 4),
            "mean_confidence": round(self.mean_confidence, 4),
        }

    def __repr__(self) -> str:
        return (
            f"ProofChain(job_id={self.job_id!r}, citations={self.citation_count}, "
            f"uncited={self.uncited_count}, grounding={self.grounding_score:.2f})"
        )
