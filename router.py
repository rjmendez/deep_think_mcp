"""
Phase 1 Part 3: Perspective Output Router

Implements the decision tree for adaptive routing of perspective analyses.
Consumes PerspectiveAnalysis from the analyzer and outputs RoutingDecision
with tool directives and reason codes for auditability.

Architecture:
1. Perspective Quality Classifier (stub for Phase 2c training)
2. Hard gates (4 sequential checks)
3. Main decision tree (6 phases by quality tier)
4. Hysteresis mechanism (prevent flip-flopping)
5. Tool directive creation with priority handling
6. Comprehensive reason code tracking
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum

try:
    from .models_adaptive import (
        PerspectiveAnalysis,
        ToolDirective,
        RoutingDecision,
        RoutingAction,
        AdaptiveConfig,
        DEFAULT_ADAPTIVE_CONFIG,
    )
except ImportError:  # pragma: no cover - support direct module imports in tests
    from models_adaptive import (
        PerspectiveAnalysis,
        ToolDirective,
        RoutingDecision,
        RoutingAction,
        AdaptiveConfig,
        DEFAULT_ADAPTIVE_CONFIG,
    )
try:
    from .defaults import DEFAULT_TOOL_TIMEOUT_SECS
except ImportError:  # pragma: no cover - support direct module imports in tests
    from defaults import DEFAULT_TOOL_TIMEOUT_SECS


# ============================================================================
# 1. GLOBAL STATE & QUALITY SIGNALS
# ============================================================================

@dataclass
class GlobalReasoningState:
    """
    Global state tracking across all perspectives at a given height.
    Passed to router to make context-aware decisions.
    """
    height: int
    tool_budget_remaining: int  # Global tool call budget
    eliminated_perspectives: set = field(default_factory=set)  # ID set
    prior_routes: Dict[str, str] = field(default_factory=dict)  # perspective_id -> action
    hysteresis_thresholds: Dict[str, float] = field(default_factory=dict)  # hysteresis deadbands
    
    def tool_budget_available(self) -> bool:
        """Check if global tool budget remains."""
        return self.tool_budget_remaining > 10
    
    def is_eliminated(self, perspective_id: str) -> bool:
        """Check if perspective was already eliminated."""
        return perspective_id in self.eliminated_perspectives


@dataclass
class PerspectiveQualitySignals:
    """
    8 features extracted from PerspectiveAnalysis for quality classification.
    Input to PerspectiveQualityClassifier.
    """
    confidence_mean: float          # Mean confidence of claims
    confidence_stdev: float         # Std dev (coherence measure)
    uncertainty_ratio: float        # uncertainties / claims
    contradiction_count: int        # Count of contradictions
    reasoning_depth: int            # Number of reasoning steps
    claim_count: int                # Total claims extracted
    parse_quality: str              # "pristine" | "heuristic" | "fallback"
    modality_diversity: float       # 0.0-1.0: topic range coverage
    
    def __post_init__(self):
        """Validate signal ranges."""
        if not 0.0 <= self.confidence_mean <= 1.0:
            raise ValueError(f"confidence_mean must be in [0, 1], got {self.confidence_mean}")
        if not 0.0 <= self.confidence_stdev <= 1.0:
            raise ValueError(f"confidence_stdev must be in [0, 1], got {self.confidence_stdev}")
        if not 0.0 <= self.uncertainty_ratio <= 1.0:
            raise ValueError(f"uncertainty_ratio must be in [0, 1], got {self.uncertainty_ratio}")
        if not 0.0 <= self.modality_diversity <= 1.0:
            raise ValueError(f"modality_diversity must be in [0, 1], got {self.modality_diversity}")
        
        valid_qualities = {"pristine", "heuristic", "fallback"}
        if self.parse_quality not in valid_qualities:
            raise ValueError(f"parse_quality must be one of {valid_qualities}, got {self.parse_quality}")


# ============================================================================
# 2. PERSPECTIVE QUALITY CLASSIFIER (Stub for Phase 2c)
# ============================================================================

class PerspectiveQualityClassifier:
    """
    Stub classifier using heuristic predictions.
    Will be replaced with trained ML model in Phase 2c.
    
    Tiers: "novice", "apprentice", "expert", "master"
    """
    
    def classify(self, signals: PerspectiveQualitySignals) -> Tuple[str, float]:
        """
        Classify perspective quality based on signals.
        
        Args:
            signals: PerspectiveQualitySignals with 8 features
        
        Returns:
            (tier, confidence) where tier in ["novice", "apprentice", "expert", "master"]
            and confidence in [0.0, 1.0]
        """
        # Heuristic implementation: deterministic scoring
        # Will be replaced with trained classifier in Phase 2c
        
        # Compute score: weighted combination of signals
        score = (
            signals.confidence_mean * 0.35      # Confidence is primary signal
            + (1.0 - signals.contradiction_count / max(1, signals.claim_count)) * 0.25  # Low contradictions
            + (1.0 - signals.uncertainty_ratio) * 0.20  # Low uncertainty
            + (signals.reasoning_depth / max(10, signals.claim_count)) * 0.10  # Reasoning depth
            + (1.0 if signals.parse_quality == "pristine" else 
               0.7 if signals.parse_quality == "heuristic" else 0.4) * 0.10  # Parse quality
        )
        
        # Clamp score to [0, 1]
        score = min(1.0, max(0.0, score))
        
        # Assign tier based on score + confidence in prediction
        if score >= 0.85:
            tier = "master"
            confidence = 0.95 if signals.parse_quality == "pristine" else 0.80
        elif score >= 0.70:
            tier = "expert"
            confidence = 0.90 if signals.parse_quality in ["pristine", "heuristic"] else 0.70
        elif score >= 0.50:
            tier = "apprentice"
            confidence = 0.85
        else:
            tier = "novice"
            confidence = 0.80 if signals.parse_quality != "fallback" else 0.60
        
        return tier, confidence


# ============================================================================
# 3. ROUTING ACTIONS & HELPERS
# ============================================================================

# Map internal routing decisions to schema actions
# Internal decision → Schema action mapping
ROUTING_ACTION_MAP = {
    "continue_without_tools": RoutingAction.CONTINUE.value,
    "ground_via_search": RoutingAction.CONTINUE_WITH_TOOLS.value,
    "resolve_contradiction": RoutingAction.CONTINUE_WITH_TOOLS.value,
    "stress_test_high_confidence": RoutingAction.CONTINUE_WITH_TOOLS.value,
    "ground_weak_perspective": RoutingAction.CONTINUE_WITH_TOOLS.value,
    "skip_no_budget": RoutingAction.DROP.value,
    "skip_eliminated": RoutingAction.DROP.value,
}


# ============================================================================
# 4. TOOL DIRECTIVE CREATION
# ============================================================================

def create_tool_directive(
    reason: str,
    analysis: PerspectiveAnalysis,
    priority: int,
    search_intent: str = "evidence",
    max_results: int = 5,
    timeout: float = DEFAULT_TOOL_TIMEOUT_SECS,
) -> ToolDirective:
    """
    Create a ToolDirective with proper query extraction and configuration.
    
    Args:
        reason: Routing reason (e.g., "low_confidence", "contradiction_resolve")
        analysis: PerspectiveAnalysis containing context for query
        priority: 0=must, 1=high, 2=medium, 3=exploratory
        search_intent: "evidence", "refutation", "grounding", "exploration"
        max_results: Default 5; can be overridden
        timeout: Per-tool timeout in seconds
    
    Returns:
        ToolDirective configured for tool invoker
    """
    
    # Extract query based on reason
    if "contradiction" in reason:
        query = _compute_contradiction_query(analysis)
        tool_name = "web_search"
    elif "low_confidence" in reason or "ground" in reason:
        query = _compute_grounding_query(analysis)
        tool_name = "web_search"
    elif "refutation" in reason or "stress" in reason:
        query = _compute_refutation_query(analysis)
        tool_name = "web_search"
    else:
        # Fallback: use aggregate claim summary
        query = _compute_grounding_query(analysis)
        tool_name = "web_search"
    
    return ToolDirective(
        tool_name=tool_name,
        query=query,
        reason=reason,
        perspective_id=analysis.perspective_id,
        priority=priority,
        expected_impact=search_intent,
        max_results=max_results,
        timeout=int(timeout),
    )


def _compute_contradiction_query(analysis: PerspectiveAnalysis) -> str:
    """Extract contradiction topic for web search."""
    if analysis.internal_contradictions:
        contradiction = analysis.internal_contradictions[0]
        # Extract key terms from contradiction claims
        terms = set()
        for claim in analysis.claims[:2]:  # Use top 2 claims
            words = claim.text.split()[:5]  # First 5 words
            terms.update(words)
        return " ".join(sorted(terms)[:5])
    
    # Fallback: use highest confidence claims
    if analysis.claims:
        sorted_claims = sorted(analysis.claims, key=lambda c: c.confidence, reverse=True)
        return " ".join(sorted_claims[0].text.split()[:5])
    
    return "contradiction resolution"


def _compute_grounding_query(analysis: PerspectiveAnalysis) -> str:
    """Extract grounding/evidence topic for web search."""
    if not analysis.claims:
        return "evidence grounding"
    
    # Use lowest confidence claims for grounding
    sorted_claims = sorted(analysis.claims, key=lambda c: c.confidence)
    for claim in sorted_claims[:2]:
        if claim.confidence < 0.7:
            return " ".join(claim.text.split()[:7])
    
    # Fallback: any claim
    return " ".join(analysis.claims[0].text.split()[:7])


def _compute_refutation_query(analysis: PerspectiveAnalysis) -> str:
    """Extract refutation/stress-test topic for web search."""
    if not analysis.claims:
        return "refutation check"
    
    # Use highest confidence claims for stress-testing
    sorted_claims = sorted(analysis.claims, key=lambda c: c.confidence, reverse=True)
    return " ".join(sorted_claims[0].text.split()[:7])


# ============================================================================
# 5. HARD GATES
# ============================================================================

def _check_hard_gate_1(
    quality_tier: str,
    quality_confidence: float,
    global_state: GlobalReasoningState,
    analysis: PerspectiveAnalysis,
) -> Optional[Tuple[str, Optional[ToolDirective], List[str]]]:
    """
    Hard Gate 1: Novice + High Confidence Classifier
    
    If classifier is very confident perspective is novice, ground it if budget permits.
    """
    if quality_tier == "novice" and quality_confidence > 0.90:
        reason_codes = ["hard_gate_novice_high_confidence"]
        
        if global_state.tool_budget_available():
            directive = create_tool_directive(
                reason="ground_weak_novice",
                analysis=analysis,
                priority=2,
                search_intent="grounding",
            )
            return (
                ROUTING_ACTION_MAP["ground_weak_perspective"],
                directive,
                reason_codes,
            )
        else:
            return (
                ROUTING_ACTION_MAP["skip_no_budget"],
                None,
                reason_codes + ["insufficient_budget"],
            )
    
    return None


def _check_hard_gate_2(
    analysis: PerspectiveAnalysis,
    global_state: GlobalReasoningState,
) -> Optional[Tuple[str, Optional[ToolDirective], List[str]]]:
    """
    Hard Gate 2: Extremely High Confidence
    
    If confidence >= 0.95, stress-test if early height, else skip tools.
    """
    if analysis.aggregate_confidence >= 0.95:
        reason_codes = ["hard_gate_high_confidence"]
        
        if global_state.height < 3:
            directive = create_tool_directive(
                reason="stress_test_high_confidence",
                analysis=analysis,
                priority=1,
                search_intent="refutation",
            )
            return (
                ROUTING_ACTION_MAP["stress_test_high_confidence"],
                directive,
                reason_codes,
            )
        else:
            return (
                ROUTING_ACTION_MAP["continue_without_tools"],
                None,
                reason_codes + ["height_too_advanced_for_stress_test"],
            )
    
    return None


def _check_hard_gate_3(
    global_state: GlobalReasoningState,
) -> Optional[Tuple[str, Optional[ToolDirective], List[str]]]:
    """
    Hard Gate 3: Out of Budget
    
    If global tool budget exhausted, skip all tools.
    """
    if not global_state.tool_budget_available():
        return (
            ROUTING_ACTION_MAP["skip_no_budget"],
            None,
            ["hard_gate_budget_exhausted"],
        )
    
    return None


def _check_hard_gate_4(
    perspective_id: str,
    global_state: GlobalReasoningState,
) -> Optional[Tuple[str, Optional[ToolDirective], List[str]]]:
    """
    Hard Gate 4: Already Eliminated
    
    If perspective was eliminated in prior height, skip it.
    """
    if global_state.is_eliminated(perspective_id):
        return (
            ROUTING_ACTION_MAP["skip_eliminated"],
            None,
            ["hard_gate_perspective_eliminated"],
        )
    
    return None


# ============================================================================
# 6. MAIN DECISION TREE
# ============================================================================

def _route_by_tier(
    quality_tier: str,
    analysis: PerspectiveAnalysis,
    global_state: GlobalReasoningState,
) -> Tuple[str, Optional[ToolDirective], List[str]]:
    """
    Main decision tree: Route by quality tier.
    
    6 phases:
    1. Master tier: Check for contradictions
    2. Expert tier: Check for contradictions
    3. Apprentice tier: Check for low confidence
    4-6. Novice fallback: Always ground with search
    """
    
    if quality_tier in ["master", "expert"]:
        # Phase 1-2: Master/Expert tier
        reason_codes = [f"{quality_tier}_perspective"]
        
        if analysis.contradiction_severity > 0.60:
            directive = create_tool_directive(
                reason="resolve_contradiction_expert",
                analysis=analysis,
                priority=0,  # Highest priority
                search_intent="evidence",
            )
            return (
                ROUTING_ACTION_MAP["resolve_contradiction"],
                directive,
                reason_codes + ["contradiction_severity_high"],
            )
        else:
            return (
                ROUTING_ACTION_MAP["continue_without_tools"],
                None,
                reason_codes + ["coherent_perspective"],
            )
    
    elif quality_tier == "apprentice":
        # Phase 3: Apprentice tier
        reason_codes = ["apprentice_perspective"]

        # Contradiction check mirrors master/expert: severity > 0.60 → resolve with tools
        if analysis.contradiction_severity > 0.60:
            directive = create_tool_directive(
                reason="resolve_contradiction_apprentice",
                analysis=analysis,
                priority=1,
                search_intent="evidence",
            )
            return (
                ROUTING_ACTION_MAP["resolve_contradiction"],
                directive,
                reason_codes + ["contradiction_severity_high"],
            )
        elif analysis.aggregate_confidence < 0.50:
            directive = create_tool_directive(
                reason="ground_uncertain_apprentice",
                analysis=analysis,
                priority=1,
                search_intent="grounding",
            )
            return (
                ROUTING_ACTION_MAP["ground_via_search"],
                directive,
                reason_codes + ["low_confidence"],
            )
        else:
            return (
                ROUTING_ACTION_MAP["continue_without_tools"],
                None,
                reason_codes + ["acceptable_confidence"],
            )
    
    else:
        # Phase 4-6: Novice fallback
        reason_codes = ["novice_perspective"]
        directive = create_tool_directive(
            reason="ground_weak_novice",
            analysis=analysis,
            priority=2,
            search_intent="grounding",
        )
        return (
            ROUTING_ACTION_MAP["ground_weak_perspective"],
            directive,
            reason_codes,
        )


# ============================================================================
# 7. HYSTERESIS (Prevent Flip-Flopping)
# ============================================================================

def _apply_hysteresis(
    perspective_id: str,
    base_action: str,
    base_reason_codes: List[str],
    analysis: PerspectiveAnalysis,
    global_state: GlobalReasoningState,
) -> Tuple[str, List[str]]:
    """
    Apply hysteresis to prevent routing flip-flop.
    
    If confidence is near a threshold, stick with prior routing if it exists.
    Deadband logic: if signal within deadband of prior threshold, maintain prior route.
    """
    
    perspective_id_str = str(perspective_id)
    prior_action = global_state.prior_routes.get(perspective_id_str)
    
    if not prior_action or prior_action == base_action:
        # No prior route or route unchanged
        return base_action, base_reason_codes
    
    # Check if confidence is in deadband zone
    deadband = 0.05  # 5% deadband
    
    # Determine threshold based on prior action
    if prior_action == RoutingAction.CONTINUE.value:
        # Confidence was high enough to skip tools; allow some degradation
        threshold = 0.45
    elif prior_action == RoutingAction.CONTINUE_WITH_TOOLS.value:
        # Confidence was low; require significant improvement to skip tools
        threshold = 0.60
    else:
        # For other actions, be conservative
        threshold = 0.5
    
    # If confidence within deadband of threshold, stick with prior route
    if abs(analysis.aggregate_confidence - threshold) < deadband:
        reason_codes = base_reason_codes + ["hysteresis_prevents_route_flip"]
        return prior_action, reason_codes
    
    return base_action, base_reason_codes


# ============================================================================
# 8. MAIN ROUTING FUNCTION
# ============================================================================

def route_reasoning_perspective(
    analysis: PerspectiveAnalysis,
    quality_tier: str,
    quality_confidence: float,
    global_state: GlobalReasoningState,
    adaptive_config: AdaptiveConfig = DEFAULT_ADAPTIVE_CONFIG,
) -> RoutingDecision:
    """
    Main router function: Consume PerspectiveAnalysis, output RoutingDecision.
    
    Process:
    1. Check 4 hard gates in sequence
    2. If no gate fires, apply main decision tree
    3. Apply hysteresis to prevent flip-flopping
    4. Create RoutingDecision with tool directive + reason codes
    
    Args:
        analysis: PerspectiveAnalysis from analyzer
        quality_tier: From classifier ("novice", "apprentice", "expert", "master")
        quality_confidence: Classifier confidence in tier prediction [0, 1]
        global_state: GlobalReasoningState with budget, height, eliminated set
        adaptive_config: Configuration with thresholds
    
    Returns:
        RoutingDecision with action, tool_directive, reason_codes
    """
    
    # ========== HARD GATES (sequential checks) ==========
    
    # Gate 4 first (fastest check): Already eliminated
    gate_4_result = _check_hard_gate_4(analysis.perspective_id, global_state)
    if gate_4_result:
        action, directive, reason_codes = gate_4_result
        return RoutingDecision(
            perspective_id=analysis.perspective_id,
            action=action,
            recommended_tools=[directive] if directive else [],
            decision_basis=reason_codes,
            should_eliminate=True,
        )
    
    # Gate 3: Out of budget
    gate_3_result = _check_hard_gate_3(global_state)
    if gate_3_result:
        action, directive, reason_codes = gate_3_result
        return RoutingDecision(
            perspective_id=analysis.perspective_id,
            action=action,
            recommended_tools=[directive] if directive else [],
            decision_basis=reason_codes,
        )
    
    # Gate 2: Extremely high confidence
    gate_2_result = _check_hard_gate_2(analysis, global_state)
    if gate_2_result:
        action, directive, reason_codes = gate_2_result
        # Apply hysteresis
        action, reason_codes = _apply_hysteresis(
            analysis.perspective_id,
            action,
            reason_codes,
            analysis,
            global_state,
        )
        return RoutingDecision(
            perspective_id=analysis.perspective_id,
            action=action,
            recommended_tools=[directive] if directive else [],
            decision_basis=reason_codes,
        )
    
    # Gate 1: Novice + High Confidence
    gate_1_result = _check_hard_gate_1(
        quality_tier,
        quality_confidence,
        global_state,
        analysis,
    )
    if gate_1_result:
        action, directive, reason_codes = gate_1_result
        # Apply hysteresis
        action, reason_codes = _apply_hysteresis(
            analysis.perspective_id,
            action,
            reason_codes,
            analysis,
            global_state,
        )
        return RoutingDecision(
            perspective_id=analysis.perspective_id,
            action=action,
            recommended_tools=[directive] if directive else [],
            decision_basis=reason_codes,
        )
    
    # ========== MAIN DECISION TREE ==========
    
    action, directive, reason_codes = _route_by_tier(
        quality_tier,
        analysis,
        global_state,
    )
    
    # Apply hysteresis
    action, reason_codes = _apply_hysteresis(
        analysis.perspective_id,
        action,
        reason_codes,
        analysis,
        global_state,
    )
    
    # ========== CREATE ROUTING DECISION ==========
    
    # Determine priority adjustment based on action
    if action == ROUTING_ACTION_MAP["resolve_contradiction"]:
        priority_adjustment = 2.0  # High priority
    elif action == ROUTING_ACTION_MAP["stress_test_high_confidence"]:
        priority_adjustment = 1.5  # Elevated priority
    elif action == ROUTING_ACTION_MAP["ground_via_search"]:
        priority_adjustment = 1.2  # Slightly elevated
    else:
        priority_adjustment = 1.0  # Normal
    
    return RoutingDecision(
        perspective_id=analysis.perspective_id,
        action=action,
        recommended_tools=[directive] if directive else [],
        decision_basis=reason_codes,
        priority_adjustment=priority_adjustment,
        should_eliminate=(action == ROUTING_ACTION_MAP["skip_no_budget"] or
                         action == ROUTING_ACTION_MAP["skip_eliminated"]),
    )


# ============================================================================
# 9. CONVENIENCE FUNCTIONS FOR TESTING & INTEGRATION
# ============================================================================

def extract_quality_signals(analysis: PerspectiveAnalysis) -> PerspectiveQualitySignals:
    """Extract quality signals from PerspectiveAnalysis for classification."""
    
    # Compute mean confidence
    if analysis.claims:
        confidence_mean = sum(c.confidence for c in analysis.claims) / len(analysis.claims)
        # Compute stdev
        if len(analysis.claims) == 1:
            confidence_stdev = 0.0
        else:
            variance = sum((c.confidence - confidence_mean) ** 2 for c in analysis.claims) / len(analysis.claims)
            confidence_stdev = variance ** 0.5
    else:
        confidence_mean = 0.0
        confidence_stdev = 0.0
    
    return PerspectiveQualitySignals(
        confidence_mean=min(1.0, confidence_mean),
        confidence_stdev=min(1.0, confidence_stdev),
        uncertainty_ratio=analysis.uncertainty_ratio,
        contradiction_count=analysis.contradiction_count,
        reasoning_depth=analysis.reasoning_depth,
        claim_count=len(analysis.claims),
        parse_quality="pristine",  # Will be set by analyzer
        modality_diversity=analysis.completeness_score,  # Proxy
    )


def route_perspective_auto(
    analysis: PerspectiveAnalysis,
    global_state: GlobalReasoningState,
    adaptive_config: AdaptiveConfig = DEFAULT_ADAPTIVE_CONFIG,
) -> RoutingDecision:
    """
    Convenience function: Classify perspective quality, then route.
    
    Combines classifier + router in one call for testing.
    """
    # Step 1: Classify quality
    classifier = PerspectiveQualityClassifier()
    signals = extract_quality_signals(analysis)
    quality_tier, quality_confidence = classifier.classify(signals)
    
    # Step 2: Route
    return route_reasoning_perspective(
        analysis=analysis,
        quality_tier=quality_tier,
        quality_confidence=quality_confidence,
        global_state=global_state,
        adaptive_config=adaptive_config,
    )
