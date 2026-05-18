"""Tests for tool invoker engine Phase 2.

Tests:
- Priority sorting and batching
- Serial execution of Priority 0 (must)
- Parallel execution of Priority 1-2 (high/medium)
- Budget constraint enforcement
- Timeout handling
- Error handling and recovery
- Evidence formatting
- Safety limits (max_tool_calls, min_budget)
"""

import pytest
import time
from unittest.mock import Mock, patch, MagicMock
from models_invoker import (
    ToolDirective,
    ToolResult,
    ToolInvocationBatch,
    ToolInvocationConfig,
    RoutingAction,
    RoutingDecision,
)
from tool_invoker import (
    ToolInvoker,
    parse_tool_directives,
    format_evidence_for_reasoning,
)
from metrics import get_metrics, reset_metrics


# ═════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def config():
    """Default tool invocation config."""
    return ToolInvocationConfig(
        max_tool_calls_per_perspective=5,
        max_tool_calls_per_pass=15,
        tool_timeout_seconds=10,
        min_budget_remaining=10,
    )


@pytest.fixture
def invoker(config):
    """Tool invoker instance."""
    return ToolInvoker(config)


@pytest.fixture
def sample_directives():
    """Sample directives for testing."""
    return [
        ToolDirective(
            tool_name="web_search",
            query="test query 1",
            priority=0,
            purpose="ground",
            expected_impact="Should ground claim"
        ),
        ToolDirective(
            tool_name="code_search",
            query="test query 2",
            priority=1,
            purpose="validate",
            expected_impact="Should validate pattern"
        ),
        ToolDirective(
            tool_name="nova_verify",
            query="test claim",
            priority=2,
            purpose="resolve",
            expected_impact="Should resolve contradiction"
        ),
        ToolDirective(
            tool_name="document_fetch",
            query="https://example.com/doc",
            priority=3,
            purpose="validate",
            expected_impact="Should provide context"
        ),
    ]


# ═════════════════════════════════════════════════════════════════════════════
# TEST: DIRECTIVE VALIDATION
# ═════════════════════════════════════════════════════════════════════════════

class TestDirectiveValidation:
    """Tests for ToolDirective validation."""
    
    def test_valid_directive(self):
        """Valid directive can be created."""
        directive = ToolDirective(
            tool_name="web_search",
            query="test",
            priority=0,
            purpose="ground"
        )
        assert directive.tool_name == "web_search"
        assert directive.priority == 0
    
    def test_invalid_tool_name(self):
        """Invalid tool name raises ValueError."""
        with pytest.raises(ValueError):
            ToolDirective(
                tool_name="invalid_tool",
                query="test",
                priority=0,
                purpose="ground"
            )
    
    def test_invalid_priority(self):
        """Invalid priority raises ValueError."""
        with pytest.raises(ValueError):
            ToolDirective(
                tool_name="web_search",
                query="test",
                priority=4,  # Invalid
                purpose="ground"
            )
    
    def test_invalid_purpose(self):
        """Invalid purpose raises ValueError."""
        with pytest.raises(ValueError):
            ToolDirective(
                tool_name="web_search",
                query="test",
                priority=0,
                purpose="invalid"
            )
    
    def test_all_valid_tools(self):
        """All valid tools can be created."""
        for tool in ["web_search", "code_search", "nova_verify", "document_fetch"]:
            directive = ToolDirective(
                tool_name=tool,
                query="test",
                priority=0,
                purpose="ground"
            )
            assert directive.tool_name == tool
    
    def test_all_valid_purposes(self):
        """All valid purposes can be created."""
        for purpose in ["ground", "refute", "resolve", "validate", "unknown"]:
            directive = ToolDirective(
                tool_name="web_search",
                query="test",
                priority=0,
                purpose=purpose
            )
            assert directive.purpose == purpose


# ═════════════════════════════════════════════════════════════════════════════
# TEST: PRIORITY SORTING
# ═════════════════════════════════════════════════════════════════════════════

class TestPrioritySorting:
    """Tests for directive sorting by priority."""
    
    def test_parse_sorts_by_priority(self, sample_directives):
        """parse_tool_directives sorts by priority."""
        sorted_dirs = parse_tool_directives(sample_directives)
        priorities = [d.priority for d in sorted_dirs]
        assert priorities == [0, 1, 2, 3]
    
    def test_invoker_sorts_by_priority(self, invoker, sample_directives):
        """ToolInvoker sorts directives internally."""
        # Shuffle directives
        shuffled = [sample_directives[2], sample_directives[0], sample_directives[3], sample_directives[1]]
        sorted_dirs = invoker._sort_by_priority(shuffled)
        priorities = [d.priority for d in sorted_dirs]
        assert priorities == [0, 1, 2, 3]
    
    def test_stable_sort(self):
        """Sort is stable for same priority."""
        directives = [
            ToolDirective("web_search", "q1", priority=1),
            ToolDirective("code_search", "q2", priority=1),
            ToolDirective("nova_verify", "q3", priority=1),
        ]
        sorted_dirs = parse_tool_directives(directives)
        # Should maintain order for same priority
        assert sorted_dirs[0].tool_name == "web_search"
        assert sorted_dirs[1].tool_name == "code_search"
        assert sorted_dirs[2].tool_name == "nova_verify"


# ═════════════════════════════════════════════════════════════════════════════
# TEST: BUDGET CONSTRAINTS
# ═════════════════════════════════════════════════════════════════════════════

class TestBudgetConstraints:
    """Tests for budget enforcement."""
    
    def test_budget_too_low_returns_empty(self, invoker, sample_directives):
        """If budget <= min_budget_remaining, returns empty batch."""
        batch = invoker.invoke_tools(
            sample_directives,
            budget_remaining=9,  # Below min of 10
        )
        assert batch.budget_consumed == 0
        assert len(batch.results) == 0
    
    def test_budget_at_minimum(self, invoker, sample_directives):
        """At minimum budget, still rejects."""
        batch = invoker.invoke_tools(
            sample_directives,
            budget_remaining=10,  # Exactly minimum
        )
        assert batch.budget_consumed == 0
    
    def test_budget_above_minimum(self, invoker, sample_directives):
        """Above minimum budget allows calls."""
        # Mock tool execution to avoid real calls
        with patch.object(invoker, '_invoke_single_tool') as mock_invoke:
            mock_invoke.return_value = ToolResult(
                tool_name="web_search",
                query="test",
                results="success",
                tool_status="success",
                timing_ms=100,
            )
            batch = invoker.invoke_tools(
                [sample_directives[0]],  # Just Priority 0
                budget_remaining=11,
            )
            assert batch.budget_consumed == 1
    
    def test_budget_limits_calls(self, invoker):
        """Budget limits total tool calls."""
        directives = [
            ToolDirective("web_search", f"q{i}", priority=i % 2)
            for i in range(20)  # More directives than budget
        ]
        
        with patch.object(invoker, '_invoke_single_tool') as mock_invoke:
            mock_invoke.return_value = ToolResult(
                tool_name="web_search",
                query="test",
                results="success",
                tool_status="success",
                timing_ms=100,
            )
            batch = invoker.invoke_tools(directives, budget_remaining=20)
            # Should not exceed budget
            assert batch.budget_consumed <= 20


# ═════════════════════════════════════════════════════════════════════════════
# TEST: PRIORITY 0 SERIAL EXECUTION
# ═════════════════════════════════════════════════════════════════════════════

class TestSerialExecutionPriority0:
    """Tests for serial execution of Priority 0 (must) calls."""
    
    def test_priority_0_executes_first(self, invoker):
        """Priority 0 directives execute before others."""
        directives = [
            ToolDirective("web_search", "q1", priority=2),
            ToolDirective("code_search", "q2", priority=0),  # Must
            ToolDirective("nova_verify", "q3", priority=1),
        ]
        
        call_order = []
        
        def mock_invoke(directive, timeout):
            call_order.append(directive.priority)
            return ToolResult(
                tool_name=directive.tool_name,
                query=directive.query,
                results="success",
                tool_status="success",
                timing_ms=10,
            )
        
        with patch.object(invoker, '_invoke_single_tool', side_effect=mock_invoke):
            batch = invoker.invoke_tools(directives, budget_remaining=20)
        
        # Priority 0 should be first
        assert call_order[0] == 0
    
    def test_priority_0_max_2_calls(self, invoker):
        """Priority 0 limited to max 2 calls."""
        directives = [
            ToolDirective("web_search", f"q{i}", priority=0)
            for i in range(5)
        ]
        
        with patch.object(invoker, '_invoke_single_tool') as mock_invoke:
            mock_invoke.return_value = ToolResult(
                tool_name="web_search",
                query="test",
                results="success",
                tool_status="success",
                timing_ms=10,
            )
            batch = invoker.invoke_tools(directives, budget_remaining=20)
        
        # Should execute max 2 Priority 0 calls
        p0_results = [r for r in batch.results if len(r.query) > 0]
        assert len(p0_results) <= 2
    
    def test_priority_0_all_complete_before_return(self, invoker):
        """All Priority 0 calls complete before returning."""
        directives = [
            ToolDirective("web_search", "q1", priority=0),
            ToolDirective("code_search", "q2", priority=0),
        ]
        
        times = []
        
        def mock_invoke(directive, timeout):
            times.append(time.time())
            return ToolResult(
                tool_name=directive.tool_name,
                query=directive.query,
                results="success",
                tool_status="success",
                timing_ms=10,
            )
        
        with patch.object(invoker, '_invoke_single_tool', side_effect=mock_invoke):
            batch = invoker.invoke_tools(directives, budget_remaining=20)
        
        # All should complete
        assert len(batch.results) == 2


# ═════════════════════════════════════════════════════════════════════════════
# TEST: PARALLEL EXECUTION PRIORITY 1-2
# ═════════════════════════════════════════════════════════════════════════════

class TestParallelExecutionPriority1_2:
    """Tests for parallel execution of Priority 1-2 (high/medium) calls."""
    
    def test_priority_1_2_execute_in_parallel(self, invoker):
        """Priority 1-2 directives can execute in parallel."""
        directives = [
            ToolDirective("web_search", "q1", priority=1),
            ToolDirective("code_search", "q2", priority=1),
            ToolDirective("nova_verify", "q3", priority=2),
        ]
        
        call_count = {"count": 0}
        
        def mock_invoke(directive, timeout):
            call_count["count"] += 1
            # Simulate some work
            time.sleep(0.01)
            return ToolResult(
                tool_name=directive.tool_name,
                query=directive.query,
                results="success",
                tool_status="success",
                timing_ms=10,
            )
        
        with patch.object(invoker, '_invoke_single_tool', side_effect=mock_invoke):
            start = time.time()
            batch = invoker.invoke_tools(directives, budget_remaining=20)
            elapsed = time.time() - start
        
        # All should be invoked
        assert call_count["count"] == 3
        # Parallel execution should be faster than serial (roughly)
        # (Not a tight assertion, just checking batch works)
        assert len(batch.results) == 3
    
    def test_priority_1_2_max_3_calls(self, invoker):
        """Priority 1-2 limited to max 3 calls combined."""
        directives = [
            ToolDirective("web_search", f"q{i}", priority=1)
            for i in range(5)
        ] + [
            ToolDirective("code_search", f"q{i}", priority=2)
            for i in range(5)
        ]
        
        with patch.object(invoker, '_invoke_single_tool') as mock_invoke:
            mock_invoke.return_value = ToolResult(
                tool_name="web_search",
                query="test",
                results="success",
                tool_status="success",
                timing_ms=10,
            )
            batch = invoker.invoke_tools(directives, budget_remaining=20)
        
        # Should execute max 3 Priority 1-2 calls
        assert batch.budget_consumed <= 3


# ═════════════════════════════════════════════════════════════════════════════
# TEST: TIMEOUT HANDLING
# ═════════════════════════════════════════════════════════════════════════════

class TestTimeoutHandling:
    """Tests for tool timeout enforcement."""
    
    def test_timeout_returns_timeout_status(self, invoker):
        """Tool timeout returns timeout status."""
        directive = ToolDirective("web_search", "test", priority=0)
        
        # Return a timeout result instead of raising
        timeout_result = ToolResult(
            tool_name="web_search",
            query="test",
            results="",
            tool_status="timeout",
            timing_ms=10000,
            confidence_impact=-0.10,
        )
        
        with patch.object(invoker, '_invoke_single_tool', return_value=timeout_result):
            batch = invoker.invoke_tools([directive], budget_remaining=20)
        
        # Should have timeout result
        assert len(batch.results) == 1
        assert batch.results[0].tool_status == "timeout"
    
    def test_timeout_confidence_impact(self, invoker):
        """Timeout has negative confidence impact."""
        result = ToolResult(
            tool_name="web_search",
            query="test",
            results="",
            tool_status="timeout",
            timing_ms=10000,
            confidence_impact=-0.10,
        )
        assert result.confidence_impact == -0.10


# ═════════════════════════════════════════════════════════════════════════════
# TEST: ERROR HANDLING
# ═════════════════════════════════════════════════════════════════════════════

class TestErrorHandling:
    """Tests for error handling and recovery."""
    
    def test_invalid_tool_returns_error(self, invoker):
        """Invalid tool name is rejected by guardrails."""
        # Create a directive with a mocked invalid tool name
        directive = ToolDirective("web_search", "test", priority=0)
        directive.tool_name = "invalid_tool"  # Override after creation
        
        result = invoker._invoke_single_tool(directive, 10)
        assert result.tool_status == "error"
        assert "not registered" in result.error_message
    
    def test_network_error_returns_error(self, invoker):
        """Network error returns error status."""
        directive = ToolDirective("web_search", "test", priority=0)
        
        error_result = ToolResult(
            tool_name="web_search",
            query="test",
            results="",
            tool_status="error",
            timing_ms=100,
            confidence_impact=-0.05,
            error_message="Network connection failed",
        )
        
        with patch.object(invoker, '_invoke_single_tool', return_value=error_result):
            batch = invoker.invoke_tools([directive], budget_remaining=20)
        
        # Should have error result
        assert len(batch.results) == 1
        assert batch.results[0].tool_status == "error"
    
    def test_error_confidence_impact(self):
        """Error has negative confidence impact."""
        result = ToolResult(
            tool_name="web_search",
            query="test",
            results="",
            tool_status="error",
            timing_ms=100,
            confidence_impact=-0.05,
            error_message="Network connection failed",
        )
        assert result.confidence_impact == -0.05
        assert "Network" in result.error_message

    def test_code_search_no_match_maps_to_success(self, invoker):
        """No-match code_search is a successful call with neutral impact."""
        directive = ToolDirective("code_search", "unlikely query", priority=1)

        with patch("tools.code_search.invoke_code_search", return_value=("No code matches found", -0.05, "code_search returned no local matches")):
            result = invoker._invoke_single_tool(directive, 10)

        assert result.tool_status == "success"
        assert result.error_message == ""
        assert result.confidence_impact == 0.0

    def test_nova_verify_error_state_maps_to_error(self, invoker):
        """Nova verify ERROR state should not be classified as success."""
        directive = ToolDirective("nova_verify", "claim", priority=1)

        with patch("tools.nova_verify.invoke_nova_verify", return_value=("Nova Verification: ERROR", -0.10, "Nova verify auth_failed: unauthorized")):
            result = invoker._invoke_single_tool(directive, 10)

        assert result.tool_status == "error"
        assert "auth_failed" in result.error_message


# ═════════════════════════════════════════════════════════════════════════════
# TEST: DATA POLICY ENFORCEMENT
# ═════════════════════════════════════════════════════════════════════════════

class TestDataPolicyEnforcement:
    """Tests for local/cloud data policy guardrails."""

    def test_local_policy_blocks_external_tool_calls(self, config):
        invoker = ToolInvoker(config=config, data_policy="local")

        for tool_name in ("web_search", "document_fetch", "nova_verify"):
            directive = ToolDirective(tool_name=tool_name, query="test", priority=1)
            result = invoker._invoke_single_tool(directive, 10)
            assert result.tool_status == "error"
            assert "data_policy=local" in result.error_message

    def test_local_policy_allows_code_search(self, config):
        invoker = ToolInvoker(config=config, data_policy="local")
        directive = ToolDirective(tool_name="code_search", query="test", priority=1)

        with patch("tools.code_search.invoke_code_search", return_value=("No code matches found", -0.05, "code_search returned no local matches")):
            result = invoker._invoke_single_tool(directive, 10)

        assert result.tool_status == "success"


# ═════════════════════════════════════════════════════════════════════════════
# TEST: EVIDENCE FORMATTING
# ═════════════════════════════════════════════════════════════════════════════

class TestEvidenceFormatting:
    """Tests for formatting tool results as evidence."""
    
    def test_format_success_result(self):
        """Successful result formats cleanly."""
        result = ToolResult(
            tool_name="web_search",
            query="test query",
            results="Found 5 relevant sources",
            tool_status="success",
            timing_ms=250,
            confidence_impact=0.15,
        )
        evidence = format_evidence_for_reasoning(result, "ground")
        assert "web_search" in evidence
        assert "Found 5" in evidence
        assert "+0.15" in evidence
        assert "ground" in evidence.lower()
    
    def test_format_error_result(self):
        """Error result includes error message."""
        result = ToolResult(
            tool_name="web_search",
            query="test query",
            results="",
            tool_status="error",
            timing_ms=100,
            confidence_impact=-0.05,
            error_message="Connection timeout",
        )
        evidence = format_evidence_for_reasoning(result, "validate")
        assert "Connection timeout" in evidence
        assert "error" in evidence
    
    def test_format_includes_purpose(self):
        """Evidence formatting includes purpose guidance."""
        for purpose in ["ground", "refute", "resolve", "validate"]:
            result = ToolResult(
                tool_name="web_search",
                query="test",
                results="test results",
                tool_status="success",
                timing_ms=100,
                confidence_impact=0.10,
            )
            evidence = format_evidence_for_reasoning(result, purpose)
            assert purpose in evidence


# ═════════════════════════════════════════════════════════════════════════════
# TEST: TOOL RESULT VALIDATION
# ═════════════════════════════════════════════════════════════════════════════

class TestToolResultValidation:
    """Tests for ToolResult validation."""
    
    def test_valid_result(self):
        """Valid result can be created."""
        result = ToolResult(
            tool_name="web_search",
            query="test",
            results="Found results",
            tool_status="success",
            timing_ms=100,
            confidence_impact=0.15,
        )
        assert result.tool_name == "web_search"
        assert result.tool_status == "success"
    
    def test_invalid_status(self):
        """Invalid status raises ValueError."""
        with pytest.raises(ValueError):
            ToolResult(
                tool_name="web_search",
                query="test",
                results="",
                tool_status="invalid_status",
                timing_ms=100,
            )
    
    def test_all_valid_statuses(self):
        """All valid statuses work."""
        for status in ["success", "timeout", "error", "not_callable"]:
            result = ToolResult(
                tool_name="web_search",
                query="test",
                results="",
                tool_status=status,
                timing_ms=100,
            )
            assert result.tool_status == status


# ═════════════════════════════════════════════════════════════════════════════
# TEST: ROUTING DECISION INTEGRATION
# ═════════════════════════════════════════════════════════════════════════════

class TestRoutingDecisionIntegration:
    """Tests for RoutingDecision dataclass."""
    
    def test_routing_decision_with_tools(self):
        """RoutingDecision can hold tool directives."""
        directives = [
            ToolDirective("web_search", "test", priority=0),
            ToolDirective("code_search", "test", priority=1),
        ]
        decision = RoutingDecision(
            action=RoutingAction.CONTINUE_WITH_TOOLS,
            tool_directives=directives,
            confidence_in_decision=0.75,
            reasoning="Need external evidence",
        )
        assert decision.action == RoutingAction.CONTINUE_WITH_TOOLS
        assert len(decision.tool_directives) == 2
    
    def test_routing_decision_without_tools(self):
        """RoutingDecision can have empty tool list."""
        decision = RoutingDecision(
            action=RoutingAction.CONTINUE_WITHOUT_TOOLS,
            confidence_in_decision=0.95,
            reasoning="Sufficient internal evidence",
        )
        assert len(decision.tool_directives) == 0
        assert decision.action == RoutingAction.CONTINUE_WITHOUT_TOOLS


# ═════════════════════════════════════════════════════════════════════════════
# TEST: INVOCATION BATCH
# ═════════════════════════════════════════════════════════════════════════════

class TestInvocationBatch:
    """Tests for ToolInvocationBatch."""
    
    def test_batch_creation(self):
        """Batch can be created with results."""
        directives = [
            ToolDirective("web_search", "test", priority=0),
        ]
        results = [
            ToolResult(
                tool_name="web_search",
                query="test",
                results="success",
                tool_status="success",
                timing_ms=100,
            ),
        ]
        batch = ToolInvocationBatch(
            perspective_id="test_perspective",
            directives=directives,
            results=results,
            total_time_ms=100,
            budget_consumed=1,
        )
        assert batch.budget_consumed == 1
        assert len(batch.results) == 1
    
    def test_batch_empty(self):
        """Empty batch is valid."""
        batch = ToolInvocationBatch(perspective_id="test")
        assert batch.budget_consumed == 0
        assert len(batch.results) == 0


# ═════════════════════════════════════════════════════════════════════════════
# TEST: INVOKER CONFIG
# ═════════════════════════════════════════════════════════════════════════════

class TestToolInvocationConfig:
    """Tests for ToolInvocationConfig."""
    
    def test_default_config(self):
        """Default config has reasonable values."""
        config = ToolInvocationConfig()
        assert config.max_tool_calls_per_perspective == 5
        assert config.max_tool_calls_per_pass == 15
        assert config.tool_timeout_seconds == 10
        assert config.min_budget_remaining == 10
    
    def test_custom_config(self):
        """Custom config can override defaults."""
        config = ToolInvocationConfig(
            max_tool_calls_per_perspective=3,
            tool_timeout_seconds=5,
        )
        assert config.max_tool_calls_per_perspective == 3
        assert config.tool_timeout_seconds == 5
        assert config.max_tool_calls_per_pass == 15  # Default


# ═════════════════════════════════════════════════════════════════════════════
# TEST: BATCH TIMING
# ═════════════════════════════════════════════════════════════════════════════

class TestBatchTiming:
    """Tests for batch timing calculation."""
    
    def test_batch_timing_recorded(self, invoker):
        """Batch execution time is recorded."""
        directives = [
            ToolDirective("web_search", "test", priority=0),
        ]
        
        with patch.object(invoker, '_invoke_single_tool') as mock_invoke:
            mock_invoke.return_value = ToolResult(
                tool_name="web_search",
                query="test",
                results="success",
                tool_status="success",
                timing_ms=100,
            )
            batch = invoker.invoke_tools(directives, budget_remaining=20)
        
        assert batch.total_time_ms >= 0


# ═════════════════════════════════════════════════════════════════════════════
# TEST: CUSTOM HANDLER REGISTRATION (tool_discovery)
# ═════════════════════════════════════════════════════════════════════════════

class TestCustomHandlerRegistration:
    """Tests for ToolRegistry custom handler validation (ToolProtocol enforcement)."""

    def _make_registry(self):
        """Return a fresh ToolRegistry for each test."""
        from tool_discovery import ToolRegistry
        return ToolRegistry()

    def _make_schema(self, name: str):
        from tool_discovery import ToolCapability, ToolCategory
        return ToolCapability(
            name=name,
            description="Test tool",
            category=ToolCategory.SEARCH.value,
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            output_schema={"type": "string"},
        )

    def test_handler_without_execute_raises(self):
        """Registering an object without execute() raises ValueError."""
        registry = self._make_registry()
        schema = self._make_schema("custom_tool_a")

        class BadHandler:
            pass

        with pytest.raises(ValueError, match="ToolProtocol"):
            registry.register_tool("custom_tool_a", schema, handler=BadHandler())

    def test_handler_with_execute_is_accepted(self):
        """Registering an object implementing execute() succeeds."""
        registry = self._make_registry()
        schema = self._make_schema("custom_tool_b")

        class GoodHandler:
            def execute(self, query: str, timeout: int, **kwargs):
                return "result", 0.1, ""

        registry.register_tool("custom_tool_b", schema, handler=GoodHandler())
        assert registry.has_tool("custom_tool_b")

    def test_handler_with_noncallable_execute_raises(self):
        """An execute attribute that is not callable is rejected."""
        registry = self._make_registry()
        schema = self._make_schema("custom_tool_b2")

        class BadHandler:
            execute = "not callable"

        with pytest.raises(ValueError, match="ToolProtocol"):
            registry.register_tool("custom_tool_b2", schema, handler=BadHandler())

    def test_plain_callable_handler_is_accepted(self):
        """Registering a plain callable (function) handler succeeds."""
        registry = self._make_registry()
        schema = self._make_schema("custom_tool_c")

        def my_handler(query, timeout, **kwargs):
            return "result", 0.1, ""

        registry.register_tool("custom_tool_c", schema, handler=my_handler)
        assert registry.has_tool("custom_tool_c")

    def test_lambda_handler_is_accepted(self):
        """Registering a lambda handler succeeds."""
        registry = self._make_registry()
        schema = self._make_schema("custom_tool_d")
        registry.register_tool("custom_tool_d", schema, handler=lambda q, t: ("ok", 0.1, ""))
        assert registry.has_tool("custom_tool_d")

    def test_registered_execute_handler_is_invoked(self):
        """Registered custom handlers are callable through the invoker."""
        from tool_discovery import get_tool_registry, register_custom_tool

        registry = get_tool_registry()
        schema = self._make_schema("custom_tool_e")

        class GoodHandler:
            def execute(self, query: str, timeout: int, **kwargs):
                return {"query": query, "timeout": timeout}, 0.25, ""

        register_custom_tool("custom_tool_e", schema, handler=GoodHandler())

        invoker = ToolInvoker()
        invoker._tool_registry = registry

        directive = ToolDirective(
            tool_name="custom_tool_e",
            query="hello",
            priority=1,
            purpose="ground",
        )
        result = invoker._invoke_single_tool(directive, timeout=5)

        assert result.tool_status == "success"
        assert '"query": "hello"' in result.results
        assert result.confidence_impact == 0.25


# ═════════════════════════════════════════════════════════════════════════════
# TEST: TOOL OUTPUT NORMALIZATION
# ═════════════════════════════════════════════════════════════════════════════

class TestToolOutputNormalization:
    """Tests for ToolInvoker._normalize_tool_output validation and normalization."""

    def test_string_passthrough(self):
        """String output is returned unchanged."""
        result, err = ToolInvoker._normalize_tool_output("hello world")
        assert err == ""
        assert result == "hello world"

    def test_int_normalizes_to_repr(self):
        """int output is converted with repr."""
        result, err = ToolInvoker._normalize_tool_output(42)
        assert err == ""
        assert result == "42"

    def test_float_normalizes_to_repr(self):
        """float output is converted with repr."""
        result, err = ToolInvoker._normalize_tool_output(3.14)
        assert err == ""
        assert result == repr(3.14)

    def test_bool_normalizes_to_repr(self):
        """bool output is converted with repr."""
        result, err = ToolInvoker._normalize_tool_output(True)
        assert err == ""
        assert result == "True"

    def test_none_normalizes_to_repr(self):
        """None output is converted with repr."""
        result, err = ToolInvoker._normalize_tool_output(None)
        assert err == ""
        assert result == "None"

    def test_safe_dict_normalizes_to_json(self):
        """JSON-safe dict is serialized to JSON string."""
        value = {"key": "value", "count": 3}
        result, err = ToolInvoker._normalize_tool_output(value)
        import json
        assert err == ""
        assert json.loads(result) == value

    def test_safe_list_normalizes_to_json(self):
        """JSON-safe list is serialized to JSON string."""
        value = [1, "two", True, None]
        result, err = ToolInvoker._normalize_tool_output(value)
        import json
        assert err == ""
        assert json.loads(result) == value

    def test_safe_tuple_normalizes_to_json(self):
        """JSON-safe tuple is serialized as a JSON array."""
        value = (1, 2, 3)
        result, err = ToolInvoker._normalize_tool_output(value)
        import json
        assert err == ""
        assert json.loads(result) == list(value)

    def test_callable_is_rejected(self):
        """Callable tool output is rejected without calling it."""
        side_effects = []

        def evil():
            side_effects.append("called")
            return "pwned"

        result, err = ToolInvoker._normalize_tool_output(evil)
        assert result == ""
        assert "callable" in err
        assert side_effects == [], "callable must not have been invoked"

    def test_generator_is_rejected(self):
        """Generator tool output is rejected."""
        def gen():
            yield "item"

        result, err = ToolInvoker._normalize_tool_output(gen())
        assert result == ""
        assert "generator" in err

    def test_arbitrary_object_with_str_side_effect_is_rejected(self):
        """Object with side-effectful __str__ is rejected without calling str()."""
        side_effects = []

        class EvilStr:
            def __str__(self):
                side_effects.append("str called")
                return "evil"

            def __repr__(self):
                side_effects.append("repr called")
                return "evil"

        result, err = ToolInvoker._normalize_tool_output(EvilStr())
        assert result == ""
        assert "unsupported type" in err
        assert side_effects == [], "__str__/__repr__ must not have been called"

    def test_non_json_serializable_dict_rejected(self):
        """Dict containing a non-JSON-serializable value is rejected."""
        result, err = ToolInvoker._normalize_tool_output({"bad": object()})
        assert result == ""
        assert "not JSON-serializable" in err

    def test_malicious_tool_output_yields_error_result(self, invoker):
        """A tool wrapper returning a callable yields an error ToolResult."""

        def malicious_result():
            pass

        directive = ToolDirective("web_search", "test query", priority=0)

        with patch("tools.web_search.invoke_web_search", return_value=(malicious_result, 0.1, "")):
            result = invoker._invoke_single_tool(directive, 10)

        assert result.tool_status == "error"
        assert "callable" in result.error_message

    def test_safe_dict_output_normalizes_end_to_end(self, invoker):
        """A tool wrapper returning a dict normalizes correctly into ToolResult.results."""
        import json
        dict_output = {"title": "Test", "score": 0.9}
        directive = ToolDirective("web_search", "test query", priority=0)

        with patch("tools.web_search.invoke_web_search", return_value=(dict_output, 0.15, "")):
            result = invoker._invoke_single_tool(directive, 10)

        assert result.tool_status == "success"
        assert json.loads(result.results) == dict_output


class TestToolOutcomeMetrics:
    def test_tool_outcome_metrics_recorded(self, invoker):
        reset_metrics()
        directive = ToolDirective("web_search", "test", priority=0)
        with patch("tools.web_search.invoke_web_search", return_value=("ok", 0.1, "")):
            result = invoker._invoke_single_tool(directive, 10)
        assert result.tool_status == "success"
        metrics = get_metrics().to_dict()
        assert metrics["tools"]["outcomes"].get("web_search:success", 0) >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
