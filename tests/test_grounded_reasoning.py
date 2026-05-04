"""Grounded Reasoning Framework — comprehensive test suite.

Tests (25 total):
 1.  nova_search returns NovaSearchResponse with correct shape
 2.  nova_search handles network error gracefully
 3.  dama_query returns DAMAQueryResponse with correct shape
 4.  dama_query handles HTTP error gracefully
 5.  web_search returns WebSearchResponse with correct shape
 6.  web_search domain whitelist blocks disallowed URLs
 7.  web_search allows all domains when whitelist is empty
 8.  format_research_context builds Nova section
 9.  format_research_context builds DAMA section
10.  format_research_context returns empty string for empty inputs
11.  ProofChain.cite records entries correctly
12.  ProofChain.flag_uncited records uncited claims
13.  ProofChain grounding_score computed correctly
14.  ProofChain.extract_citations_from_text auto-detects bracket references
15.  ProofChain.to_dict serializes all fields
16.  is_abliteration_model correctly identifies abliteration patterns
17.  enforce_task_class: adversarial + cloud provider → TaskClassViolation
18.  enforce_task_class: adversarial + ollama → passes
19.  enforce_task_class: research + abliteration model → TaskClassViolation
20.  enforce_task_class: research + trusted model → passes
21.  check_research_tool_allowed: adversarial blocks all tools
22.  check_research_tool_allowed: general blocks dama_query and web_search
23.  check_research_tool_allowed: research allows all tools
24.  get_allowed_tools returns correct sets per task class
25.  filter_adversarial_output strips Nova context blocks
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from nova_factcheck.research_tools import (
    nova_search,
    dama_query,
    web_search,
    format_research_context,
    NovaSearchResponse,
    DAMAQueryResponse,
    WebSearchResponse,
    SearchResult,
    DAMAReading,
    WebResult,
    _domain_allowed,
)

from engine.proof_chain import (
    ProofChain,
    ProofEntry,
    UncitedClaim,
    SOURCE_TYPE_NOVA,
    SOURCE_TYPE_DAMA,
    SOURCE_TYPE_WEB,
    SOURCE_TYPE_INTERNAL,
)

from engine.task_class_enforcer import (
    TaskClassViolation,
    enforce_task_class,
    check_research_tool_allowed,
    get_allowed_tools,
    filter_adversarial_output,
    is_abliteration_model,
    ABLITERATION_MODEL_PATTERNS,
    RESEARCH_ENABLED_TASK_CLASSES,
    RESEARCH_BLOCKED_TASK_CLASSES,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _make_nova_response_payload(n: int = 3) -> dict:
    return {
        "results": [
            {
                "content": f"Content of result {i}",
                "source": f"doc_{i:03d}",
                "score": 0.9 - i * 0.05,
                "snippet": f"Snippet {i}",
            }
            for i in range(n)
        ],
        "total": n,
    }


def _make_dama_response_payload(n: int = 3) -> dict:
    return {
        "readings": [
            {"timestamp": f"2024-01-01T00:0{i}:00Z", "value": 22.5 + i, "quality": "good"}
            for i in range(n)
        ],
        "status": "ok",
        "last_update": "2024-01-01T00:05:00Z",
    }


def _make_ddg_response_payload() -> dict:
    return {
        "Heading": "Python Programming",
        "AbstractText": "Python is a high-level programming language.",
        "AbstractURL": "https://python.org/about",
        "RelatedTopics": [
            {"Text": "Python Tutorial", "FirstURL": "https://docs.python.org/tutorial"},
            {"Text": "Python Reference", "FirstURL": "https://docs.python.org/reference"},
            {"Text": "External Site", "FirstURL": "https://evil.example.com/python"},
        ],
    }


# ===========================================================================
# 1. nova_search returns NovaSearchResponse with correct shape
# ===========================================================================

@pytest.mark.asyncio
async def test_nova_search_correct_shape():
    payload = _make_nova_response_payload(3)

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=payload)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("nova_factcheck.research_tools.aiohttp.ClientSession", return_value=mock_session):
        result = await nova_search("test query", top=3, job_id="j1")

    assert isinstance(result, NovaSearchResponse)
    assert result.query == "test query"
    assert result.total_retrieved == 3
    assert len(result.results) == 3
    assert result.results[0].source == "doc_000"
    assert result.results[0].confidence == pytest.approx(0.9)
    assert result.latency_ms >= 0


# ===========================================================================
# 2. nova_search handles network error gracefully
# ===========================================================================

@pytest.mark.asyncio
async def test_nova_search_network_error():
    import aiohttp

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.post = MagicMock(side_effect=aiohttp.ClientConnectionError("refused"))

    with patch("nova_factcheck.research_tools.aiohttp.ClientSession", return_value=mock_session):
        result = await nova_search("failed query", job_id="j2")

    assert isinstance(result, NovaSearchResponse)
    assert result.results == []
    assert result.total_retrieved == 0


# ===========================================================================
# 3. dama_query returns DAMAQueryResponse with correct shape
# ===========================================================================

@pytest.mark.asyncio
async def test_dama_query_correct_shape():
    payload = _make_dama_response_payload(3)

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=payload)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("nova_factcheck.research_tools.aiohttp.ClientSession", return_value=mock_session):
        result = await dama_query("ant_001", "temperature", time_range="1h", job_id="j3")

    assert isinstance(result, DAMAQueryResponse)
    assert result.node_id == "ant_001"
    assert result.metric == "temperature"
    assert result.status == "ok"
    assert len(result.readings) == 3
    assert result.readings[0].quality == "good"
    assert result.readings[0].value == pytest.approx(22.5)


# ===========================================================================
# 4. dama_query handles HTTP error gracefully
# ===========================================================================

@pytest.mark.asyncio
async def test_dama_query_http_error():
    import aiohttp

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.get = MagicMock(side_effect=aiohttp.ClientError("timeout"))

    with patch("nova_factcheck.research_tools.aiohttp.ClientSession", return_value=mock_session):
        result = await dama_query("ant_001", "temperature", job_id="j4")

    assert isinstance(result, DAMAQueryResponse)
    assert result.readings == []
    assert result.status == "error"


# ===========================================================================
# 5. web_search returns WebSearchResponse with correct shape
# ===========================================================================

@pytest.mark.asyncio
async def test_web_search_correct_shape():
    payload = _make_ddg_response_payload()

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=payload)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("nova_factcheck.research_tools.aiohttp.ClientSession", return_value=mock_session):
        result = await web_search("python", job_id="j5")

    assert isinstance(result, WebSearchResponse)
    assert result.query == "python"
    # python.org (abstract) + 2 docs.python.org results (evil.example.com excluded when no whitelist)
    assert result.total >= 1
    domains = [r.domain for r in result.results]
    assert any("python.org" in d for d in domains)


# ===========================================================================
# 6. web_search domain whitelist blocks disallowed URLs
# ===========================================================================

@pytest.mark.asyncio
async def test_web_search_whitelist_blocks_disallowed():
    payload = _make_ddg_response_payload()

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=payload)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("nova_factcheck.research_tools.aiohttp.ClientSession", return_value=mock_session):
        result = await web_search("python", domain_whitelist=["python.org"], job_id="j6")

    # Only python.org results should be present
    for r in result.results:
        assert "python.org" in r.domain, f"Expected python.org, got {r.domain}"
    evil_urls = [r.url for r in result.results if "evil" in r.url]
    assert evil_urls == []


# ===========================================================================
# 7. web_search allows all domains when whitelist is empty
# ===========================================================================

def test_domain_allowed_empty_whitelist():
    assert _domain_allowed("https://evil.example.com", []) is True
    assert _domain_allowed("https://docs.python.org", []) is True


def test_domain_allowed_with_whitelist():
    assert _domain_allowed("https://docs.python.org/tutorial", ["python.org"]) is True
    assert _domain_allowed("https://evil.example.com", ["python.org"]) is False
    assert _domain_allowed("https://sub.python.org", ["python.org"]) is True


# ===========================================================================
# 8. format_research_context builds Nova section
# ===========================================================================

def test_format_research_context_nova_section():
    nova_resp = NovaSearchResponse(
        results=[
            SearchResult(content="c1", source="src1", confidence=0.9, snippet="Snippet one"),
            SearchResult(content="c2", source="src2", confidence=0.7, snippet="Snippet two"),
        ],
        total_retrieved=2,
        query="test",
        latency_ms=50,
    )
    ctx = format_research_context(nova_response=nova_resp)
    assert "[NOVA LIBRARY CONTEXT" in ctx
    assert "src1" in ctx
    assert "Snippet one" in ctx
    assert "0.90" in ctx


# ===========================================================================
# 9. format_research_context builds DAMA section
# ===========================================================================

def test_format_research_context_dama_section():
    dama_resp = DAMAQueryResponse(
        readings=[
            DAMAReading(timestamp="2024-01-01T00:00:00Z", value=23.5, quality="good"),
        ],
        status="ok",
        last_update="2024-01-01T00:00:00Z",
        node_id="ant_001",
        metric="temperature",
        latency_ms=20,
    )
    ctx = format_research_context(dama_response=dama_resp)
    assert "[DAMA TELEMETRY" in ctx
    assert "ant_001" in ctx
    assert "23.5" in ctx
    assert "good" in ctx


# ===========================================================================
# 10. format_research_context returns empty string for empty inputs
# ===========================================================================

def test_format_research_context_empty():
    ctx = format_research_context()
    assert ctx == ""

    ctx2 = format_research_context(
        nova_response=NovaSearchResponse(results=[], total_retrieved=0, query="", latency_ms=0),
    )
    assert ctx2 == ""


# ===========================================================================
# 11. ProofChain.cite records entries correctly
# ===========================================================================

def test_proof_chain_cite():
    chain = ProofChain(job_id="j1", task_class="research")
    entry = chain.cite("Python was created in 1991", SOURCE_TYPE_NOVA, "doc_042", confidence=0.9)

    assert isinstance(entry, ProofEntry)
    assert entry.claim == "Python was created in 1991"
    assert entry.source_type == SOURCE_TYPE_NOVA
    assert entry.source_id == "doc_042"
    assert entry.confidence == pytest.approx(0.9)
    assert chain.citation_count == 1


# ===========================================================================
# 12. ProofChain.flag_uncited records uncited claims
# ===========================================================================

def test_proof_chain_flag_uncited():
    chain = ProofChain(job_id="j2")
    uc = chain.flag_uncited("The sky is green", pass_num=2)

    assert isinstance(uc, UncitedClaim)
    assert uc.claim == "The sky is green"
    assert uc.pass_num == 2
    assert chain.uncited_count == 1


# ===========================================================================
# 13. ProofChain grounding_score computed correctly
# ===========================================================================

def test_proof_chain_grounding_score():
    chain = ProofChain()
    assert chain.grounding_score == 0.0  # no claims yet

    chain.cite("claim 1", SOURCE_TYPE_NOVA, "s1")
    chain.cite("claim 2", SOURCE_TYPE_NOVA, "s2")
    chain.cite("claim 3", SOURCE_TYPE_WEB, "url1")
    chain.flag_uncited("uncited claim")

    # 3 cited, 1 uncited → 3/4 = 0.75
    assert chain.grounding_score == pytest.approx(0.75)


# ===========================================================================
# 14. ProofChain.extract_citations_from_text auto-detects bracket references
# ===========================================================================

def test_proof_chain_extract_citations_bracket_refs():
    chain = ProofChain(job_id="j3")
    sources = [
        {"source": "doc_001", "confidence": 0.9, "source_type": "nova"},
        {"source": "doc_002", "confidence": 0.75, "source_type": "nova"},
    ]
    text = "According to [1] Python was created by Guido. Also see [2] for reference."
    count = chain.extract_citations_from_text(text, source_results=sources, pass_num=1)

    assert count == 2
    assert chain.citation_count == 2
    sources_found = {e.source_id for e in chain._entries}
    assert "doc_001" in sources_found
    assert "doc_002" in sources_found


# ===========================================================================
# 15. ProofChain.to_dict serializes all fields
# ===========================================================================

def test_proof_chain_to_dict():
    chain = ProofChain(job_id="j4", task_class="research")
    chain.cite("claim A", SOURCE_TYPE_NOVA, "src_a", confidence=0.88, pass_num=1)
    chain.flag_uncited("claim B", pass_num=2)

    d = chain.to_dict()
    assert "citations" in d
    assert "uncited_claims" in d
    assert d["citation_count"] == 1
    assert d["uncited_count"] == 1
    assert 0.0 <= d["grounding_score"] <= 1.0
    assert d["citations"][0]["source_type"] == SOURCE_TYPE_NOVA
    assert d["uncited_claims"][0]["requires_verification"] is True


# ===========================================================================
# 16. is_abliteration_model correctly identifies abliteration patterns
# ===========================================================================

def test_is_abliteration_model_patterns():
    assert is_abliteration_model("mistral-abliterated:7b") is True
    assert is_abliteration_model("dolphin-mistral:latest") is True
    assert is_abliteration_model("wizard-uncensored:13b") is True
    assert is_abliteration_model("dolphin2.6-mistral-7b") is True
    assert is_abliteration_model("manticore-13b") is True

    # Clean models should not match
    assert is_abliteration_model("llama3.1:8b") is False
    assert is_abliteration_model("phi4-mini:latest") is False
    assert is_abliteration_model("qwen3.5:27b") is False
    assert is_abliteration_model("claude-sonnet-4.6") is False


# ===========================================================================
# 17. enforce_task_class: adversarial + cloud provider → TaskClassViolation
# ===========================================================================

def test_enforce_adversarial_blocks_cloud_provider():
    with pytest.raises(TaskClassViolation) as exc_info:
        enforce_task_class(
            task_class="adversarial",
            provider="anthropic",
            models=["claude-sonnet-4-6", "claude-opus-4-7"],
            job_id="j17",
        )
    assert "cloud provider" in str(exc_info.value).lower() or "anthropic" in str(exc_info.value)


def test_enforce_adversarial_blocks_copilot_provider():
    with pytest.raises(TaskClassViolation):
        enforce_task_class(
            task_class="adversarial",
            provider="copilot",
            models=["claude-sonnet-4.6"],
            job_id="j17b",
        )


# ===========================================================================
# 18. enforce_task_class: adversarial + ollama → passes
# ===========================================================================

def test_enforce_adversarial_allows_ollama():
    # Should not raise
    enforce_task_class(
        task_class="adversarial",
        provider="ollama",
        models=["phi4-mini:latest", "llama3.1:8b"],
        job_id="j18",
    )


def test_enforce_adversarial_logs_abliteration_usage():
    # Abliterated model on ollama in adversarial mode should be allowed (just logged)
    enforce_task_class(
        task_class="adversarial",
        provider="ollama",
        models=["mistral-abliterated:7b"],
        job_id="j18b",
    )


# ===========================================================================
# 19. enforce_task_class: research + abliteration model → TaskClassViolation
# ===========================================================================

def test_enforce_research_blocks_abliteration_model():
    with pytest.raises(TaskClassViolation) as exc_info:
        enforce_task_class(
            task_class="research",
            provider="ollama",
            models=["dolphin-mistral:latest"],
            job_id="j19",
        )
    assert "abliteration" in str(exc_info.value).lower() or "dolphin" in str(exc_info.value)


# ===========================================================================
# 20. enforce_task_class: research + trusted model → passes
# ===========================================================================

def test_enforce_research_allows_trusted_model():
    # Should not raise
    enforce_task_class(
        task_class="research",
        provider="copilot",
        models=["claude-sonnet-4.6", "claude-opus-4.7"],
        job_id="j20",
    )
    enforce_task_class(
        task_class="research",
        provider="ollama",
        models=["llama3.1:8b", "phi4-mini:latest"],
        job_id="j20b",
    )


# ===========================================================================
# 21. check_research_tool_allowed: adversarial blocks all tools
# ===========================================================================

def test_adversarial_blocks_all_research_tools():
    for tool in ("nova_search", "dama_query", "web_search"):
        result = check_research_tool_allowed("adversarial", tool, job_id="j21")
        assert result is False, f"Expected tool {tool} to be blocked for adversarial"


# ===========================================================================
# 22. check_research_tool_allowed: general blocks dama_query and web_search
# ===========================================================================

def test_general_blocks_dama_and_web():
    assert check_research_tool_allowed("general", "nova_search") is True
    assert check_research_tool_allowed("general", "dama_query") is False
    assert check_research_tool_allowed("general", "web_search") is False


# ===========================================================================
# 23. check_research_tool_allowed: research allows all tools
# ===========================================================================

def test_research_allows_all_tools():
    for tool in ("nova_search", "dama_query", "web_search"):
        result = check_research_tool_allowed("research", tool, job_id="j23")
        assert result is True, f"Expected tool {tool} to be allowed for research"


# ===========================================================================
# 24. get_allowed_tools returns correct sets per task class
# ===========================================================================

def test_get_allowed_tools_per_task_class():
    adversarial_tools = get_allowed_tools("adversarial")
    assert adversarial_tools == []

    research_tools = get_allowed_tools("research")
    assert set(research_tools) == {"nova_search", "dama_query", "web_search"}

    general_tools = get_allowed_tools("general")
    assert "nova_search" in general_tools
    assert "dama_query" not in general_tools
    assert "web_search" not in general_tools

    investigation_tools = get_allowed_tools("investigation")
    assert "nova_search" in investigation_tools
    assert "dama_query" not in investigation_tools


# ===========================================================================
# 25. filter_adversarial_output strips Nova context blocks
# ===========================================================================

def test_filter_adversarial_output():
    text_with_context = (
        "[NOVA LIBRARY CONTEXT — cite source IDs in your response]\n"
        "  [1] Source: doc_001 (confidence=0.90)\n"
        "      Secret internal data\n\n"
        "The actual adversarial reasoning is here.\n\n"
        "[DAMA TELEMETRY — node=ant_001 metric=temperature status=ok]\n"
        "  2024-01-01: 23.5 (quality=good)\n\n"
        "More reasoning here."
    )

    filtered = filter_adversarial_output(text_with_context, job_id="j25")

    assert "[NOVA LIBRARY CONTEXT" not in filtered
    assert "[DAMA TELEMETRY" not in filtered
    assert "Secret internal data" not in filtered
    # Actual reasoning content should be preserved
    assert "adversarial reasoning" in filtered


# ===========================================================================
# Edge cases and integration smoke tests
# ===========================================================================

def test_proof_chain_unknown_source_type_defaults_to_internal():
    chain = ProofChain()
    entry = chain.cite("test claim", "unknown_source_type", "src_id")
    assert entry.source_type == SOURCE_TYPE_INTERNAL


def test_proof_chain_confidence_clamped():
    chain = ProofChain()
    entry_high = chain.cite("c1", SOURCE_TYPE_NOVA, "s1", confidence=1.5)
    entry_low = chain.cite("c2", SOURCE_TYPE_NOVA, "s2", confidence=-0.5)
    assert entry_high.confidence == 1.0
    assert entry_low.confidence == 0.0


def test_proof_chain_mean_confidence_empty():
    chain = ProofChain()
    assert chain.mean_confidence == 0.0


def test_proof_chain_build_from_nova_results():
    chain = ProofChain(job_id="j_build")

    results = [
        SearchResult(content="c1", source="src_a", confidence=0.9, snippet="s1"),
        SearchResult(content="c2", source="src_b", confidence=0.75, snippet="s2"),
    ]
    chain.build_citations_from_nova_results(results, pass_num=0)
    assert chain.citation_count == 2
    src_ids = {e.source_id for e in chain._entries}
    assert "src_a" in src_ids
    assert "src_b" in src_ids


def test_research_blocked_task_classes_constant():
    assert "adversarial" in RESEARCH_BLOCKED_TASK_CLASSES


def test_research_enabled_task_classes_constant():
    assert "research" in RESEARCH_ENABLED_TASK_CLASSES
    assert "adversarial" not in RESEARCH_ENABLED_TASK_CLASSES
