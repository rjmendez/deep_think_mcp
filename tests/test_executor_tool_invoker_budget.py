from __future__ import annotations

from types import SimpleNamespace

from deep_think_mcp import executor
from deep_think_mcp.models_invoker import ToolInvocationBatch, ToolResult


def test_invoke_tools_and_digest_initializes_invoker_with_low_budget_gate(monkeypatch):
    captured_config = {}
    captured_perspective_ids = []

    class FakeInvoker:
        def __init__(self, config=None, task_class=None, job_id=None, web_domain_whitelist=None, data_policy=None):
            captured_config["config"] = config

        def invoke_tools(self, directives, budget_remaining, perspective_id, timeout):
            captured_perspective_ids.extend(getattr(d, "perspective_id", "") for d in directives)
            return ToolInvocationBatch(
                perspective_id=perspective_id,
                directives=directives,
                results=[
                    ToolResult(
                        tool_name="code_search",
                        query="test",
                        results="match",
                        tool_status="success",
                        timing_ms=5,
                    )
                ],
                total_time_ms=5,
                budget_consumed=1,
            )

    class FakeEvidenceManager:
        def process_batch(self, batch, original_confidence):
            return SimpleNamespace(
                entries=batch.results,
                total_confidence_delta=0.1,
                formatted_summary="ok",
            )

    monkeypatch.setattr("deep_think_mcp.tool_invoker.ToolInvoker", FakeInvoker)

    digest, consumed = executor.invoke_tools_and_digest(
        tools_queued=[
            {
                "tool_name": "code_search",
                "query": "test",
                "reason": "ground",
                "priority": 1,
            }
        ],
        perspective_id="p1",
        budget_remaining=5,
        original_confidence=0.5,
        config=executor.ExecutionConfig(tool_timeout=7),
        estimated_budget_cost=1,
        evidence_manager=FakeEvidenceManager(),
        tool_invoker=None,
    )

    assert captured_config["config"].min_budget_remaining == 1
    assert captured_config["config"].tool_timeout_seconds == 7
    assert captured_perspective_ids == ["p1"]
    assert digest is not None
    assert consumed == 1
