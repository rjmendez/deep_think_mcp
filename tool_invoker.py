"""Tool invoker engine for Phase 2 deep_think.

Orchestrates tool directive execution with:
- Priority-based sorting and execution order
- Serial execution for must-have tools (Priority 0)
- Parallel execution for high/medium priority batches
- Budget and timeout constraints
- Safety limits on tool calls per perspective/pass
"""

import asyncio
import logging
import time
from typing import List, Dict, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

try:
    from .models_invoker import (
        ToolDirective,
        ToolResult,
        ToolInvocationBatch,
        ToolInvocationConfig,
    )
except ImportError:  # pragma: no cover - support direct module imports in tests
    from models_invoker import (
        ToolDirective,
        ToolResult,
        ToolInvocationBatch,
        ToolInvocationConfig,
    )

log = logging.getLogger(__name__)


class ToolInvoker:
    """Executes ToolDirective batches with safety constraints."""
    
    def __init__(self, config: Optional[ToolInvocationConfig] = None):
        """Initialize tool invoker with configuration.
        
        Args:
            config: ToolInvocationConfig with safety constraints
        """
        self.config = config or ToolInvocationConfig()
        self.executor = ThreadPoolExecutor(max_workers=5)
    
    def invoke_tools(
        self,
        directives: List[ToolDirective],
        budget_remaining: int,
        perspective_id: str = "default",
        timeout: int = 10,
    ) -> ToolInvocationBatch:
        """Invoke tools in priority order with safety constraints.
        
        Execution order:
        1. Priority 0 (must): Execute serially, must complete
        2. Priority 1-2 (high/medium): Execute in parallel batches
        3. Priority 3 (exploratory): Only if budget_remaining > 5
        
        Args:
            directives: List of ToolDirective objects
            budget_remaining: Budget available for tool calls
            perspective_id: ID of perspective running this batch
            timeout: Timeout per tool call in seconds
            
        Returns:
            ToolInvocationBatch with results and metadata
        """
        # Validate budget
        if budget_remaining <= self.config.min_budget_remaining:
            log.warning(f"Budget exhausted: {budget_remaining} <= {self.config.min_budget_remaining}")
            return ToolInvocationBatch(
                perspective_id=perspective_id,
                directives=[],
                results=[],
                total_time_ms=0,
                budget_consumed=0,
            )
        
        # Sort directives by priority
        sorted_directives = self._sort_by_priority(directives)
        
        # Partition into batches by priority
        batch_0 = [d for d in sorted_directives if d.priority == 0]
        batch_1_2 = [d for d in sorted_directives if d.priority in (1, 2)]
        batch_3 = [d for d in sorted_directives if d.priority == 3]
        
        # Enforce max calls per perspective
        batch_0 = batch_0[:2]  # Max 2 must calls
        batch_1_2 = batch_1_2[:3]  # Max 3 high/medium calls
        
        # Check budget for exploratory
        total_calls = len(batch_0) + len(batch_1_2)
        if len(batch_3) > 0 and budget_remaining - total_calls > 5:
            batch_3 = batch_3[:1]  # Max 1 exploratory if budget allows
        else:
            batch_3 = []
        
        # Check against global ceiling
        all_directives = batch_0 + batch_1_2 + batch_3
        if len(all_directives) > self.config.max_tool_calls_per_pass:
            all_directives = all_directives[:self.config.max_tool_calls_per_pass]
        
        if len(all_directives) > budget_remaining:
            all_directives = all_directives[:budget_remaining]
        
        # Rebuild batches from trimmed all_directives to maintain invariant:
        # len(directives) == len(results)
        batch_0_trimmed = [d for d in all_directives if d.priority == 0]
        batch_1_2_trimmed = [d for d in all_directives if d.priority in (1, 2)]
        batch_3_trimmed = [d for d in all_directives if d.priority == 3]
        
        # Execute batches
        start_time = time.time()
        results = []
        
        # Execute Priority 0 (serial, must complete)
        if batch_0_trimmed:
            log.info(f"Executing {len(batch_0_trimmed)} must-have tool calls (Priority 0)")
            batch_0_results = self._execute_batch_serial(batch_0_trimmed, timeout)
            results.extend(batch_0_results)
        
        # Execute Priority 1-2 (parallel)
        if batch_1_2_trimmed:
            log.info(f"Executing {len(batch_1_2_trimmed)} high/medium priority calls (Priority 1-2)")
            batch_1_2_results = self._execute_batch_parallel(batch_1_2_trimmed, timeout)
            results.extend(batch_1_2_results)
        
        # Execute Priority 3 (parallel if budget allows)
        if batch_3_trimmed:
            log.info(f"Executing {len(batch_3_trimmed)} exploratory calls (Priority 3)")
            batch_3_results = self._execute_batch_parallel(batch_3_trimmed, timeout)
            results.extend(batch_3_results)
        
        total_time_ms = int((time.time() - start_time) * 1000)
        
        return ToolInvocationBatch(
            perspective_id=perspective_id,
            directives=all_directives,
            results=results,
            total_time_ms=total_time_ms,
            budget_consumed=len(results),
        )
    
    def _sort_by_priority(self, directives: List[ToolDirective]) -> List[ToolDirective]:
        """Sort directives by priority (ascending).
        
        Lower priority number = higher importance.
        
        Args:
            directives: List of ToolDirective objects
            
        Returns:
            Sorted list (Priority 0, then 1, 2, 3)
        """
        return sorted(directives, key=lambda d: d.priority)
    
    def _execute_batch_serial(
        self,
        directives: List[ToolDirective],
        timeout: int,
    ) -> List[ToolResult]:
        """Execute batch serially (must complete).
        
        Args:
            directives: List of ToolDirective objects
            timeout: Timeout per call in seconds
            
        Returns:
            List of ToolResult objects
        """
        results = []
        for directive in directives:
            result = self._invoke_single_tool(directive, timeout)
            results.append(result)
            log.debug(f"Tool {directive.tool_name} completed: {result.tool_status}")
        return results
    
    def _execute_batch_parallel(
        self,
        directives: List[ToolDirective],
        timeout: int,
    ) -> List[ToolResult]:
        """Execute batch in parallel using asyncio.
        
        Args:
            directives: List of ToolDirective objects
            timeout: Timeout per call in seconds
            
        Returns:
            List of ToolResult objects
        """
        # Use ThreadPoolExecutor to run in parallel
        futures = []
        for directive in directives:
            future = self.executor.submit(self._invoke_single_tool, directive, timeout)
            futures.append((directive, future))
        
        results = []
        for directive, future in futures:
            try:
                result = future.result(timeout=timeout + 2)
                results.append(result)
                log.debug(f"Tool {directive.tool_name} completed: {result.tool_status}")
            except FutureTimeoutError:
                log.warning(f"Tool {directive.tool_name} timed out in parallel batch")
                results.append(ToolResult(
                    tool_name=directive.tool_name,
                    query=directive.query,
                    results="",
                    tool_status="timeout",
                    timing_ms=timeout * 1000,
                    confidence_impact=-0.10,
                    error_message="Tool call timed out in parallel batch",
                ))
            except Exception as e:
                log.error(f"Tool {directive.tool_name} failed: {e}")
                results.append(ToolResult(
                    tool_name=directive.tool_name,
                    query=directive.query,
                    results="",
                    tool_status="error",
                    timing_ms=0,
                    confidence_impact=-0.05,
                    error_message=str(e),
                ))
        
        return results
    
    def _invoke_single_tool(
        self,
        directive: ToolDirective,
        timeout: int,
    ) -> ToolResult:
        """Invoke a single tool with timeout enforcement.
        
        Args:
            directive: ToolDirective specifying tool and query
            timeout: Timeout in seconds
            
        Returns:
            ToolResult with status, results, and timing
        """
        start_time = time.time()
        
        try:
            # Route to appropriate tool wrapper
            if directive.tool_name == "web_search":
                try:
                    from .tools.web_search import invoke_web_search
                except ImportError:  # pragma: no cover - support direct module imports in tests
                    from tools.web_search import invoke_web_search
                results, impact, error = invoke_web_search(directive.query, timeout)
            elif directive.tool_name == "code_search":
                try:
                    from .tools.code_search import invoke_code_search
                except ImportError:  # pragma: no cover - support direct module imports in tests
                    from tools.code_search import invoke_code_search
                results, impact, error = invoke_code_search(directive.query, timeout)
            elif directive.tool_name == "nova_verify":
                try:
                    from .tools.nova_verify import invoke_nova_verify
                except ImportError:  # pragma: no cover - support direct module imports in tests
                    from tools.nova_verify import invoke_nova_verify
                results, impact, error = invoke_nova_verify(directive.query, timeout)
            elif directive.tool_name == "document_fetch":
                try:
                    from .tools.document_fetch import invoke_document_fetch
                except ImportError:  # pragma: no cover - support direct module imports in tests
                    from tools.document_fetch import invoke_document_fetch
                results, impact, error = invoke_document_fetch(directive.query, timeout)
            else:
                return ToolResult(
                    tool_name=directive.tool_name,
                    query=directive.query,
                    results="",
                    tool_status="not_callable",
                    timing_ms=0,
                    confidence_impact=-0.05,
                    error_message=f"Unknown tool: {directive.tool_name}",
                )
            
            elapsed_ms = int((time.time() - start_time) * 1000)
            
            # Determine status
            if error:
                normalized_error = error.lower()
                if (
                    directive.tool_name == "code_search"
                    and "no local matches" in normalized_error
                ):
                    status = "success"
                    impact = max(impact, 0.0)
                    error = ""
                elif "timed out" in normalized_error:
                    status = "timeout"
                    impact = -0.10
                else:
                    status = "error"
                    impact = -0.05
            else:
                status = "success"
                # Impact already calculated by tool wrapper
            
            return ToolResult(
                tool_name=directive.tool_name,
                query=directive.query,
                results=results,
                tool_status=status,
                timing_ms=elapsed_ms,
                confidence_impact=impact,
                error_message=error,
            )
            
        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            log.error(f"Exception in tool {directive.tool_name}: {e}")
            return ToolResult(
                tool_name=directive.tool_name,
                query=directive.query,
                results="",
                tool_status="error",
                timing_ms=elapsed_ms,
                confidence_impact=-0.05,
                error_message=str(e),
            )
    
    def close(self):
        """Clean up thread pool executor."""
        self.executor.shutdown(wait=False)


def parse_tool_directives(directives: List[ToolDirective]) -> List[ToolDirective]:
    """Parse and sort tool directives by priority.
    
    Args:
        directives: List of ToolDirective objects
        
    Returns:
        Sorted list by priority (0 → 1 → 2 → 3)
    """
    return sorted(directives, key=lambda d: d.priority)


def format_evidence_for_reasoning(result: ToolResult, purpose: str) -> str:
    """Format tool result as evidence for reasoning context.
    
    Args:
        result: ToolResult from tool invocation
        purpose: Purpose of the tool call (ground, refute, resolve, validate)
        
    Returns:
        Formatted evidence string for integration into reasoning pass
    """
    lines = []
    lines.append(f"Tool: {result.tool_name}")
    lines.append(f"Query: {result.query}")
    lines.append(f"Purpose: {purpose}")
    lines.append(f"Status: {result.tool_status}")
    lines.append(f"Confidence impact: {result.confidence_impact:+.2f}")
    
    if result.tool_status == "success":
        # Truncate results to 500 chars
        truncated_results = result.results
        if len(truncated_results) > 500:
            truncated_results = truncated_results[:500] + "..."
        lines.append(f"\nResults:\n{truncated_results}")
        
        # Add interpretation guidance based on purpose
        if purpose == "ground":
            lines.append("\nInterpretation: Use this to support claims with external evidence.")
        elif purpose == "refute":
            lines.append("\nInterpretation: Examine for contradictions to test confidence.")
        elif purpose == "resolve":
            lines.append("\nInterpretation: Use this to resolve contradictions between passes.")
        elif purpose == "validate":
            lines.append("\nInterpretation: Verify claims against authoritative sources.")
    else:
        if result.error_message:
            lines.append(f"\nError: {result.error_message}")
        lines.append(f"Timing: {result.timing_ms}ms")
    
    return "\n".join(lines)


if __name__ == "__main__":
    # Example usage
    logging.basicConfig(level=logging.INFO)
    
    # Create some test directives
    directives = [
        ToolDirective(
            tool_name="web_search",
            query="Python asyncio best practices",
            priority=0,
            purpose="ground",
            expected_impact="Grounds reasoning with current best practices"
        ),
        ToolDirective(
            tool_name="code_search",
            query="asyncio timeout implementation",
            priority=1,
            purpose="validate",
            expected_impact="Validates pattern with real code examples"
        ),
        ToolDirective(
            tool_name="web_search",
            query="thread pool executor vs asyncio",
            priority=2,
            purpose="resolve",
            expected_impact="Resolves decision between threading models"
        ),
    ]
    
    # Invoke tools
    invoker = ToolInvoker()
    try:
        batch = invoker.invoke_tools(directives, budget_remaining=10)
        print(f"\nInvoked {batch.budget_consumed} tools in {batch.total_time_ms}ms")
        for result in batch.results:
            print(f"  - {result.tool_name}: {result.tool_status}")
    finally:
        invoker.close()
