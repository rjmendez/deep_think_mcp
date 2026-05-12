"""
Phase 1 Part 1: Core Schemas for Adaptive Reasoning in Deep-Think MCP

This module defines the dataclasses and schemas used by the analyzer and router
to structure reasoning outputs, uncertainties, contradictions, and routing decisions.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any
import re
from enum import Enum

try:
    from .defaults import (
        AGGRESSIVE_TOOL_TIMEOUT_SECS,
        CONSERVATIVE_TOOL_TIMEOUT_SECS,
        DEFAULT_TOOL_TIMEOUT_SECS,
    )
except ImportError:  # pragma: no cover - support direct module imports in tests
    from defaults import (
        AGGRESSIVE_TOOL_TIMEOUT_SECS,
        CONSERVATIVE_TOOL_TIMEOUT_SECS,
        DEFAULT_TOOL_TIMEOUT_SECS,
    )

# ============================================================================
# ENUMS
# ============================================================================

class ClaimCategory(Enum):
    """Classification of claim types."""
    FACTUAL = "factual"          # empirically verifiable
    OPINION = "opinion"          # subjective assessment
    PROCEDURAL = "procedural"    # how-to, method-based
    UNKNOWN = "unknown"          # unable to classify


class ClaimSource(Enum):
    """How the claim was derived."""
    EXTRACTED = "extracted"      # directly from reasoning text
    INFERRED = "inferred"        # inferred from context
    FALLBACK = "fallback"        # default/uncertain


class RoutingAction(Enum):
    """Actions available for routing."""
    CONTINUE = "continue"                          # proceed normally
    CONTINUE_WITH_TOOLS = "continue_with_tools"    # invoke tools
    DROP = "drop"                                  # eliminate perspective
    FAST_TRACK = "fast_track"                      # promote to synthesis


class ContradictionType(Enum):
    """Classification of contradictions."""
    DIRECT_NEGATION = "direct_negation"    # A and NOT A
    NUMERIC_CONFLICT = "numeric_conflict"  # conflicting values
    LOGICAL = "logical"                    # logical inconsistency
    UNKNOWN = "unknown"                    # unclassified


# ============================================================================
# DATACLASSES
# ============================================================================

@dataclass
class Claim:
    """Basic unit of reasoning - an assertion made during analysis."""
    
    text: str
    confidence: float                # 0.0-1.0: how sure are we?
    importance: float                # 0.0-1.0: how central to perspective?
    category: str                    # from ClaimCategory
    source: str                      # from ClaimSource
    justification_tokens: int        # how many tokens support this claim
    
    def __post_init__(self):
        """Validate confidence and importance are in valid range."""
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"Confidence must be in [0, 1], got {self.confidence}")
        if not 0.0 <= self.importance <= 1.0:
            raise ValueError(f"Importance must be in [0, 1], got {self.importance}")
        
        # Validate category
        valid_categories = {e.value for e in ClaimCategory}
        if self.category not in valid_categories:
            raise ValueError(f"Category must be one of {valid_categories}, got {self.category}")
        
        # Validate source
        valid_sources = {e.value for e in ClaimSource}
        if self.source not in valid_sources:
            raise ValueError(f"Source must be one of {valid_sources}, got {self.source}")


@dataclass
class Uncertainty:
    """Areas of doubt in the analysis."""
    
    statement: str                   # "uncertain about X"
    about_claim: Optional[str]       # reference to claim.text if applicable
    severity: float                  # 0.0-1.0: how problematic is this uncertainty?
    
    def __post_init__(self):
        """Validate severity is in valid range."""
        if not 0.0 <= self.severity <= 1.0:
            raise ValueError(f"Severity must be in [0, 1], got {self.severity}")


@dataclass
class Contradiction:
    """Internal or cross-perspective contradiction."""
    
    claim_a: str
    claim_b: str
    contradiction_type: str          # from ContradictionType
    severity: float                  # 0.0-1.0: how serious?
    evidence: Optional[str] = None   # explanation of the contradiction
    
    def __post_init__(self):
        """Validate contradiction_type and severity."""
        if not 0.0 <= self.severity <= 1.0:
            raise ValueError(f"Severity must be in [0, 1], got {self.severity}")
        
        valid_types = {e.value for e in ContradictionType}
        if self.contradiction_type not in valid_types:
            raise ValueError(f"Type must be one of {valid_types}, got {self.contradiction_type}")


@dataclass
class PerspectiveAnalysis:
    """Complete analysis of one perspective output."""
    
    # Identity (required)
    perspective_id: str
    height: int
    model_tier: str                  # "light", "medium", "heavy"
    
    # Claims & assertions (required)
    claims: list[Claim]
    
    # Confidence (required)
    aggregate_confidence: float       # 0.0-1.0: overall confidence level
    
    # Now all fields with defaults
    claim_set: list[str] = field(default_factory=list)  # just the text for easy reference
    confidence_distribution: Optional[dict] = None  # {"mean": X, "stdev": Y, "min": Z, "max": W}
    
    # Uncertainties
    uncertainties: list[Uncertainty] = field(default_factory=list)
    uncertainty_count: int = 0
    uncertainty_ratio: float = 0.0  # uncertainties / claims
    
    # Contradictions
    internal_contradictions: list[Contradiction] = field(default_factory=list)
    external_contradictions: list[Contradiction] = field(default_factory=list)  # Phase 2
    contradiction_count: int = 0
    contradiction_severity: float = 0.0  # max severity among all contradictions
    
    # Depth & quality
    reasoning_depth: int = 0         # number of reasoning steps
    reasoning_chain_length: int = 0  # total words
    completeness_score: float = 0.5  # estimated coverage of topic [0, 1]
    
    # Aggregate quality
    quality_score: float = 0.5       # 0.0-1.0: overall quality rating
    quality_breakdown: dict = field(default_factory=lambda: {
        "confidence": 0.5,
        "consistency": 0.5,
        "depth": 0.5,
        "completeness": 0.5,
    })
    
    # Convergence (populated in Phase 2)
    convergence_signal: Optional[float] = None  # does this match prior pass?
    divergence_from_median: Optional[float] = None
    
    # Tool readiness
    needs_grounding: bool = False    # needs web search / document fetch?
    needs_refutation: bool = False   # needs adversarial testing?
    needs_resolution: bool = False   # needs tool-based conflict resolution?
    
    # Metadata
    timestamp: datetime = field(default_factory=datetime.now)
    reasoning_text_summary: str = "" # first 500 chars for audit trail
    
    def __post_init__(self):
        """Validate fields and compute derived metrics."""
        # Validate scores
        if not 0.0 <= self.aggregate_confidence <= 1.0:
            raise ValueError(f"Aggregate confidence must be in [0, 1], got {self.aggregate_confidence}")
        if not 0.0 <= self.quality_score <= 1.0:
            raise ValueError(f"Quality score must be in [0, 1], got {self.quality_score}")
        if not 0.0 <= self.completeness_score <= 1.0:
            raise ValueError(f"Completeness score must be in [0, 1], got {self.completeness_score}")
        
        # Validate model tier
        valid_tiers = {"light", "medium", "heavy"}
        if self.model_tier not in valid_tiers:
            raise ValueError(f"Model tier must be one of {valid_tiers}, got {self.model_tier}")
        
        # Populate claim_set if claims are provided
        if self.claims and not self.claim_set:
            self.claim_set = [c.text for c in self.claims]
        
        # Update counts
        self.uncertainty_count = len(self.uncertainties)
        self.contradiction_count = len(self.internal_contradictions) + len(self.external_contradictions)
        
        # Compute uncertainty ratio
        if self.claims:
            self.uncertainty_ratio = self.uncertainty_count / len(self.claims)
        
        # Compute contradiction severity
        if self.internal_contradictions:
            self.contradiction_severity = max(c.severity for c in self.internal_contradictions)
        if self.external_contradictions:
            self.contradiction_severity = max(
                self.contradiction_severity,
                max(c.severity for c in self.external_contradictions)
            )


@dataclass
class ToolDirective:
    """Instruction to invoke a tool."""
    
    tool_name: str                   # "web_search", "code_search", "document_fetch"
    query: str
    reason: str                      # why this tool was triggered
    priority: int = 1                # 0=must, 1=high, 2=medium, 3=exploratory
    expected_impact: str = ""        # what this tool should accomplish
    max_results: int = 10
    timeout: int = DEFAULT_TOOL_TIMEOUT_SECS
    
    def __post_init__(self):
        """Validate tool configuration."""
        valid_tools = {"web_search", "code_search", "document_fetch", "nova_verify"}
        if self.tool_name not in valid_tools:
            raise ValueError(f"Tool must be one of {valid_tools}, got {self.tool_name}")
        if self.priority not in (0, 1, 2, 3):
            raise ValueError(f"priority must be in (0, 1, 2, 3), got {self.priority}")
        if self.max_results <= 0:
            raise ValueError(f"max_results must be > 0, got {self.max_results}")
        if self.timeout <= 0:
            raise ValueError(f"timeout must be > 0, got {self.timeout}")


@dataclass
class RoutingDecision:
    """What to do with this perspective."""
    
    # Identity
    perspective_id: str
    
    # Action
    action: str                      # from RoutingAction
    
    # Tools
    recommended_tools: list[ToolDirective] = field(default_factory=list)
    
    # Decision basis
    decision_basis: list[str] = field(default_factory=list)  # reasons for decision
    elimination_reason: Optional[str] = None  # if action="drop"
    should_eliminate: bool = False
    
    # Priority
    priority_adjustment: float = 1.0  # 1.0 = normal, 2.0 = high, 0.5 = low
    
    # Metadata
    decision_timestamp: datetime = field(default_factory=datetime.now)
    
    def __post_init__(self):
        """Validate routing decision configuration."""
        valid_actions = {e.value for e in RoutingAction}
        if self.action not in valid_actions:
            raise ValueError(f"Action must be one of {valid_actions}, got {self.action}")
        
        if self.priority_adjustment <= 0:
            raise ValueError(f"priority_adjustment must be > 0, got {self.priority_adjustment}")


@dataclass
class AnalysisResult:
    """Batch result from analyzer."""
    
    analyses: list[PerspectiveAnalysis]
    height: int
    timestamp: datetime = field(default_factory=datetime.now)
    total_analysis_time_ms: float = 0.0
    
    # Aggregate stats
    avg_confidence: float = 0.0
    confidence_distribution_histogram: dict = field(default_factory=dict)
    contradiction_count: int = 0
    total_uncertainties: int = 0
    perspectives_needing_tools: int = 0
    
    def __post_init__(self):
        """Compute aggregate statistics."""
        if self.analyses:
            # Compute average confidence
            confidences = [a.aggregate_confidence for a in self.analyses]
            self.avg_confidence = sum(confidences) / len(confidences)
            
            # Aggregate counts
            self.contradiction_count = sum(a.contradiction_count for a in self.analyses)
            self.total_uncertainties = sum(a.uncertainty_count for a in self.analyses)
            self.perspectives_needing_tools = sum(
                1 for a in self.analyses
                if a.needs_grounding or a.needs_refutation or a.needs_resolution
            )
            
            # Build histogram (10 bins)
            bins = [0] * 10
            for conf in confidences:
                bin_idx = int(conf * 10)
                if bin_idx >= 10:
                    bin_idx = 9
                bins[bin_idx] += 1
            
            for i, count in enumerate(bins):
                key = f"{i*0.1:.1f}-{(i+1)*0.1:.1f}"
                self.confidence_distribution_histogram[key] = count


@dataclass
class AdaptiveDecision:
    """Routing decision made during adaptive reasoning at a specific height."""
    
    height: int                          # At which height was this decision made?
    perspective_id: str                  # Which perspective is this for?
    action: str                          # "continue", "continue_with_tools", "drop", "fast_track"
    reason: str                          # Why this action?
    quality_score: Optional[float] = None  # Quality score at decision time
    tool_triggered: bool = False         # Was a tool invoked?
    timestamp: datetime = field(default_factory=datetime.now)
    
    def __post_init__(self):
        """Validate routing decision."""
        valid_actions = {"continue", "continue_with_tools", "drop", "fast_track"}
        if self.action not in valid_actions:
            raise ValueError(f"action must be one of {valid_actions}, got {self.action}")


@dataclass
class ToolInvocation:
    """Record of a single tool invocation during reasoning."""
    
    tool_name: str                       # "web_search", "code_search", etc.
    query: str                           # What was searched/queried?
    perspective_id: str                  # Which perspective invoked this?
    height: int                          # At which height?
    result_summary: str                  # Brief summary of results
    confidence_before: float              # Confidence before tool invocation
    confidence_after: float               # Confidence after tool invocation
    result_quality_score: float = 0.5    # How useful were the results? [0, 1]
    error: Optional[str] = None          # Error message if tool failed
    execution_time_ms: float = 0.0       # How long did the tool take?
    timestamp: datetime = field(default_factory=datetime.now)
    
    def __post_init__(self):
        """Validate tool invocation."""
        if not 0.0 <= self.confidence_before <= 1.0:
            raise ValueError(f"confidence_before must be in [0, 1], got {self.confidence_before}")
        if not 0.0 <= self.confidence_after <= 1.0:
            raise ValueError(f"confidence_after must be in [0, 1], got {self.confidence_after}")
        if not 0.0 <= self.result_quality_score <= 1.0:
            raise ValueError(f"result_quality_score must be in [0, 1], got {self.result_quality_score}")


@dataclass
class EvidenceChain:
    """Complete chain of evidence gathering during adaptive reasoning."""
    
    job_id: Optional[str] = None         # Associated job ID
    tool_invocations: list[ToolInvocation] = field(default_factory=list)
    total_tools_invoked: int = 0
    total_execution_time_ms: float = 0.0
    avg_confidence_improvement: float = 0.0
    
    def __post_init__(self):
        """Compute aggregate statistics."""
        self.total_tools_invoked = len(self.tool_invocations)
        if self.tool_invocations:
            self.total_execution_time_ms = sum(t.execution_time_ms for t in self.tool_invocations)
            improvements = [
                t.confidence_after - t.confidence_before 
                for t in self.tool_invocations
            ]
            self.avg_confidence_improvement = sum(improvements) / len(improvements)


@dataclass
class AdaptiveConfig:
    """Configuration for adaptive routing and analysis."""
    
    # Model selection
    analysis_model: str              # "light", "medium", "heavy"
    
    # Tool triggers: {trigger_name: {threshold, tools, action}}
    tool_triggers: dict = field(default_factory=dict)
    
    # Perspective elimination
    perspective_elimination: dict = field(default_factory=dict)
    
    # Budgets
    max_tool_calls_per_perspective: int = 5
    max_tool_calls_global: int = 20
    tool_timeout: int = DEFAULT_TOOL_TIMEOUT_SECS
    
    # Tools
    allowed_tools: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    
    # Weights for quality scoring
    quality_score_weights: dict = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate configuration."""
        valid_tiers = {"light", "medium", "heavy"}
        if self.analysis_model not in valid_tiers:
            raise ValueError(f"analysis_model must be one of {valid_tiers}, got {self.analysis_model}")
        
        if self.max_tool_calls_per_perspective <= 0:
            raise ValueError(f"max_tool_calls_per_perspective must be > 0, got {self.max_tool_calls_per_perspective}")
        if self.max_tool_calls_global <= 0:
            raise ValueError(f"max_tool_calls_global must be > 0, got {self.max_tool_calls_global}")
        if self.tool_timeout <= 0:
            raise ValueError(f"tool_timeout must be > 0, got {self.tool_timeout}")
        
        # Validate allowed/forbidden tools
        valid_tools = {"web_search", "code_search", "document_fetch", "nova_verify"}
        for tool in self.allowed_tools:
            if tool not in valid_tools:
                raise ValueError(f"allowed_tools: {tool} not in {valid_tools}")
        for tool in self.forbidden_tools:
            if tool not in valid_tools:
                raise ValueError(f"forbidden_tools: {tool} not in {valid_tools}")


# ============================================================================
# DEFAULT CONFIGURATIONS
# ============================================================================

DEFAULT_ADAPTIVE_CONFIG = AdaptiveConfig(
    analysis_model="medium",
    tool_triggers={
        "low_confidence": {
            "threshold": 0.3,
            "tools": ["web_search"],
            "action": "ground",
        },
        "high_confidence": {
            "threshold": 0.85,
            "tools": ["nova_verify"],
            "action": "refute",
        },
        "contradiction": {
            "threshold": 0.6,
            "tools": ["web_search", "code_search"],
            "action": "resolve",
        },
    },
    perspective_elimination={
        "enabled": True,
        "quality_threshold": 0.4,
        "review_at_heights": [1, 2, 3],
    },
    max_tool_calls_per_perspective=5,
    max_tool_calls_global=20,
    tool_timeout=DEFAULT_TOOL_TIMEOUT_SECS,
    allowed_tools=["web_search", "code_search", "document_fetch", "nova_verify"],
    forbidden_tools=[],
    quality_score_weights={
        "confidence": 0.4,
        "consistency": 0.3,
        "depth": 0.15,
        "completeness": 0.15,
    },
)

CONSERVATIVE_CONFIG = AdaptiveConfig(
    analysis_model="light",
    tool_triggers={
        "low_confidence": {
            "threshold": 0.2,
            "tools": ["web_search", "document_fetch"],
            "action": "ground",
        },
        "contradiction": {
            "threshold": 0.5,
            "tools": ["web_search"],
            "action": "resolve",
        },
    },
    perspective_elimination={
        "enabled": False,
        "quality_threshold": 0.2,
        "review_at_heights": [],
    },
    max_tool_calls_per_perspective=2,
    max_tool_calls_global=10,
    tool_timeout=CONSERVATIVE_TOOL_TIMEOUT_SECS,
    allowed_tools=["web_search"],
    forbidden_tools=["code_search"],
    quality_score_weights={
        "confidence": 0.6,
        "consistency": 0.25,
        "depth": 0.1,
        "completeness": 0.05,
    },
)

AGGRESSIVE_CONFIG = AdaptiveConfig(
    analysis_model="heavy",
    tool_triggers={
        "low_confidence": {
            "threshold": 0.5,
            "tools": ["web_search", "code_search", "document_fetch"],
            "action": "ground",
        },
        "high_confidence": {
            "threshold": 0.75,
            "tools": ["nova_verify"],
            "action": "refute",
        },
        "contradiction": {
            "threshold": 0.4,
            "tools": ["web_search", "code_search", "document_fetch", "nova_verify"],
            "action": "resolve",
        },
    },
    perspective_elimination={
        "enabled": True,
        "quality_threshold": 0.6,
        "review_at_heights": [1, 2],
    },
    max_tool_calls_per_perspective=10,
    max_tool_calls_global=40,
    tool_timeout=AGGRESSIVE_TOOL_TIMEOUT_SECS,
    allowed_tools=["web_search", "code_search", "document_fetch", "nova_verify"],
    forbidden_tools=[],
    quality_score_weights={
        "confidence": 0.3,
        "consistency": 0.3,
        "depth": 0.25,
        "completeness": 0.15,
    },
)


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def normalize_confidence(value: Any) -> float:
    """Convert various confidence formats to [0, 1] float.
    
    Args:
        value: Can be float, int, percentage string, etc.
        
    Returns:
        Normalized confidence in [0, 1]
        
    Raises:
        ValueError: If value cannot be normalized
    """
    if isinstance(value, float):
        if 0.0 <= value <= 1.0:
            return value
        elif 0.0 <= value <= 100.0:
            return value / 100.0
        else:
            raise ValueError(f"Float confidence out of range: {value}")
    
    if isinstance(value, int):
        if 0 <= value <= 1:
            return float(value)
        elif 0 <= value <= 100:
            return value / 100.0
        else:
            raise ValueError(f"Int confidence out of range: {value}")
    
    if isinstance(value, str):
        # Try to extract percentage (e.g., "80%")
        match = re.search(r'(\d+(?:\.\d+)?)\s*%', value)
        if match:
            return float(match.group(1)) / 100.0
        
        # Try to parse as float
        try:
            parsed = float(value)
            return normalize_confidence(parsed)
        except ValueError:
            raise ValueError(f"Cannot normalize string confidence: {value}")
    
    raise ValueError(f"Unsupported confidence type: {type(value)}")


def infer_confidence_from_text(claim_text: str) -> float:
    """Heuristic: extract confidence from claim text.
    
    Examples:
        "I am 80% sure..." -> 0.8
        "Definitely true" -> 0.9
        "Probably false" -> 0.3
        
    Args:
        claim_text: The claim statement
        
    Returns:
        Inferred confidence [0, 1]
    """
    text_lower = claim_text.lower()
    
    # Explicit percentages
    match = re.search(r'(\d+(?:\.\d+)?)\s*%', text_lower)
    if match:
        return normalize_confidence(float(match.group(1)))
    
    # Confidence phrases
    high_confidence_words = {
        "certain", "definitely", "absolutely", "undoubtedly", "clearly",
        "obviously", "evidently", "certainly", "unquestionably"
    }
    medium_high_words = {
        "likely", "probably", "strongly", "appear", "seems"
    }
    medium_words = {
        "possibly", "might", "could", "suggest", "indicate"
    }
    low_words = {
        "unlikely", "rarely", "seldom", "probably not", "doubtful"
    }
    very_low_words = {
        "definitely not", "false", "wrong", "certainly not"
    }
    
    if any(word in text_lower for word in high_confidence_words):
        return 0.85
    if any(word in text_lower for word in medium_high_words):
        return 0.70
    if any(word in text_lower for word in medium_words):
        return 0.50
    if any(word in text_lower for word in low_words):
        return 0.30
    if any(word in text_lower for word in very_low_words):
        return 0.10
    
    # Default to medium confidence
    return 0.5


def infer_importance(claim_text: str) -> float:
    """Heuristic: estimate claim importance from length + signal words.
    
    Longer claims tend to be more important/detailed.
    Certain keywords signal importance.
    
    Args:
        claim_text: The claim statement
        
    Returns:
        Estimated importance [0, 1]
    """
    # Base score on length
    word_count = len(claim_text.split())
    length_score = min(word_count / 50.0, 1.0)  # max out at 50 words
    
    # Boost for important keywords
    text_lower = claim_text.lower()
    importance_keywords = {
        "crucial", "critical", "essential", "fundamental", "key",
        "primary", "main", "major", "significant", "important",
        "must", "should", "should not", "must not"
    }
    
    keyword_boost = 0.0
    if any(kw in text_lower for kw in importance_keywords):
        keyword_boost = 0.2
    
    # Combine scores
    importance = min(length_score + keyword_boost, 1.0)
    return importance


def infer_category(claim_text: str) -> str:
    """Heuristic: categorize claim as factual, opinion, procedural, or unknown.
    
    Args:
        claim_text: The claim statement
        
    Returns:
        Category string from ClaimCategory
    """
    text_lower = claim_text.lower()
    
    # Procedural: how-to, method-based
    procedural_keywords = {
        "should", "must", "how to", "process", "step", "procedure",
        "method", "approach", "technique", "implement", "configure",
        "set up", "install", "deploy"
    }
    if any(kw in text_lower for kw in procedural_keywords):
        return ClaimCategory.PROCEDURAL.value
    
    # Opinion: subjective assessment
    opinion_keywords = {
        "i think", "i believe", "in my opinion", "i feel", "i suggest",
        "seems", "appears", "probably", "likely", "personally",
        "arguably", "allegedly"
    }
    if any(kw in text_lower for kw in opinion_keywords):
        return ClaimCategory.OPINION.value
    
    # Factual: empirical statements
    factual_keywords = {
        "is", "are", "was", "were", "has", "have", "fact",
        "proven", "demonstrated", "shows", "indicates", "data",
        "found", "discovered", "confirmed"
    }
    if any(kw in text_lower for kw in factual_keywords):
        return ClaimCategory.FACTUAL.value
    
    # Default to unknown
    return ClaimCategory.UNKNOWN.value


def quality_score(analysis: PerspectiveAnalysis, config: Optional[AdaptiveConfig] = None) -> float:
    """Compute quality_score from components using weights.
    
    Quality = weighted sum of:
      - confidence: aggregate_confidence
      - consistency: 1 - (contradiction_count / total_claims)
      - depth: min(reasoning_depth / 10, 1.0)
      - completeness: completeness_score
    
    Args:
        analysis: The perspective analysis
        config: Optional config with weights; uses DEFAULT if not provided
        
    Returns:
        Quality score [0, 1]
    """
    if config is None:
        config = DEFAULT_ADAPTIVE_CONFIG
    
    weights = config.quality_score_weights
    
    # Component scores
    confidence_score = analysis.aggregate_confidence
    
    # Consistency: penalize contradictions
    consistency_score = 1.0
    if analysis.claims:
        contradiction_penalty = min(analysis.contradiction_count / len(analysis.claims), 1.0)
        consistency_score = 1.0 - contradiction_penalty * 0.5  # max 50% penalty
    
    # Depth: normalize reasoning steps
    depth_score = min(analysis.reasoning_depth / 10.0, 1.0)
    
    # Completeness: use directly
    completeness_score = analysis.completeness_score
    
    # Weighted sum
    quality = (
        weights.get("confidence", 0.4) * confidence_score +
        weights.get("consistency", 0.3) * consistency_score +
        weights.get("depth", 0.15) * depth_score +
        weights.get("completeness", 0.15) * completeness_score
    )
    
    return max(0.0, min(quality, 1.0))
