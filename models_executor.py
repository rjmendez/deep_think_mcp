"""
Phase 2 Part 3: Executor Schema & Models

Data structures for the OODA loop executor:
- PerspectiveExecutionState: Full execution trace for one perspective
- ExecutionPass: Aggregated metrics for one pass (height)
- ExecutionConfig: Configuration for execution behavior

This is the data contract between executor.py and test_executor_loop.py.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, List, Protocol, runtime_checkable
from enum import Enum

try:
    from .models_adaptive import PerspectiveAnalysis, RoutingDecision
except ImportError:  # pragma: no cover - support direct module imports in tests
    from models_adaptive import PerspectiveAnalysis, RoutingDecision
try:
    from .models_evidence import EvidenceDigest
except ImportError:  # pragma: no cover - support direct module imports in tests
    from models_evidence import EvidenceDigest
try:
    from .defaults import (
        DEFAULT_PASS_TIMEOUT_SECS,
        DEFAULT_REASONING_TIMEOUT_SECS,
        DEFAULT_TOOL_TIMEOUT_SECS,
    )
except ImportError:  # pragma: no cover - support direct module imports in tests
    from defaults import (
        DEFAULT_PASS_TIMEOUT_SECS,
        DEFAULT_REASONING_TIMEOUT_SECS,
        DEFAULT_TOOL_TIMEOUT_SECS,
    )


# ============================================================================
# TOOL PROTOCOL (Fix p3-fix-001)
# ============================================================================

@runtime_checkable
class ToolProtocol(Protocol):
    """
    Runtime-checkable protocol for tool implementations.
    
    Ensures tools implement the correct interface:
    - execute(params: dict) -> dict: async/sync method to invoke tool
    - name: str property for tool identity
    - schema: dict property for JSON schema
    
    Usage:
        if isinstance(my_tool, ToolProtocol):
            # Tool is valid
        else:
            # Tool is invalid, raise ToolConfigurationError
    """
    
    async def execute(self, params: dict) -> dict:
        """Execute the tool with given parameters."""
        ...
    
    @property
    def name(self) -> str:
        """Tool name identifier."""
        ...
    
    @property
    def schema(self) -> dict:
        """JSON schema for tool parameters."""
        ...


# ============================================================================
# EXCEPTIONS
# ============================================================================

class ToolConfigurationError(Exception):
    """Tool registration or configuration error."""
    pass


# ============================================================================
# ENUMS
# ============================================================================

class ExecutionStatus(Enum):
    """Status of a perspective execution."""
    RUNNING = "running"
    COMPLETE = "complete"
    ELIMINATED = "eliminated"
    ERROR = "error"


# ============================================================================
# EXECUTION STATE MODELS
# ============================================================================

@dataclass
class PerspectiveExecutionState:
    """
    Complete execution trace for one perspective in one pass.
    
    Captures all 4 OODA phases:
    - Observe: reasoning_output
    - Orient: analysis + routing_decision
    - Decide: queued tools
    - Act: evidence_digest
    """
    
    # Identity
    perspective_id: str
    pass_number: int
    
    # Status
    status: str  # "running", "complete", "eliminated", "error"
    
    # Observe phase: Raw reasoning output
    reasoning_output: str = ""
    
    # Orient phase: Analysis + decision
    analysis: Optional[PerspectiveAnalysis] = None
    routing_decision: Optional[RoutingDecision] = None
    
    # Decide phase: Tools that were queued
    tools_queued: List[dict] = field(default_factory=list)
    
    # Act phase: Evidence collected
    evidence_digest: Optional[EvidenceDigest] = None
    
    # Confidence tracking
    confidence_before: float = 0.0
    confidence_after: float = 0.0
    
    # Elimination tracking
    eliminated: bool = False
    elimination_reason: Optional[str] = None
    
    # Timing
    timing_ms: int = 0  # Total execution time for this perspective
    observe_ms: int = 0
    orient_ms: int = 0
    decide_ms: int = 0
    act_ms: int = 0
    
    # Metadata
    timestamp: datetime = field(default_factory=datetime.now)
    error_message: Optional[str] = None
    
    def __post_init__(self):
        """Validate execution state."""
        valid_statuses = {"running", "complete", "eliminated", "error"}
        if self.status not in valid_statuses:
            raise ValueError(f"Status must be one of {valid_statuses}, got {self.status}")
        
        if self.confidence_before < 0.0 or self.confidence_before > 1.0:
            raise ValueError(f"confidence_before must be in [0, 1], got {self.confidence_before}")
        
        if self.confidence_after < 0.0 or self.confidence_after > 1.0:
            raise ValueError(f"confidence_after must be in [0, 1], got {self.confidence_after}")


@dataclass
class ExecutionPass:
    """
    Aggregated metrics for one complete pass (height) across all perspectives.
    
    Tracks:
    - Which perspectives were active, which were eliminated
    - Total tool calls and budget consumed
    - Timing
    """
    
    # Identity
    pass_number: int
    height: int
    
    # Perspective tracking
    perspectives_active: List[str] = field(default_factory=list)  # IDs still active
    perspectives_eliminated: List[str] = field(default_factory=list)  # IDs eliminated
    elimination_reasons: Dict[str, str] = field(default_factory=dict)  # perspective_id -> reason
    
    # Tool tracking
    total_tool_calls: int = 0
    budget_remaining: int = 0
    budget_consumed_this_pass: int = 0
    
    # Execution results
    results: Dict[str, PerspectiveExecutionState] = field(default_factory=dict)  # perspective_id -> state
    
    # Timing
    total_time_ms: int = 0
    
    # Quality metrics
    avg_confidence_before: float = 0.0
    avg_confidence_after: float = 0.0
    avg_confidence_delta: float = 0.0
    
    # Errors
    error_count: int = 0
    
    def __post_init__(self):
        """Compute derived metrics."""
        # Count active vs eliminated
        self.perspectives_active = [
            pid for pid, state in self.results.items()
            if not state.eliminated and state.status != "eliminated"
        ]
        self.perspectives_eliminated = [
            pid for pid, state in self.results.items()
            if state.eliminated or state.status == "eliminated"
        ]
        
        # Compute average confidences
        if self.results:
            before_values = [s.confidence_before for s in self.results.values() if s.confidence_before > 0.0]
            after_values = [s.confidence_after for s in self.results.values() if s.confidence_after > 0.0]
            
            if before_values:
                self.avg_confidence_before = sum(before_values) / len(before_values)
            if after_values:
                self.avg_confidence_after = sum(after_values) / len(after_values)
            
            self.avg_confidence_delta = self.avg_confidence_after - self.avg_confidence_before
        
        # Count errors
        self.error_count = sum(1 for s in self.results.values() if s.status == "error")


@dataclass
class ExecutionConfig:
    """
    Configuration for executor behavior.
    
    Controls thresholds, timeouts, and constraints.
    """
    
    # Elimination thresholds
    elimination_threshold: float = 0.40  # Eliminate if confidence drops below this
    low_quality_threshold: float = 0.25  # Eliminate if quality below this
    
    # Timeouts (seconds)
    reasoning_timeout: int = DEFAULT_REASONING_TIMEOUT_SECS
    tool_timeout: int = DEFAULT_TOOL_TIMEOUT_SECS
    pass_timeout: int = DEFAULT_PASS_TIMEOUT_SECS
    
    # Constraints
    max_perspectives_eliminated_per_pass: int = 3
    min_budget_to_invoke_tools: int = 10  # Hard gate: never go below this
    
    # Tool priorities (which tools to drop first if budget low)
    drop_priority_3_if_budget_below: int = 5
    drop_priority_2_if_budget_below: int = 3
    
    def __post_init__(self):
        """Validate configuration."""
        if not 0.0 <= self.elimination_threshold <= 1.0:
            raise ValueError(f"elimination_threshold must be in [0, 1], got {self.elimination_threshold}")
        
        if not 0.0 <= self.low_quality_threshold <= 1.0:
            raise ValueError(f"low_quality_threshold must be in [0, 1], got {self.low_quality_threshold}")
        
        if self.reasoning_timeout <= 0:
            raise ValueError(f"reasoning_timeout must be > 0, got {self.reasoning_timeout}")
        
        if self.tool_timeout <= 0:
            raise ValueError(f"tool_timeout must be > 0, got {self.tool_timeout}")
        
        if self.pass_timeout <= 0:
            raise ValueError(f"pass_timeout must be > 0, got {self.pass_timeout}")
        
        if self.max_perspectives_eliminated_per_pass <= 0:
            raise ValueError(f"max_perspectives_eliminated_per_pass must be > 0, got {self.max_perspectives_eliminated_per_pass}")
        
        if self.min_budget_to_invoke_tools <= 0:
            raise ValueError(f"min_budget_to_invoke_tools must be > 0, got {self.min_budget_to_invoke_tools}")
