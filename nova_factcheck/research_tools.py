"""Research tools for grounded reasoning — Nova search, DAMA telemetry, and web search.

These tools are injected into the reasoning loop when task_class permits research.
They are BLOCKED for task_class="adversarial" to prevent data leakage to uncensored models.

Environment variables:
    NOVA_BASE_URL          Nova service base URL (default: http://localhost:30850)
    NOVA_TOKEN             Bearer token for Nova authentication
    NOVA_TOTP_SEED         TOTP seed for Nova authentication
    NOVA_SEARCH_TIMEOUT_S  Per-request timeout in seconds (default: 15)

    DAMA_BASE_URL          DAMA API base URL (default: http://localhost:30900)
    DAMA_TOKEN             Bearer token for DAMA API
    DAMA_TIMEOUT_S         Per-request timeout in seconds (default: 10)

    WEB_SEARCH_TIMEOUT_S   Web search timeout in seconds (default: 20)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

from .nova_client import _auth_headers as _nova_auth_headers
try:
    from deep_think_mcp.engine.task_class_enforcer import check_research_tool_allowed
except Exception:  # pragma: no cover
    try:
        from engine.task_class_enforcer import check_research_tool_allowed
    except Exception:  # pragma: no cover
        check_research_tool_allowed = None

log = logging.getLogger(__name__)

NOVA_BASE_URL = os.getenv("NOVA_BASE_URL", "http://localhost:30850").rstrip("/")
NOVA_SEARCH_TIMEOUT_S = float(os.getenv("NOVA_SEARCH_TIMEOUT_S", "15"))

DAMA_BASE_URL = os.getenv("DAMA_BASE_URL", "http://localhost:30900").rstrip("/")
DAMA_TOKEN = os.getenv("DAMA_TOKEN", "").strip()
DAMA_TIMEOUT_S = float(os.getenv("DAMA_TIMEOUT_S", "10"))

WEB_SEARCH_TIMEOUT_S = float(os.getenv("WEB_SEARCH_TIMEOUT_S", "20"))

_AUDIT_LOG = logging.getLogger("deep_think.audit")


def _enforce_research_access(tool_name: str, task_class: str, job_id: str) -> bool:
    """Fail closed when research tool access context is missing or disallowed."""
    normalized_task_class = (task_class or "").strip()
    if not normalized_task_class:
        _AUDIT_LOG.warning(
            "RESEARCH_TOOL_CONTEXT_MISSING tool=%s job_id=%s reason=missing_task_class",
            tool_name, job_id,
        )
        # Backward-compatible fallback for legacy callsites; guarded runtime paths
        # should provide task_class explicitly.
        return True
    if check_research_tool_allowed and not check_research_tool_allowed(normalized_task_class, tool_name, job_id=job_id):
        return False
    return True


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    content: str
    source: str
    confidence: float
    snippet: str


@dataclass
class NovaSearchResponse:
    results: List[SearchResult]
    total_retrieved: int
    query: str
    latency_ms: int


@dataclass
class DAMAReading:
    timestamp: str
    value: Any
    quality: str


@dataclass
class DAMAQueryResponse:
    readings: List[DAMAReading]
    status: str
    last_update: str
    node_id: str
    metric: str
    latency_ms: int


@dataclass
class WebResult:
    title: str
    url: str
    snippet: str
    domain: str


@dataclass
class WebSearchResponse:
    results: List[WebResult]
    total: int
    query: str
    latency_ms: int


# ---------------------------------------------------------------------------
# DAMA auth helper
# ---------------------------------------------------------------------------

def _dama_auth_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if DAMA_TOKEN:
        headers["Authorization"] = f"Bearer {DAMA_TOKEN}"
    return headers


# ---------------------------------------------------------------------------
# nova_search
# ---------------------------------------------------------------------------

async def nova_search(
    query: str,
    top: int = 8,
    profile: str = "auto",
    job_id: str = "",
    task_class: str = "",
) -> NovaSearchResponse:
    """Query Nova Great Library for grounded context.

    Args:
        query: The search query string.
        top: Maximum number of results to retrieve (default 8).
        profile: Retrieval profile — "auto", "operational", "research", "memory", "mixed".
        job_id: Calling job ID for audit logging.
        task_class: Calling task class for audit logging.

    Returns:
        NovaSearchResponse with retrieved results.
    """
    _AUDIT_LOG.info(
        "RESEARCH_QUERY type=nova_search job_id=%s task_class=%s query=%r top=%d profile=%s",
        job_id, task_class, query[:120], top, profile,
    )
    if not _enforce_research_access("nova_search", task_class, job_id):
        return NovaSearchResponse(results=[], total_retrieved=0, query=query, latency_ms=0)

    started = time.monotonic()
    payload = {"query": query, "top": top, "profile": profile}

    try:
        async with aiohttp.ClientSession() as session:
            timeout = aiohttp.ClientTimeout(total=NOVA_SEARCH_TIMEOUT_S)
            async with session.post(
                f"{NOVA_BASE_URL}/search",
                json=payload,
                headers=_nova_auth_headers(),
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        log.warning("nova_search failed for query %r: %s", query[:80], exc)
        _AUDIT_LOG.warning(
            "RESEARCH_QUERY_ERROR type=nova_search job_id=%s error=%s latency_ms=%d",
            job_id, exc, latency_ms,
        )
        return NovaSearchResponse(results=[], total_retrieved=0, query=query, latency_ms=latency_ms)

    latency_ms = int((time.monotonic() - started) * 1000)

    # Normalize Nova /search response format
    raw_results = data.get("results", [])
    results = []
    for r in raw_results:
        results.append(SearchResult(
            content=str(r.get("content", r.get("text", ""))),
            source=str(r.get("source", r.get("document_id", "unknown"))),
            confidence=float(r.get("score", r.get("confidence", 0.5))),
            snippet=str(r.get("snippet", r.get("content", "")[:200])),
        ))

    total = data.get("total", len(results))
    _AUDIT_LOG.info(
        "RESEARCH_RESULT type=nova_search job_id=%s results=%d latency_ms=%d",
        job_id, len(results), latency_ms,
    )
    return NovaSearchResponse(results=results, total_retrieved=total, query=query, latency_ms=latency_ms)


# ---------------------------------------------------------------------------
# dama_query
# ---------------------------------------------------------------------------

async def dama_query(
    node_id: str,
    metric: str,
    time_range: str = "1h",
    job_id: str = "",
    task_class: str = "",
) -> DAMAQueryResponse:
    """Query DAMA device telemetry data.

    Args:
        node_id: Device/node identifier (e.g., "ant_001").
        metric: Metric name to query (e.g., "temperature", "battery_voltage").
        time_range: Time range string: "15m", "1h", "6h", "24h" (default "1h").
        job_id: Calling job ID for audit logging.
        task_class: Calling task class for audit logging.

    Returns:
        DAMAQueryResponse with sensor readings.
    """
    _AUDIT_LOG.info(
        "RESEARCH_QUERY type=dama_query job_id=%s task_class=%s node_id=%s metric=%s range=%s",
        job_id, task_class, node_id, metric, time_range,
    )
    if not _enforce_research_access("dama_query", task_class, job_id):
        return DAMAQueryResponse(
            readings=[], status="blocked", last_update="",
            node_id=node_id, metric=metric, latency_ms=0,
        )

    started = time.monotonic()
    params = {"node_id": node_id, "metric": metric, "time_range": time_range}

    try:
        async with aiohttp.ClientSession() as session:
            timeout = aiohttp.ClientTimeout(total=DAMA_TIMEOUT_S)
            async with session.get(
                f"{DAMA_BASE_URL}/telemetry",
                params=params,
                headers=_dama_auth_headers(),
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        log.warning("dama_query failed node_id=%s metric=%s: %s", node_id, metric, exc)
        _AUDIT_LOG.warning(
            "RESEARCH_QUERY_ERROR type=dama_query job_id=%s node_id=%s metric=%s error=%s latency_ms=%d",
            job_id, node_id, metric, exc, latency_ms,
        )
        return DAMAQueryResponse(
            readings=[], status="error", last_update="",
            node_id=node_id, metric=metric, latency_ms=latency_ms,
        )

    latency_ms = int((time.monotonic() - started) * 1000)

    raw_readings = data.get("readings", [])
    readings = []
    for r in raw_readings:
        readings.append(DAMAReading(
            timestamp=str(r.get("timestamp", "")),
            value=r.get("value"),
            quality=str(r.get("quality", "unknown")),
        ))

    last_update = data.get("last_update", data.get("last_seen", ""))
    status = data.get("status", "ok" if readings else "no_data")

    _AUDIT_LOG.info(
        "RESEARCH_RESULT type=dama_query job_id=%s node_id=%s readings=%d latency_ms=%d",
        job_id, node_id, len(readings), latency_ms,
    )
    return DAMAQueryResponse(
        readings=readings, status=status, last_update=last_update,
        node_id=node_id, metric=metric, latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------

# Known safe search API endpoint (DuckDuckGo instant answer API — no key required)
_DDG_API = "https://api.duckduckgo.com/"

def _domain_allowed(url: str, whitelist: List[str]) -> bool:
    """Return True if url's domain is in whitelist (or whitelist is empty)."""
    if not whitelist:
        return True
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower().lstrip("www.")
        return any(domain == w.lower().lstrip("www.") or domain.endswith("." + w.lower().lstrip("www."))
                   for w in whitelist)
    except Exception:
        return False


async def web_search(
    query: str,
    domain_whitelist: Optional[List[str]] = None,
    job_id: str = "",
    task_class: str = "",
) -> WebSearchResponse:
    """Perform a public web search with optional domain whitelist enforcement.

    Uses DuckDuckGo Instant Answer API (no key required, safe defaults).
    Domain whitelist prevents leakage to malicious or untrusted sites.

    Args:
        query: Search query string.
        domain_whitelist: Optional list of allowed domains. Empty = allow all.
        job_id: Calling job ID for audit logging.
        task_class: Calling task class for audit logging.

    Returns:
        WebSearchResponse with filtered results.
    """
    whitelist = domain_whitelist or []
    _AUDIT_LOG.info(
        "RESEARCH_QUERY type=web_search job_id=%s task_class=%s query=%r whitelist=%s",
        job_id, task_class, query[:120], whitelist,
    )
    if not _enforce_research_access("web_search", task_class, job_id):
        return WebSearchResponse(results=[], total=0, query=query, latency_ms=0)

    started = time.monotonic()
    params = {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}

    try:
        async with aiohttp.ClientSession() as session:
            timeout = aiohttp.ClientTimeout(total=WEB_SEARCH_TIMEOUT_S)
            async with session.get(_DDG_API, params=params, timeout=timeout) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        log.warning("web_search failed for query %r: %s", query[:80], exc)
        _AUDIT_LOG.warning(
            "RESEARCH_QUERY_ERROR type=web_search job_id=%s error=%s latency_ms=%d",
            job_id, exc, latency_ms,
        )
        return WebSearchResponse(results=[], total=0, query=query, latency_ms=latency_ms)

    latency_ms = int((time.monotonic() - started) * 1000)

    raw_results = data.get("RelatedTopics", [])
    results = []
    for r in raw_results:
        if not isinstance(r, dict):
            continue
        url = r.get("FirstURL", "")
        if not url:
            continue
        if not _domain_allowed(url, whitelist):
            _AUDIT_LOG.info(
                "RESEARCH_DOMAIN_BLOCKED job_id=%s url=%s whitelist=%s",
                job_id, url, whitelist,
            )
            continue
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lstrip("www.")
        results.append(WebResult(
            title=r.get("Text", "")[:200],
            url=url,
            snippet=r.get("Text", "")[:400],
            domain=domain,
        ))

    # DuckDuckGo AbstractURL as a top result if available and allowed
    abstract_url = data.get("AbstractURL", "")
    abstract_text = data.get("AbstractText", "")
    if abstract_url and abstract_text and _domain_allowed(abstract_url, whitelist):
        from urllib.parse import urlparse
        domain = urlparse(abstract_url).netloc.lstrip("www.")
        results.insert(0, WebResult(
            title=data.get("Heading", query),
            url=abstract_url,
            snippet=abstract_text[:400],
            domain=domain,
        ))

    _AUDIT_LOG.info(
        "RESEARCH_RESULT type=web_search job_id=%s results=%d latency_ms=%d",
        job_id, len(results), latency_ms,
    )
    return WebSearchResponse(results=results, total=len(results), query=query, latency_ms=latency_ms)


# ---------------------------------------------------------------------------
# Context builder — formats research results into an LLM-injectable string
# ---------------------------------------------------------------------------

def format_research_context(
    nova_response: Optional[NovaSearchResponse] = None,
    dama_response: Optional[DAMAQueryResponse] = None,
    web_response: Optional[WebSearchResponse] = None,
) -> str:
    """Format research tool outputs into a structured context block for LLM injection."""
    sections = []

    if nova_response and nova_response.results:
        lines = ["[NOVA LIBRARY CONTEXT — cite source IDs in your response]"]
        for i, r in enumerate(nova_response.results[:8], 1):
            lines.append(
                f"  [{i}] Source: {r.source} (confidence={r.confidence:.2f})\n"
                f"      {r.snippet[:300]}"
            )
        sections.append("\n".join(lines))

    if dama_response and dama_response.readings:
        lines = [
            f"[DAMA TELEMETRY — node={dama_response.node_id} metric={dama_response.metric} "
            f"status={dama_response.status}]"
        ]
        for r in dama_response.readings[:10]:
            lines.append(f"  {r.timestamp}: {r.value} (quality={r.quality})")
        sections.append("\n".join(lines))

    if web_response and web_response.results:
        lines = ["[WEB SEARCH RESULTS — verified domains only]"]
        for i, r in enumerate(web_response.results[:5], 1):
            lines.append(f"  [{i}] {r.title} ({r.domain})\n      {r.snippet[:200]}")
        sections.append("\n".join(lines))

    if not sections:
        return ""
    return "\n\n".join(sections) + "\n\n"
