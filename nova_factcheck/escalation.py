"""Human escalation queue for unverifiable deep-think claims.

Claims are considered "unverifiable" when:
  - Nova returns ERROR (network / auth failure) for every attempt
  - Claim confidence_in_text is below the low-confidence threshold AND
    Nova returns UNCERTAIN

The queue is in-process and survives only for the lifetime of the worker process.
External callers (e.g. a monitoring endpoint) can drain it with drain().

Thread safety: uses asyncio.Queue which is coroutine-safe.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Any

log = logging.getLogger(__name__)

# Claims with in-text confidence below this threshold AND UNCERTAIN Nova status
# are escalated.
LOW_CONFIDENCE_THRESHOLD: float = 0.4


@dataclass
class EscalationItem:
    """A claim queued for human review."""

    claim_id: str
    claim_text: str
    claim_type: str
    nova_status: str         # "ERROR" or "UNCERTAIN"
    nova_confidence: float
    confidence_in_text: float
    reason: str              # why it was escalated
    job_id: str
    escalated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "claim_text": self.claim_text,
            "claim_type": self.claim_type,
            "nova_status": self.nova_status,
            "nova_confidence": self.nova_confidence,
            "confidence_in_text": self.confidence_in_text,
            "reason": self.reason,
            "job_id": self.job_id,
            "escalated_at": self.escalated_at,
        }


class HumanEscalationQueue:
    """Async queue for claims that require human review.

    Usage::

        queue = HumanEscalationQueue()
        await queue.put(item)
        items = queue.drain()  # non-blocking snapshot
    """

    def __init__(self, max_size: int = 1000) -> None:
        self._queue: asyncio.Queue[EscalationItem] = asyncio.Queue(maxsize=max_size)
        self._overflow: List[EscalationItem] = []

    async def put(self, item: EscalationItem) -> None:
        """Add an item.  If the queue is full, stores in overflow list and logs a warning."""
        try:
            self._queue.put_nowait(item)
            log.info(
                "Escalated claim %r (nova=%s, confidence=%.2f): %s",
                item.claim_id,
                item.nova_status,
                item.confidence_in_text,
                item.reason,
            )
        except asyncio.QueueFull:
            self._overflow.append(item)
            log.warning(
                "Escalation queue full; claim %r stored in overflow (total overflow=%d)",
                item.claim_id,
                len(self._overflow),
            )

    def drain(self) -> List[EscalationItem]:
        """Non-blocking snapshot of all queued items.  Empties the queue."""
        items: List[EscalationItem] = []
        while not self._queue.empty():
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        items.extend(self._overflow)
        self._overflow.clear()
        return items

    def size(self) -> int:
        return self._queue.qsize() + len(self._overflow)


# Module-level singleton — shared across worker tasks in the same process.
_GLOBAL_QUEUE: "HumanEscalationQueue | None" = None


def get_escalation_queue() -> HumanEscalationQueue:
    """Return (or lazily create) the module-level escalation queue."""
    global _GLOBAL_QUEUE
    if _GLOBAL_QUEUE is None:
        _GLOBAL_QUEUE = HumanEscalationQueue()
    return _GLOBAL_QUEUE
