"""NovaVerificationClient — async HTTP client for Nova /verify endpoint.

Translates Nova verdicts (SUPPORTED / CONTRADICTED / INSUFFICIENT_EVIDENCE)
to the pipeline's canonical statuses (TRUE / FALSE / UNCERTAIN).

Auth uses Bearer token + TOTP, mirroring nova_mcp/core.py.
All settings are read from environment variables so no secrets appear in code.

REQUIRED Environment Variables (for authentication):
    NOVA_TOKEN         Bearer token for Nova API (required for auth)
                       Example: "nova-your-token-here"
    NOVA_TOTP_SEED     Base32-encoded TOTP seed (required for auth)
                       Example: "YOUR_BASE32_TOTP_SEED"
                       
OPTIONAL Environment Variables:
    NOVA_BASE_URL      Base URL of Nova service 
                       Required
    NOVA_VERIFY_TIMEOUT_S  Per-request timeout in seconds
                           Default: 20
    NOVA_VERIFY_RETRIES    Number of retries on transient errors
                           Default: 2

WARNING: If NOVA_TOKEN or NOVA_TOTP_SEED are empty, all verification calls will 
fail with 401 Unauthorized. This container must have these set in its environment 
(ConfigMap, Secret, or .env file) before starting.
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


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default


def _load_nova_settings() -> dict:
    """Read Nova settings at runtime so env loading order does not poison auth."""
    return {
        "base_url": os.getenv("NOVA_BASE_URL", "").rstrip("/"),
        "token": os.getenv("NOVA_TOKEN", "").strip(),
        "totp_seed": os.getenv("NOVA_TOTP_SEED", "").strip(),
        "timeout_s": _env_float("NOVA_VERIFY_TIMEOUT_S", 20.0),
        "retries": _env_int("NOVA_VERIFY_RETRIES", 2),
    }


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
    error_kind: str = ""                # auth_failed | auth_config_missing | timeout | network


# ---------------------------------------------------------------------------
# TOTP helper (no dependency on nova_mcp)
# ---------------------------------------------------------------------------

def _totp_now(seed: str) -> str:
    """Generate current TOTP value from NOVA_TOTP_SEED."""
    try:
        import pyotp  # type: ignore
        return pyotp.TOTP(seed).now()
    except Exception as exc:
        log.warning("TOTP generation failed: %s", exc)
        return ""


def _auth_headers() -> Dict[str, str]:
    """Runtime auth-header helper retained for compatibility with research tools."""
    settings = _load_nova_settings()
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if settings["token"]:
        headers["Authorization"] = f"Bearer {settings['token']}"
    if settings["totp_seed"]:
        headers["X-TOTP-Challenge"] = _totp_now(settings["totp_seed"])
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
        base_url: Optional[str] = None,
        timeout_s: Optional[float] = None,
        retries: Optional[int] = None,
        profile: str = "auto",
        top: int = 8,
        token: Optional[str] = None,
        totp_seed: Optional[str] = None,
    ) -> None:
        settings = _load_nova_settings()
        self.base_url = (base_url or settings["base_url"]).rstrip("/")
        self.timeout_s = timeout_s if timeout_s is not None else settings["timeout_s"]
        self.retries = retries if retries is not None else settings["retries"]
        self.profile = profile
        self.top = top
        self._token = token if token is not None else settings["token"]
        self._totp_seed = totp_seed if totp_seed is not None else settings["totp_seed"]
        self._session: Optional[aiohttp.ClientSession] = None

        if not self._token or not self._totp_seed:
            log.warning(
                "Nova authentication credentials missing at client init: NOVA_TOKEN=%s, NOVA_TOTP_SEED=%s",
                "SET" if self._token else "NOT SET",
                "SET" if self._totp_seed else "NOT SET",
            )

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

        if not self._token or not self._totp_seed:
            return ClaimVerificationResult(
                claim_id=claim_id,
                claim_text=claim_text,
                status=VerificationStatus.ERROR,
                nova_confidence=0.0,
                reasoning="Nova authentication credentials missing",
                evidence=[],
                latency_ms=0,
                error_kind="auth_config_missing",
            )

        payload = {
            "claim": claim_text[:1024],
            "top": self.top,
            "profile": self.profile,
        }

        last_exc: Optional[Exception] = None
        error_kind = "network"
        for attempt in range(self.retries + 1):
            try:
                result = await self._post_verify(payload)
                latency_ms = int((time.monotonic() - started) * 1000)
                return self._parse_response(result, claim_id, claim_text, latency_ms)
            except aiohttp.ClientResponseError as exc:
                last_exc = exc
                if exc.status == 401:
                    latency_ms = int((time.monotonic() - started) * 1000)
                    log.warning("Nova authentication failed for claim %r", claim_id)
                    return ClaimVerificationResult(
                        claim_id=claim_id,
                        claim_text=claim_text,
                        status=VerificationStatus.ERROR,
                        nova_confidence=0.0,
                        reasoning="Nova authentication failed",
                        evidence=[],
                        latency_ms=latency_ms,
                        error_kind="auth_failed",
                    )
                if attempt < self.retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    log.debug("Nova verify retry %d/%d for claim %r: %s", attempt + 1, self.retries, claim_id, exc)
            except asyncio.TimeoutError as exc:
                last_exc = exc
                error_kind = "timeout"
                if attempt < self.retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    log.debug("Nova verify retry %d/%d for claim %r: %s", attempt + 1, self.retries, claim_id, exc)
            except aiohttp.ClientError as exc:
                last_exc = exc
                error_kind = "network"
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
            reasoning=f"{error_kind.title()} error: {last_exc}",
            evidence=[],
            latency_ms=latency_ms,
            error_kind=error_kind,
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
        headers = self._auth_headers()
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

    def _auth_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._totp_seed:
            headers["X-TOTP-Challenge"] = _totp_now(self._totp_seed)
        return headers

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
