"""Schema and dataclasses for tool invocation in Phase 2 of deep_think engine.

Provides:
- ToolDirective: Instructions to invoke a specific tool
- ToolResult: Result of a single tool invocation
- ToolInvocationBatch: Batch of tool results from a perspective
- ToolInvocationConfig: Configuration for tool safety constraints
"""

from dataclasses import dataclass, field
from typing import Optional, List
from enum import Enum


class RoutingAction(Enum):
    """Routing decision from Phase 1 router."""
    CONTINUE_WITHOUT_TOOLS = "continue_without_tools"
    CONTINUE_WITH_TOOLS = "continue_with_tools"
    DROP = "drop"
    STRESS_TEST = "stress_test"
    ESCALATE_NOVELTY = "escalate_novelty"
    CONTRADICTION = "contradiction"


@dataclass
class ToolDirective:
    """Instruction to invoke a specific tool.
    
    Attributes:
        tool_name: str - Name of the tool (web_search, code_search, nova_verify, document_fetch)
        query: str - The query/claim to pass to the tool
        priority: int - Execution priority (0=must, 1=high, 2=medium, 3=exploratory)
        purpose: str - Purpose of the tool call (ground, refute, resolve, validate, unknown)
        expected_impact: str - Description of expected confidence impact if successful
    """
    tool_name: str
    query: str
    perspective_id: str = ""
    priority: int = 1
    purpose: str = "ground"
    expected_impact: str = ""
    
    def __post_init__(self):
        """Validate directive fields."""
        valid_tools = {"web_search", "code_search", "nova_verify", "document_fetch"}
        is_registered_tool = False
        try:
            from .tool_discovery import get_tool_registry
        except ImportError:  # pragma: no cover - support direct imports in tests
            try:
                from tool_discovery import get_tool_registry
            except ImportError:
                get_tool_registry = None
        if get_tool_registry is not None:
            try:
                is_registered_tool = get_tool_registry().has_tool(self.tool_name)
            except Exception:
                is_registered_tool = False

        if self.tool_name not in valid_tools and not is_registered_tool:
            raise ValueError(f"Invalid tool_name: {self.tool_name}. Must be one of {valid_tools}")
        if not 0 <= self.priority <= 3:
            raise ValueError(f"Invalid priority: {self.priority}. Must be 0-3")
        valid_purposes = {"ground", "refute", "resolve", "validate", "unknown"}
        if self.purpose not in valid_purposes:
            raise ValueError(f"Invalid purpose: {self.purpose}. Must be one of {valid_purposes}")


@dataclass
class ToolResult:
    """Result of a single tool invocation.
    
    Attributes:
        tool_name: str - Name of the tool that was invoked
        query: str - The query that was executed
        results: str - Formatted results from the tool
        tool_status: str - Status (success, timeout, error, not_callable)
        timing_ms: int - Time taken to invoke the tool (milliseconds)
        confidence_impact: float - Delta confidence change from this result
        error_message: str - Error detail if status != success
    """
    tool_name: str
    query: str
    results: str
    tool_status: str
    timing_ms: int
    confidence_impact: float = 0.0
    error_message: str = ""
    
    def __post_init__(self):
        """Validate result fields."""
        valid_statuses = {"success", "timeout", "error", "not_callable"}
        if self.tool_status not in valid_statuses:
            raise ValueError(f"Invalid tool_status: {self.tool_status}. Must be one of {valid_statuses}")


@dataclass
class ToolInvocationBatch:
    """Batch of tool invocations and their results.
    
    Attributes:
        perspective_id: str - ID of the perspective running this batch
        directives: List[ToolDirective] - Directives that were executed
        results: List[ToolResult] - Results from each directive
        total_time_ms: int - Total time for all tool calls
        budget_consumed: int - Number of tool calls executed
    """
    perspective_id: str
    directives: List[ToolDirective] = field(default_factory=list)
    results: List[ToolResult] = field(default_factory=list)
    total_time_ms: int = 0
    budget_consumed: int = 0


@dataclass
class RoutingDecision:
    """Decision from Phase 1 router with tool directives.
    
    Attributes:
        action: RoutingAction - The routing decision
        tool_directives: List[ToolDirective] - Tool calls to execute
        confidence_in_decision: float - Confidence in this decision
        reasoning: str - Explanation of the decision
    """
    action: RoutingAction
    tool_directives: List[ToolDirective] = field(default_factory=list)
    confidence_in_decision: float = 0.5
    reasoning: str = ""


@dataclass
class ToolInvocationConfig:
    """Configuration for tool invocation safety constraints.
    
    Attributes:
        max_tool_calls_per_perspective: int - Max calls per perspective
        max_tool_calls_per_pass: int - Global ceiling
        tool_timeout_seconds: int - Timeout per tool call
        min_budget_remaining: int - Minimum budget before refusing calls
    """
    max_tool_calls_per_perspective: int = 5
    max_tool_calls_per_pass: int = 15
    tool_timeout_seconds: int = 10
    min_budget_remaining: int = 10
