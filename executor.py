"""
Phase 2 Part 3: Executor OODA Loop Implementation

Implements the execution orchestrator that runs the OODA loop per pass:
- Observe: Execute perspective reasoning
- Orient: Analyze output + route decision
- Decide: Queue tools based on routing
- Act: Execute tools + collect evidence

This glues Phase 1 (router) + Phase 2 Parts 1-2 (tool invoker + evidence manager)
into integrated agentic loop.
"""

import time
import logging
from typing import Optional, List, Tuple, Dict, Any
from dataclasses import dataclass

from .models_executor import (
    PerspectiveExecutionState,
    ExecutionPass,
    ExecutionConfig,
    ExecutionStatus,
)
from .models_adaptive import (
    PerspectiveAnalysis,
    RoutingDecision,
    RoutingAction,
)
from .models_evidence import (
    EvidenceDigest,
    EvidenceCache,
    ToolResult,
    ToolInvocationBatch,
)

from .analyzer import analyze_perspective
from .router import (
    route_reasoning_perspective,
    GlobalReasoningState,
    extract_quality_signals,
    PerspectiveQualityClassifier,
)
from .defaults import DEFAULT_REASONING_TIMEOUT_SECS


# ============================================================================
# LOGGING
# ============================================================================

logger = logging.getLogger(__name__)


# ============================================================================
# PHASE 1: OBSERVE - Execute Perspective Reasoning
# ============================================================================

def execute_reasoning(
    perspective_id: str,
    prompt: str,
    model: str,
    timeout: int = DEFAULT_REASONING_TIMEOUT_SECS,
) -> str:
    """
    Phase 1 OBSERVE: Execute perspective reasoning and capture output.
    
    Args:
        perspective_id: Identifier for this perspective
        prompt: The reasoning prompt to send to model
        model: Model name/tier to invoke
        timeout: Seconds to wait for response
    
    Returns:
        reasoning_output: Full text output from model, or error marker on failure
    
    This is a stub that would call the actual reasoning model.
    In integration, this would call the appropriate tier (light/medium/heavy).
    """
    try:
        # TODO: Call actual reasoning model (stubbed for Phase 2 Part 3)
        # This would be integrated with the worker/engine in production
        logger.info(f"[OBSERVE] perspective={perspective_id} model={model}")
        
        # For testing, return a mock response
        return f"Reasoning output for {perspective_id} from {model}"
    
    except TimeoutError:
        error_msg = f"[OBSERVE ERROR] Reasoning timeout after {timeout}s for perspective={perspective_id}"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"
    except Exception as e:
        error_msg = f"[OBSERVE ERROR] Failed to execute reasoning for perspective={perspective_id}: {str(e)}"
        logger.error(error_msg)
        return f"ERROR: {error_msg}"


# ============================================================================
# PHASE 2: ORIENT - Analyze Output + Route Decision
# ============================================================================

def analyze_and_route(
    reasoning_output: str,
    perspective_id: str,
    global_state: GlobalReasoningState,
) -> Tuple[Optional[PerspectiveAnalysis], Optional[RoutingDecision]]:
    """
    Phase 2 ORIENT: Analyze reasoning output and route decision.
    
    Args:
        reasoning_output: Raw text from Observe phase
        perspective_id: ID of this perspective
        global_state: Global state with budget, height, eliminated set
    
    Returns:
        Tuple of (analysis, routing_decision)
        Returns (None, None) if analysis fails
    
    Steps:
    1. analyzer.py: format_detection + claim_extraction
    2. analyzer.py: contradiction_detection
    3. analyzer.py: quality_scoring → PerspectiveAnalysis
    4. router.py: extract_quality_signals
    5. router.py: PerspectiveQualityClassifier
    6. router.py: route_reasoning_perspective() → RoutingDecision
    """
    try:
        # Step 1-3: Analyze reasoning output
        logger.info(f"[ORIENT] Starting analysis for perspective={perspective_id}")
        
        analysis = analyze_perspective(
            reasoning_output=reasoning_output,
            perspective_id=perspective_id,
            height=global_state.height,
            model_tier="medium",
        )
        
        if not analysis:
            logger.warning(f"[ORIENT] Analysis returned None for perspective={perspective_id}")
            return None, None
        
        logger.info(
            f"[ORIENT] Analysis complete: confidence={analysis.aggregate_confidence:.2f}, "
            f"contradictions={analysis.contradiction_count}, claims={len(analysis.claims)}"
        )
        
        # Step 4-5: Classify quality tier
        signals = extract_quality_signals(analysis)
        classifier = PerspectiveQualityClassifier()
        quality_tier, quality_confidence = classifier.classify(signals)
        
        logger.info(f"[ORIENT] Quality classification: tier={quality_tier} confidence={quality_confidence:.2f}")
        
        # Step 6: Route decision
        routing_decision = route_reasoning_perspective(
            analysis=analysis,
            quality_tier=quality_tier,
            quality_confidence=quality_confidence,
            global_state=global_state,
        )
        
        logger.info(
            f"[ORIENT] Routing decision: action={routing_decision.action} "
            f"tools={len(routing_decision.recommended_tools)}"
        )
        
        return analysis, routing_decision
    
    except Exception as e:
        logger.error(f"[ORIENT ERROR] Failed to analyze perspective={perspective_id}: {str(e)}")
        return None, None


# ============================================================================
# PHASE 3: DECIDE - Queue Tools Based on Routing
# ============================================================================

def queue_tools(
    routing_decision: RoutingDecision,
    budget_remaining: int,
    config: ExecutionConfig,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Phase 3 DECIDE: Filter and queue tools from routing decision.
    
    Args:
        routing_decision: Decision from Orient phase with recommended_tools
        budget_remaining: Current tool budget remaining
        config: ExecutionConfig with constraints
    
    Returns:
        Tuple of (queued_tools, estimated_budget_cost)
        - queued_tools: List of tool directives to invoke (may be empty if budget constraints violated)
        - estimated_budget_cost: Estimated cost of queued tools (1 per tool)
    
    Constraints:
    - Hard gate: budget_remaining > min_budget_to_invoke_tools (never go below)
    - Drop Priority 3 tools if budget < drop_priority_3_threshold
    - Drop Priority 2 tools if budget < drop_priority_2_threshold
    - Enforce per-tool timeout from config
    """
    
    # Hard gate: Check if we have budget
    if budget_remaining <= config.min_budget_to_invoke_tools:
        logger.info(f"[DECIDE] Hard gate triggered: budget={budget_remaining} <= min={config.min_budget_to_invoke_tools}")
        return [], 0
    
    # No tools recommended
    if not routing_decision.recommended_tools:
        logger.info(f"[DECIDE] No tools recommended in routing decision")
        return [], 0
    
    # Filter tools by budget and priority constraints
    queued_tools = []
    budget_left = budget_remaining - config.min_budget_to_invoke_tools  # Reserve budget
    
    for tool_directive in routing_decision.recommended_tools:
        # Skip based on priority and budget
        if tool_directive.priority == 3 and budget_left < config.drop_priority_3_if_budget_below:
            logger.info(f"[DECIDE] Dropping Priority 3 tool: {tool_directive.tool_name} (low budget)")
            continue
        
        if tool_directive.priority == 2 and budget_left < config.drop_priority_2_if_budget_below:
            logger.info(f"[DECIDE] Dropping Priority 2 tool: {tool_directive.tool_name} (low budget)")
            continue
        
        # Enforce timeout from config
        tool_dict = {
            "tool_name": tool_directive.tool_name,
            "query": tool_directive.query,
            "reason": tool_directive.reason,
            "priority": tool_directive.priority,
            "max_results": tool_directive.max_results,
            # Validate and clamp timeout to safe range [1, 600] seconds
            "timeout": max(1, min(min(tool_directive.timeout, config.tool_timeout), 600)),
        }
        queued_tools.append(tool_dict)
        budget_left -= 1  # Approximate: 1 budget per tool
    
    estimated_cost = len(queued_tools)
    logger.info(f"[DECIDE] Queued {len(queued_tools)} tools, estimated_cost={estimated_cost}, budget_left={budget_left}")
    return queued_tools, estimated_cost


def _map_router_reason_to_purpose(router_reason: str) -> str:
    """Map router-generated reasons to valid ToolResult purposes.
    
    Router reasons are complex (e.g., "ground_uncertain_apprentice")
    but ToolResult purposes are simple: {ground, refute, resolve, validate, unknown}
    
    Args:
        router_reason: Complex reason from router (may be None or string)
    
    Returns:
        Valid purpose: one of {ground, refute, resolve, validate, unknown}
    """
    if not router_reason:
        return "unknown"
    
    reason_lower = str(router_reason).lower()
    
    # Extract base purpose from complex router reason
    if "ground" in reason_lower:
        return "ground"
    elif "refute" in reason_lower or "stress_test" in reason_lower:
        return "refute"
    elif "resolve" in reason_lower or "contradiction" in reason_lower:
        return "resolve"
    elif "validate" in reason_lower or "verify" in reason_lower:
        return "validate"
    else:
        return "unknown"


# ============================================================================
# PHASE 4: ACT - Invoke Tools + Collect Evidence
# ============================================================================

def invoke_tools_and_digest(
    tools_queued: List[Dict[str, Any]],
    perspective_id: str,
    budget_remaining: int,
    original_confidence: float,
    config: ExecutionConfig,
    estimated_budget_cost: int,
    evidence_cache: Optional[EvidenceCache] = None,
    tool_invoker=None,
    evidence_manager=None,
) -> Tuple[Optional[EvidenceDigest], int]:
    """
    Phase 4 ACT: Invoke tools and process results into evidence.
    
    Args:
        tools_queued: List of tool directives from Decide phase
        perspective_id: Which perspective is using these tools
        budget_remaining: Current budget (will be updated)
        original_confidence: Perspective's confidence before tools
        config: ExecutionConfig with timeouts
        estimated_budget_cost: Estimated budget cost from queue_tools (1 per queued tool)
        evidence_cache: Optional cache for deduplication
        tool_invoker: ToolInvoker instance (optional, will import if None)
        evidence_manager: EvidenceManager instance (optional, will import if None)
    
    Returns:
        Tuple of (evidence_digest, budget_consumed)
        Returns (None, 0) if no tools or errors
        
    IMPORTANT: budget_consumed uses the ESTIMATED cost from queue_tools, not actual results count.
    This keeps DECIDE and ACT phases in sync. Both phases use the same budget calculation:
    1 budget unit per queued tool, regardless of actual execution results.
    
    Steps:
    1. Call tool_invoker.invoke_tools() to execute directives
    2. Call evidence_manager.process_batch() to create EvidenceDigest
    3. Calculate confidence delta from evidence
    4. Return digest with estimated budget_consumed (not actual results count)
    """
    
    if not tools_queued:
        logger.info(f"[ACT] No tools to invoke for perspective={perspective_id}")
        return None, 0
    
    try:
        logger.info(f"[ACT] Invoking {len(tools_queued)} tools for perspective={perspective_id}, estimated_cost={estimated_budget_cost}")
        
        # Import tool_invoker and evidence_manager if not provided
        if tool_invoker is None:
            try:
                from .tool_invoker import ToolInvoker
                tool_invoker = ToolInvoker()
            except ImportError:
                logger.warning("tool_invoker not available; using fallback mock")
                tool_invoker = None
        
        if evidence_manager is None:
            try:
                from .evidence_manager import EvidenceManager
                evidence_manager = EvidenceManager()
            except ImportError:
                logger.warning("evidence_manager not available; returning no evidence digest")
                return None, estimated_budget_cost
        
        # Convert tool directives dict to ToolDirective objects for invoker
        # Try to use real ToolDirective if models_invoker available
        tool_directives = None
        try:
            from models_invoker import ToolDirective as InvokerToolDirective
            tool_directives = [
                InvokerToolDirective(
                    tool_name=td["tool_name"],
                    query=td["query"],
                    priority=td.get("priority", 1),
                    purpose=_map_router_reason_to_purpose(td.get("reason", "unknown")),
                    expected_impact=td.get("expected_impact", "unknown"),
                )
                for td in tools_queued
            ]
        except ImportError:
            # Fallback: models_invoker not available, use dict representation
            logger.warning("models_invoker not available; using dict tool directives")
            tool_directives = [
                {
                    "tool_name": td["tool_name"],
                    "query": td["query"],
                    "priority": td.get("priority", 1),
                    "purpose": _map_router_reason_to_purpose(td.get("reason", "unknown")),
                    "expected_impact": td.get("expected_impact", "unknown"),
                    "perspective_id": perspective_id,  # ADD THIS (Fix p3-fix-003)
                }
                for td in tools_queued
            ]
        
        # Phase 4.1: Invoke tools via tool_invoker
        # Try to invoke tools; fallback to mock if invocation fails
        tool_results = []
        try:
            if tool_invoker:
                batch = tool_invoker.invoke_tools_batch(
                    directives=tool_directives,
                    budget_remaining=budget_remaining,
                    perspective_id=perspective_id,
                    timeout=config.tool_timeout,
                )
                tool_results = batch.results if batch else []
            else:
                logger.warning(f"[ACT] ToolInvoker not available; tool invocation will fail")
                raise ImportError("ToolInvoker not initialized")
        except (ImportError, AttributeError, Exception) as e:
            logger.warning(
                "[ACT] Tool invocation failed (%s: %s); returning no evidence",
                type(e).__name__,
                e,
            )
            return None, estimated_budget_cost
        
        if not tool_results:
            logger.info(f"[ACT] Tool invocation returned no results for perspective={perspective_id}")
            return None, 0
        
        # Phase 4.2: Process batch via evidence_manager to get EvidenceDigest
        evidence_digest = None
        try:
            from models_invoker import ToolInvocationBatch as InvokerToolInvocationBatch
            batch_cls = InvokerToolInvocationBatch
        except ImportError:
            batch_cls = ToolInvocationBatch

        if tool_results:
            total_time_ms = sum(r.execution_time_ms for r in tool_results)
        else:
            total_time_ms = 0

        batch = batch_cls(
            perspective_id=perspective_id,
            directives=tool_directives,
            results=tool_results,
            total_time_ms=total_time_ms,
            budget_consumed=len(tool_results),
        )
        evidence_digest = evidence_manager.process_batch(batch, original_confidence)
        
        budget_consumed = estimated_budget_cost
        
        # Fix Issue 3: Check if evidence_digest is None before dereferencing
        if evidence_digest:
            logger.info(
                f"[ACT] Tools executed: {len(tool_results)} results, "
                f"confidence_delta={evidence_digest.total_confidence_delta:.2f}, "
                f"budget_consumed={budget_consumed} (estimated from DECIDE phase)"
            )
        else:
            logger.info(f"[ACT] No evidence digest created for perspective={perspective_id}")
        
        return evidence_digest, budget_consumed
    
    except Exception as e:
        logger.error(f"[ACT ERROR] Failed to invoke tools for perspective={perspective_id}: {str(e)}")
        return None, 0


# ============================================================================
# EXECUTOR ORCHESTRATOR CLASS
# ============================================================================

class ExecutionOrchestrator:
    """
    Main orchestrator that runs the OODA loop per perspective per pass.
    
    Coordinates all 4 phases and manages execution state.
    """
    
    def __init__(
        self,
        config: Optional[ExecutionConfig] = None,
        evidence_cache: Optional[EvidenceCache] = None,
    ):
        """
        Initialize executor.
        
        Args:
            config: ExecutionConfig (uses defaults if None)
            evidence_cache: Optional evidence cache for deduplication
        """
        self.config = config or ExecutionConfig()
        self.evidence_cache = evidence_cache or EvidenceCache()
        logger.info(f"[EXECUTOR] Initialized with config: {self.config}")
    
    def execute_perspective(
        self,
        perspective_id: str,
        prompt: str,
        model: str,
        global_state: GlobalReasoningState,
        pass_number: int,
    ) -> PerspectiveExecutionState:
        """
        Execute full OODA loop for one perspective.
        
        Args:
            perspective_id: ID of perspective
            prompt: Reasoning prompt
            model: Model to use
            global_state: Global state with budget, height, etc.
            pass_number: Which pass is this
        
        Returns:
            PerspectiveExecutionState with full execution trace
        
        Flow:
        1. OBSERVE: Execute reasoning
        2. ORIENT: Analyze + route
        3. DECIDE: Queue tools
        4. ACT: Invoke tools + evidence
        5. Check elimination criteria
        """
        
        start_time = time.time()
        state = PerspectiveExecutionState(
            perspective_id=perspective_id,
            pass_number=pass_number,
            status="running",
        )
        
        logger.info(f"[EXECUTOR] Starting execution for perspective={perspective_id} pass={pass_number}")
        
        try:
            # ========== PHASE 1: OBSERVE ==========
            observe_start = time.time()
            
            state.reasoning_output = execute_reasoning(
                perspective_id=perspective_id,
                prompt=prompt,
                model=model,
                timeout=self.config.reasoning_timeout,
            )
            
            state.observe_ms = int((time.time() - observe_start) * 1000)
            logger.info(f"[EXECUTOR] OBSERVE complete: {state.observe_ms}ms")
            
            # Check for reasoning error
            if state.reasoning_output.startswith("ERROR:"):
                state.status = "error"
                state.error_message = state.reasoning_output
                state.timing_ms = int((time.time() - start_time) * 1000)
                return state
            
            # ========== PHASE 2: ORIENT ==========
            orient_start = time.time()
            
            state.analysis, state.routing_decision = analyze_and_route(
                reasoning_output=state.reasoning_output,
                perspective_id=perspective_id,
                global_state=global_state,
            )
            
            state.orient_ms = int((time.time() - orient_start) * 1000)
            logger.info(f"[EXECUTOR] ORIENT complete: {state.orient_ms}ms")
            
            # Check if analysis failed
            if not state.analysis or not state.routing_decision:
                state.status = "error"
                state.error_message = "Analysis failed to produce analysis/routing_decision"
                state.timing_ms = int((time.time() - start_time) * 1000)
                return state
            
            # Capture confidence before tools
            state.confidence_before = state.analysis.aggregate_confidence
            
            # ========== CHECK ELIMINATION BEFORE TOOLS ==========
            # Check if routing decision says DROP
            if state.routing_decision.action == RoutingAction.DROP.value:
                state.eliminated = True
                state.elimination_reason = "Routing decision: DROP"
                state.status = "eliminated"
                state.timing_ms = int((time.time() - start_time) * 1000)
                logger.info(f"[EXECUTOR] Perspective eliminated: {state.elimination_reason}")
                return state
            
            # ========== PHASE 3: DECIDE ==========
            decide_start = time.time()
            
            state.tools_queued, estimated_budget_cost = queue_tools(
                routing_decision=state.routing_decision,
                budget_remaining=global_state.tool_budget_remaining,
                config=self.config,
            )
            
            state.decide_ms = int((time.time() - decide_start) * 1000)
            logger.info(f"[EXECUTOR] DECIDE complete: {state.decide_ms}ms, queued={len(state.tools_queued)}, estimated_cost={estimated_budget_cost}")
            
            # ========== PHASE 4: ACT ==========
            act_start = time.time()
            
            state.evidence_digest, budget_consumed = invoke_tools_and_digest(
                tools_queued=state.tools_queued,
                perspective_id=perspective_id,
                budget_remaining=global_state.tool_budget_remaining,
                original_confidence=state.confidence_before,
                config=self.config,
                estimated_budget_cost=estimated_budget_cost,
                evidence_cache=self.evidence_cache,
            )
            
            state.act_ms = int((time.time() - act_start) * 1000)
            logger.info(f"[EXECUTOR] ACT complete: {state.act_ms}ms, budget_consumed={budget_consumed}")
            
            # Update global budget
            global_state.tool_budget_remaining -= budget_consumed
            
            # Update confidence after evidence
            if state.evidence_digest:
                state.confidence_after = state.evidence_digest.updated_confidence
            else:
                state.confidence_after = state.confidence_before
            
            # ========== CHECK ELIMINATION CRITERIA ==========
            # Check elimination by low confidence after tools
            if state.confidence_after < self.config.elimination_threshold:
                state.eliminated = True
                state.elimination_reason = f"Confidence below threshold: {state.confidence_after:.2f} < {self.config.elimination_threshold:.2f}"
                state.status = "eliminated"
                logger.info(f"[EXECUTOR] Perspective eliminated: {state.elimination_reason}")
            else:
                state.status = "complete"
            
            # ========== FINALIZE ==========
            state.timing_ms = int((time.time() - start_time) * 1000)
            logger.info(
                f"[EXECUTOR] Execution complete for perspective={perspective_id}: "
                f"status={state.status} eliminated={state.eliminated} "
                f"confidence_before={state.confidence_before:.2f} → "
                f"confidence_after={state.confidence_after:.2f} "
                f"timing={state.timing_ms}ms"
            )
            
            return state
        
        except Exception as e:
            logger.error(f"[EXECUTOR ERROR] Unexpected error for perspective={perspective_id}: {str(e)}")
            state.status = "error"
            state.error_message = str(e)
            state.timing_ms = int((time.time() - start_time) * 1000)
            return state
    
    def execute_pass(
        self,
        perspectives: List[str],
        pass_number: int,
        height: int,
        model: str,
        global_budget: int,
        prompt_template: str = "Reason about: {perspective}",
    ) -> ExecutionPass:
        """
        Execute one complete pass (height) across all perspectives.
        
        Args:
            perspectives: List of perspective IDs to execute
            pass_number: Which pass (1-indexed)
            height: Height within fan-out
            model: Model to use
            global_budget: Total tool budget for this pass
            prompt_template: Template for reasoning prompts
        
        Returns:
            ExecutionPass with aggregated metrics and results
        """
        
        start_time = time.time()
        
        execution_pass = ExecutionPass(
            pass_number=pass_number,
            height=height,
            budget_remaining=global_budget,
        )
        
        # Create global state
        global_state = GlobalReasoningState(
            height=height,
            tool_budget_remaining=global_budget,
        )
        
        # Explicitly clear eliminated_perspectives for this pass (Fix p3-fix-005)
        global_state.eliminated_perspectives = set()
        
        logger.info(f"[EXECUTOR PASS] Starting pass={pass_number} height={height} perspectives={len(perspectives)}")
        
        # Execute each perspective
        for perspective_id in perspectives:
            # Skip if already eliminated
            if perspective_id in global_state.eliminated_perspectives:
                logger.info(f"[EXECUTOR PASS] Skipping eliminated perspective={perspective_id}")
                continue
            
            # Create prompt
            prompt = prompt_template.format(perspective=perspective_id)
            
            # Execute OODA loop
            state = self.execute_perspective(
                perspective_id=perspective_id,
                prompt=prompt,
                model=model,
                global_state=global_state,
                pass_number=pass_number,
            )
            
            # Store result
            execution_pass.results[perspective_id] = state
            
            # Track elimination
            if state.eliminated:
                global_state.eliminated_perspectives.add(perspective_id)
                execution_pass.elimination_reasons[perspective_id] = state.elimination_reason or "Unknown"
            
            # Check max eliminations per pass - use elimination_reasons length which is updated in real-time
            if len(execution_pass.elimination_reasons) >= self.config.max_perspectives_eliminated_per_pass:
                logger.warning(
                    f"[EXECUTOR PASS] Reached maximum elimination limit "
                    f"({self.config.max_perspectives_eliminated_per_pass}) at height {execution_pass.height}, stopping"
                )
                break
        
        # Finalize metrics
        execution_pass.budget_remaining = global_state.tool_budget_remaining
        execution_pass.budget_consumed_this_pass = global_budget - global_state.tool_budget_remaining
        execution_pass.total_time_ms = int((time.time() - start_time) * 1000)
        execution_pass.total_tool_calls = sum(len(s.tools_queued) for s in execution_pass.results.values())
        
        logger.info(
            f"[EXECUTOR PASS] Pass complete: "
            f"perspectives={len(execution_pass.perspectives_active)} active, "
            f"{len(execution_pass.perspectives_eliminated)} eliminated, "
            f"tools={execution_pass.total_tool_calls}, "
            f"budget_remaining={execution_pass.budget_remaining}, "
            f"time={execution_pass.total_time_ms}ms"
        )
        
        return execution_pass
