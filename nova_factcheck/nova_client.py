"""NovaVerificationClient — async HTTP client for Nova /verify endpoint.

Translates Nova verdicts (SUPPORTED / CONTRADICTED / INSUFFICIENT_EVIDENCE)
to the pipeline's canonical statuses (TRUE / FALSE / UNCERTAIN).

Auth uses Bearer token + TOTP, mirroring nova_mcp/core.py.
All settings are read from environment variables so no secrets appear in code.

Environment variables:
    NOVA_BASE_URL      Base URL of Nova service (default: http://[REDACTED_INTERNAL_IP]:30850)
    NOVA_TOKEN         Bearer token
    NOVA_TOTP_SEED     TOTP seed (base32)
    NOVA_VERIFY_TIMEOUT_S  Per-request timeout in seconds (default: 20)
    NOVA_VERIFY_RETRIES    Number of retries on transient errors (default: 2)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import aiohttp

log = logging.getLogger(__name__)

NOVA_BASE_URL = os.getenv("NOVA_BASE_URL", "http://[REDACTED_INTERNAL_IP]:30850").rstrip("/")
NOVA_TOKEN = os.getenv("NOVA_TOKEN", "").strip()
NOVA_TOTP_SEED = os.getenv("NOVA_TOTP_SEED", "").strip()
NOVA_VERIFY_TIMEOUT_S = float(os.getenv("NOVA_VERIFY_TIMEOUT_S", "20"))
NOVA_VERIFY_RETRIES = int(os.getenv("NOVA_VERIFY_RETRIES", "2"))


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class VerificationStatus(str, Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNCERTAIN = "UNCERTAIN"
    ERROR = "ERROR"  # Network / auth failure — treated like UNCERTAIN in pipeline


@dataclass
class ClaimVerificationResult:
    """Result of verifying a single claim against Nova."""

    claim_id: str
    claim_text: str
    status: VerificationStatus          # TRUE / FALSE / UNCERTAIN / ERROR
    nova_confidence: float              # Nova's confidence (0–1)
    reasoning: str                      # Nova's reasoning snippet
    evidence: List[Dict[str, Any]]      # supporting/contradicting evidence chunks
    latency_ms: int


# ---------------------------------------------------------------------------
# TOTP helper (no dependency on nova_mcp)
# ---------------------------------------------------------------------------

def _totp_now() -> str:
    """Generate current TOTP value from NOVA_TOTP_SEED."""
    try:
        import pyotp  # type: ignore
        return pyotp.TOTP(NOVA_TOTP_SEED).now()
    except Exception as exc:
        log.warning("TOTP generation failed: %s", exc)
        return ""


def _auth_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if NOVA_TOKEN:
        headers["Authorization"] = f"Bearer {NOVA_TOKEN}"
    if NOVA_TOTP_SEED:
        headers["X-TOTP-Challenge"] = _totp_now()
    return headers


# ---------------------------------------------------------------------------
# Verdict mapping
# ---------------------------------------------------------------------------

def _map_verdict(verdict: str) -> VerificationStatus:
    v = verdict.upper().strip()
    if v == "SUPPORTED":
        return VerificationStatus.TRUE
    if v == "CONTRADICTED":
        return VerificationStatus.FALSE
    # INSUFFICIENT_EVIDENCE, empty, or unknown
    return VerificationStatus.UNCERTAIN


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class NovaVerificationClient:
    """Async client for Nova's /verify endpoint.

    Usage::

        async with NovaVerificationClient() as client:
            result = await client.verify("Python was created in 1991.")
    """

    def __init__(
        self,
        base_url: str = NOVA_BASE_URL,
        timeout_s: float = NOVA_VERIFY_TIMEOUT_S,
        retries: int = NOVA_VERIFY_RETRIES,
        profile: str = "auto",
        top: int = 8,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.retries = retries
        self.profile = profile
        self.top = top
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "NovaVerificationClient":
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def verify(self, claim_text: str, claim_id: str = "") -> ClaimVerificationResult:
        """Verify a single claim against Nova's Great Library.

        Returns a ClaimVerificationResult.  On network/auth failure,
        status is set to ERROR and nova_confidence to 0.0.
        """
        claim_id = claim_id or claim_text[:16].replace(" ", "_")
        started = time.monotonic()

        payload = {
            "claim": claim_text[:1024],
            "top": self.top,
            "profile": self.profile,
        }

        last_exc: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                result = await self._post_verify(payload)
                latency_ms = int((time.monotonic() - started) * 1000)
                return self._parse_response(result, claim_id, claim_text, latency_ms)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < self.retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    log.debug("Nova verify retry %d/%d for claim %r: %s", attempt + 1, self.retries, claim_id, exc)

        latency_ms = int((time.monotonic() - started) * 1000)
        log.warning("Nova verify failed for claim %r after %d attempts: %s", claim_id, self.retries + 1, last_exc)
        return ClaimVerificationResult(
            claim_id=claim_id,
            claim_text=claim_text,
            status=VerificationStatus.ERROR,
            nova_confidence=0.0,
            reasoning=f"Network error: {last_exc}",
            evidence=[],
            latency_ms=latency_ms,
        )

    async def verify_batch(
        self,
        claims: List[tuple[str, str]],  # List of (claim_id, claim_text)
        max_concurrent: int = 5,
    ) -> List[ClaimVerificationResult]:
        """Verify multiple claims concurrently.

        Args:
            claims: list of (claim_id, claim_text) tuples
            max_concurrent: maximum simultaneous requests

        Returns:
            Results in the same order as *claims*.
        """
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _limited(cid: str, ctext: str) -> ClaimVerificationResult:
            async with semaphore:
                return await self.verify(ctext, claim_id=cid)

        tasks = [_limited(cid, ctext) for cid, ctext in claims]
        return list(await asyncio.gather(*tasks))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _post_verify(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST to /verify.  Raises aiohttp.ClientError or asyncio.TimeoutError on failure."""
        url = f"{self.base_url}/verify"
        headers = _auth_headers()
        timeout = aiohttp.ClientTimeout(total=self.timeout_s)

        session = self._session
        owned = session is None
        if owned:
            session = aiohttp.ClientSession()

        try:
            async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
                if resp.status == 401:
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history,
                        status=401, message="Nova authentication failed",
                    )
                resp.raise_for_status()
                return await resp.json(content_type=None)
        finally:
            if owned:
                await session.close()

    def _parse_response(
        self,
        data: Dict[str, Any],
        claim_id: str,
        claim_text: str,
        latency_ms: int,
    ) -> ClaimVerificationResult:
        """Parse Nova /verify response into ClaimVerificationResult."""
        verdict_raw = data.get("verdict", "INSUFFICIENT_EVIDENCE")
        status = _map_verdict(verdict_raw)

        raw_conf = data.get("confidence", 0.0)
        try:
            nova_confidence = float(raw_conf)
        except (TypeError, ValueError):
            nova_confidence = 0.0
        nova_confidence = max(0.0, min(1.0, nova_confidence))

        reasoning = str(data.get("reasoning", ""))[:500]
        evidence = data.get("evidence", [])
        if not isinstance(evidence, list):
            evidence = []

        return ClaimVerificationResult(
            claim_id=claim_id,
            claim_text=claim_text,
            status=status,
            nova_confidence=nova_confidence,
            reasoning=reasoning,
            evidence=evidence[:10],  # cap to avoid bloat
            latency_ms=latency_ms,
        )
