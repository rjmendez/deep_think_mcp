from __future__ import annotations

import socket

from fastmcp import FastMCP

from deep_think_mcp.api import reasoning as reasoning_api
from deep_think_mcp.engine import orchestrator
from deep_think_mcp import discover
from nova_factcheck import research_tools
from tools.code_search import invoke_code_search, _search_local_repo
from tools.web_search import invoke_web_search
from tools.nova_verify import invoke_nova_verify
from tools.document_fetch import invoke_document_fetch


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


def test_detect_cloud_providers_includes_abliteration_from_env(monkeypatch):
    monkeypatch.setenv("ABLITERATION_API_KEY", "test-abliteration-key")

    providers = discover._detect_cloud_providers()

    abliteration_models = [m for m in providers if m.provider == "abliteration"]
    assert abliteration_models
    assert abliteration_models[0].model_id == "abliterated-model"


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
    assert abliteration_models[0].timeout_secs == discover.cloud_timeout("abliterated-model")


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
