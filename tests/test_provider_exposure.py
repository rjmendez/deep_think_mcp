from __future__ import annotations

import socket

from fastmcp import FastMCP

from deep_think_mcp.api import reasoning as reasoning_api
from deep_think_mcp.engine import orchestrator
from deep_think_mcp.engine.orchestrator import _validate_synthesis_grounding
from deep_think_mcp import discover
from nova_factcheck import research_tools
from tools.code_search import invoke_code_search, _search_local_repo
from tools.web_search import invoke_web_search
from tools.nova_verify import invoke_nova_verify
from tools.document_fetch import invoke_document_fetch
import tools.document_fetch as document_fetch_module
from tool_invoker import ToolInvoker
from models_invoker import ToolDirective as InvokerDirective


def test_reasoning_tool_schema_exposes_provider_config_fields():
    mcp = FastMCP("test-provider-exposure")
    reasoning_api.register(mcp)

    tool = mcp._tool_manager._tools["deep_think_async"]
    provider_config_schema = tool.parameters["properties"]["provider_config"]["anyOf"][0]
    provider_fields = provider_config_schema["properties"]

    assert "provider" in provider_fields
    assert "medium_provider" in provider_fields
    assert "heavy_provider" in provider_fields
    assert "temperature" in provider_fields
    assert "adversarial_provider" in provider_fields
    assert "adversarial_heretic_model" in provider_fields


def test_detect_cloud_providers_includes_abliteration_from_env(monkeypatch):
    monkeypatch.setenv("ABLITERATION_API_KEY", "test-abliteration-key")

    providers = discover._detect_cloud_providers()

    abliteration_models = [m for m in providers if m.provider == "abliteration"]
    assert abliteration_models
    assert [m.model_id for m in abliteration_models] == ["gpt-4.1", "gpt-5.4", "gpt-5.5"]


def test_detect_cloud_providers_reads_abliteration_credentials_file(monkeypatch, tmp_path):
    monkeypatch.delenv("ABLITERATION_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(socket, "gethostname", lambda: "testhost")

    cred_dir = tmp_path / ".abliteration"
    cred_dir.mkdir()
    (cred_dir / "credentials").write_text("testhost=file-backed-key\n", encoding="utf-8")

    providers = discover._detect_cloud_providers()

    abliteration_models = [m for m in providers if m.provider == "abliteration"]
    assert abliteration_models
    assert abliteration_models[0].timeout_secs == discover.cloud_timeout("gpt-4.1")


def test_grounding_gate_marks_failed_tool_calls_as_inference_only():
    inference_only, warnings = orchestrator._validate_synthesis_grounding(
        synthesis_text="The result cites deep_think_mcp.c:123",
        tools_invoked_total=1,
        successful_tool_calls=0,
        enable_tool_use=True,
        task_class="code_review",
    )
    assert inference_only is True
    assert any("GROUNDING UNAVAILABLE" in w for w in warnings)


def test_grounding_gate_rejects_citation_like_text_without_evidence():
    inference_only, warnings = orchestrator._validate_synthesis_grounding(
        synthesis_text="Potential bug in engine/orchestrator.py:932 with routing drift.",
        tools_invoked_total=0,
        successful_tool_calls=0,
        enable_tool_use=False,
        task_class="code_review",
    )
    assert inference_only is True
    assert any("citation-like" in w.lower() for w in warnings)


def test_off_topic_detector_flags_rewritten_question():
    off_topic, reason = orchestrator._is_off_topic_response(
        "Review deep_think_mcp grounding integrity in engine/orchestrator.py",
        "Question: Explain the difference between a neural network and a decision tree.\nAnswer: ...",
    )
    assert off_topic is True
    assert "different question context" in reason


def test_off_topic_detector_allows_same_question_context():
    off_topic, reason = orchestrator._is_off_topic_response(
        "Review deep_think_mcp grounding integrity in engine/orchestrator.py",
        "Question: Review deep_think_mcp grounding integrity in engine/orchestrator.py\nFindings: ...",
    )
    assert off_topic is False
    assert reason == ""


def test_off_topic_detector_flags_long_low_overlap_answer_without_question_header():
    off_topic, reason = orchestrator._is_off_topic_response(
        "Audit retry behavior in orchestrator run_fan_out and cache gating",
        "Neural image style transfer uses convolutional layers and gradient descent to produce art-like results from photos.",
    )
    assert off_topic is True
    assert "overlap" in reason


def test_off_topic_detector_does_not_overblock_short_valid_answer():
    off_topic, reason = orchestrator._is_off_topic_response(
        "Add retries and logging for off-topic response handling",
        "Added bounded retries, logs, and metrics.",
    )
    assert off_topic is False
    assert reason == ""


def test_cached_answer_quality_gate_rejects_low_overlap():
    ok, reason = orchestrator._passes_cached_answer_quality_gate(
        "Review grounding checks in engine/orchestrator.py",
        "This response discusses neural style transfer and image classifiers.",
    )
    assert ok is False
    assert "low_question_overlap" in reason


def test_cached_answer_quality_gate_accepts_relevant_cached_answer():
    ok, reason = orchestrator._passes_cached_answer_quality_gate(
        "Review grounding checks in engine/orchestrator.py",
        "Question: Review grounding checks in engine/orchestrator.py\nAnswer: The grounding gate needs evidence coverage.",
    )
    assert ok is True
    assert reason == ""


def test_confidence_score_normalization_accepts_percent_string():
    normalized, warnings = orchestrator._normalize_synthesis_structured(
        {"confidence_score": "85.7%", "final_answer": "ok"}
    )
    assert normalized["confidence_score"] == 85
    assert warnings == []


def test_confidence_score_normalization_handles_invalid_values():
    normalized, warnings = orchestrator._normalize_synthesis_structured(
        {"confidence_score": "NaN", "final_answer": "ok"}
    )
    assert normalized["confidence_score"] == 0
    assert any("missing or invalid" in w for w in warnings)


def test_tool_guardrails_apply_auth_checks_to_registry_tools(monkeypatch):
    monkeypatch.delenv("NOVA_TOKEN", raising=False)

    class FakeSchema:
        requires_auth = True

    class FakeRegistry:
        def has_tool(self, tool_name):
            return tool_name == "nova_search"

        def get_tool_schema(self, _tool_name):
            return FakeSchema()

        def validate_tool_directive(self, _directive):
            return True, ""

        def get_tool_handler(self, _tool_name):
            return lambda q, t: ("ok", 0.1, "")

    invoker = ToolInvoker()
    invoker._tool_registry = FakeRegistry()

    result = invoker._invoke_single_tool(
        InvokerDirective(tool_name="nova_search", query="auth test"),
        timeout=5,
    )

    assert result.tool_status == "error"
    assert "requires authentication" in result.error_message


def test_local_code_search_returns_repo_matches():
    results, impact, error = invoke_code_search("tool_invoker", timeout=5)
    assert error == ""
    assert impact > 0
    assert "tool_invoker" in results


def test_local_code_search_no_match_is_success(monkeypatch):
    monkeypatch.setattr("tools.code_search._search_local_repo", lambda _query, _timeout: {"results": []})
    results, impact, error = invoke_code_search("query-that-does-not-exist-xyz", timeout=5)
    assert "No code matches found" in results
    assert impact == 0.0
    assert error == ""


def test_local_code_search_uses_perspective_specific_terms():
    base = (
        "Perform a deep whole-repository review for correctness security reliability "
        "of /path/to/repo."
    )
    query_a = (
        f"{base} Focus on sql injection in api/reasoning.py and data_policy precedence in provider."
    )
    query_b = (
        f"{base} Focus on timeout cancellation in tool_invoker and race handling in worker."
    )

    result_a = _search_local_repo(query_a, timeout=5)["results"]
    result_b = _search_local_repo(query_b, timeout=5)["results"]
    paths_a = {item["path"] for item in result_a}
    paths_b = {item["path"] for item in result_b}

    assert result_a and result_b
    assert paths_a != paths_b


def test_build_tool_query_prioritizes_perspective_context():
    query = orchestrator._build_tool_query(
        question="Base question about policy enforcement.",
        perspective_answer="Perspective-specific evidence about timeout handling.",
    )
    assert query.startswith("Perspective-specific evidence")
    assert "Base question about policy enforcement." in query


def test_web_search_wrapper_uses_real_research_provider(monkeypatch):
    async def fake_web_search(query, job_id="", task_class=""):
        return research_tools.WebSearchResponse(
            results=[
                research_tools.WebResult(
                    title="Python",
                    url="https://python.org",
                    snippet=f"Search for {query}",
                    domain="python.org",
                )
            ],
            total=1,
            query=query,
            latency_ms=12,
        )

    monkeypatch.setattr(research_tools, "web_search", fake_web_search)

    results, impact, error = invoke_web_search("python", timeout=5)
    assert error == ""
    assert impact > 0
    assert "Python" in results


def test_nova_verify_wrapper_formats_verification_result(monkeypatch):
    class FakeResult:
        status = "TRUE"
        nova_confidence = 0.88
        reasoning = "supported"
        evidence = [{"text": "evidence"}]
        latency_ms = 15

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def verify(self, claim):
            return FakeResult()

    monkeypatch.setattr("nova_factcheck.nova_client.NovaVerificationClient", lambda timeout_s=10: FakeClient())

    results, impact, error = invoke_nova_verify("Python is real", timeout=5)
    assert error == ""
    assert impact > 0
    assert "GROUNDED" in results


def test_nova_verify_wrapper_surfaces_error_state(monkeypatch):
    class FakeErrorResult:
        status = "ERROR"
        nova_confidence = 0.0
        reasoning = "Nova authentication failed"
        evidence = []
        error_kind = "auth_failed"
        latency_ms = 10

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def verify(self, claim):
            return FakeErrorResult()

    monkeypatch.setattr("nova_factcheck.nova_client.NovaVerificationClient", lambda timeout_s=10: FakeClient())

    results, impact, error = invoke_nova_verify("Python is real", timeout=5)
    assert "ERROR" in results
    assert impact < 0
    assert "auth_failed" in error


def test_document_fetch_blocks_local_path_outside_allowlist(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    blocked_file = tmp_path / "blocked.txt"
    blocked_file.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("DEEP_THINK_DOCUMENT_FETCH_ROOTS", str(allowed))

    _results, impact, error = invoke_document_fetch(str(blocked_file), timeout=5)
    assert impact < 0
    assert "blocked by document fetch policy" in error


def test_document_fetch_blocks_domain_not_in_whitelist(monkeypatch):
    monkeypatch.setenv("DEEP_THINK_DOCUMENT_FETCH_DOMAIN_WHITELIST", "python.org,docs.python.org")
    _results, impact, error = invoke_document_fetch("https://example.com", timeout=5)
    assert impact < 0
    assert "Domain blocked by document fetch policy" in error


def test_domain_whitelist_uses_exact_www_prefix_removal():
    assert research_tools._domain_allowed("https://www.evil.com", ["evil.com"]) is True
    assert research_tools._domain_allowed("https://wwwevil.com", ["evil.com"]) is False
    assert document_fetch_module._domain_allowed("https://www.evil.com", ["evil.com"]) is True
    assert document_fetch_module._domain_allowed("https://wwwevil.com", ["evil.com"]) is False


# --- Regression tests: _validate_synthesis_grounding citation mismatch check ---

def test_grounding_citation_mismatch_detected():
    synthesis = "See engine/orchestrator.py:120 for details."
    evidence = ["__init__.py:1\ndefaults.py:2"]
    inference_only, warnings = _validate_synthesis_grounding(
        synthesis_text=synthesis,
        tools_invoked_total=1,
        successful_tool_calls=1,
        enable_tool_use=True,
        task_class="code_review",
        evidence_texts=evidence,
    )
    assert inference_only is True
    assert any("CITATION MISMATCH" in w for w in warnings)


def test_grounding_citation_matched():
    synthesis = "See engine/orchestrator.py:120 for details."
    evidence = ["engine/orchestrator.py:120  some content here"]
    inference_only, warnings = _validate_synthesis_grounding(
        synthesis_text=synthesis,
        tools_invoked_total=1,
        successful_tool_calls=1,
        enable_tool_use=True,
        task_class="general",
        evidence_texts=evidence,
    )
    assert inference_only is False
    assert not any("CITATION MISMATCH" in w for w in warnings)


def test_grounding_citation_check_skipped_no_evidence():
    synthesis = "See engine/orchestrator.py:120 for details."
    inference_only, warnings = _validate_synthesis_grounding(
        synthesis_text=synthesis,
        tools_invoked_total=1,
        successful_tool_calls=1,
        enable_tool_use=True,
        task_class="general",
        evidence_texts=None,
    )
    assert not any("CITATION MISMATCH" in w for w in warnings)


def test_grounding_citation_check_skipped_no_tool_calls():
    synthesis = "See engine/orchestrator.py:120 for details."
    evidence = ["some evidence without the citation"]
    inference_only, warnings = _validate_synthesis_grounding(
        synthesis_text=synthesis,
        tools_invoked_total=0,
        successful_tool_calls=0,
        enable_tool_use=False,
        task_class="general",
        evidence_texts=evidence,
    )
    assert not any("CITATION MISMATCH" in w for w in warnings)
