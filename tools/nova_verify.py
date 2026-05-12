"""Nova verification tool wrapper for deep_think tool invoker.

Wraps the Nova verification client and formats verification results.
"""

import asyncio
import logging
import time
from typing import Tuple

log = logging.getLogger(__name__)


def invoke_nova_verify(claim: str, timeout: int = 10) -> Tuple[str, float, str]:
    """Invoke Nova verification to ground claims against Great Library.
    
    Args:
        claim: Claim to verify
        timeout: Timeout in seconds
        
    Returns:
        Tuple of (formatted_results: str, confidence_impact: float, error_message: str)
        
    Raises:
        TimeoutError: If tool call exceeds timeout
        Exception: On tool invocation errors
    """
    log.debug(f"Invoking nova_verify with claim: {claim[:100]}...")
    start_time = time.time()
    
    try:
        from nova_factcheck.nova_client import NovaVerificationClient

        async def _run() -> object:
            async with NovaVerificationClient(timeout_s=timeout) as client:
                # Hard-cap the entire retry loop at `timeout` seconds.
                # Without this, the client retries up to 3× before the outer
                # asyncio.run() returns, holding a ThreadPoolExecutor slot for
                # ~3× timeout even after tool_invoker has already recorded a timeout.
                return await asyncio.wait_for(client.verify(claim), timeout=timeout)

        result = asyncio.run(_run())
        
        elapsed_ms = int((time.time() - start_time) * 1000)
        
        # Format verification results
        error_msg = _extract_nova_error(result)
        formatted, impact = _format_nova_results(result)

        if error_msg:
            log.warning(f"nova_verify returned error state in {elapsed_ms}ms: {error_msg}")
            return formatted, impact, error_msg

        log.info(f"nova_verify succeeded in {elapsed_ms}ms")
        return formatted, impact, ""
        
    except TimeoutError:
        elapsed_ms = int((time.time() - start_time) * 1000)
        log.warning(f"nova_verify timed out after {elapsed_ms}ms")
        return "", -0.10, "Tool call timed out"
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        error_msg = str(e)
        log.error(f"nova_verify failed: {error_msg}")
        return "", -0.05, error_msg


def _format_nova_results(result: any) -> Tuple[str, float]:
    """Format nova verification results for reasoning context.
    
    Args:
        result: Raw result from nova_verify tool
        
    Returns:
        Tuple of (formatted_string, confidence_impact)
        - formatted_string: Grounding verdict with evidence
        - confidence_impact: Delta adjustment to confidence
    """
    try:
        if hasattr(result, "status") and hasattr(result, "reasoning"):
            status = getattr(result, "status", "ERROR")
            status = status.value if hasattr(status, "value") else str(status)
            if status == "TRUE":
                grounding = "grounded"
            elif status == "FALSE":
                grounding = "contradicted"
            elif status == "ERROR":
                grounding = "error"
            else:
                grounding = "ungrounded"
            evidence = getattr(result, "evidence", [])
            grounding_score = float(getattr(result, "nova_confidence", 0.0))
            if grounding == "error" and not evidence:
                reasoning = str(getattr(result, "reasoning", "")).strip()
                if reasoning:
                    evidence = [reasoning]
        elif isinstance(result, dict):
            grounding = result.get("grounding", "ungrounded")
            evidence = result.get("evidence", [])
            grounding_score = result.get("grounding_score", 0.5)
        else:
            # Try to parse as string
            result_str = str(result).lower()
            if "grounded" in result_str:
                grounding = "grounded"
                grounding_score = 0.8
            elif "contradicted" in result_str:
                grounding = "contradicted"
                grounding_score = 0.1
            else:
                grounding = "ungrounded"
                grounding_score = 0.3
            evidence = [result_str]
        
        # Calculate confidence impact based on grounding verdict
        if grounding == "grounded":
            impact = 0.20  # Strong support
        elif grounding == "contradicted":
            impact = -0.25  # Direct contradiction
        elif grounding == "partially_grounded":
            impact = 0.10  # Partial support
        elif grounding == "error":
            impact = -0.10  # Verification unavailable (auth/network/etc)
        else:  # ungrounded
            impact = -0.05  # No supporting evidence found
        
        # Format output
        formatted_lines = [f"Nova Verification: {grounding.upper()}"]
        if evidence:
            formatted_lines.append("Evidence:")
            for ev in evidence[:3]:  # Limit to 3 pieces of evidence
                if isinstance(ev, dict):
                    formatted_lines.append(f"  - {ev.get('text', str(ev))[:200]}")
                else:
                    formatted_lines.append(f"  - {str(ev)[:200]}")
        
        formatted_lines.append(f"Confidence score: {grounding_score:.2f}")
        
        return "\n".join(formatted_lines), impact
        
    except Exception as e:
        log.warning(f"Error formatting nova results: {e}")
        return str(result), 0.0


def _extract_nova_error(result: any) -> str:
    """Extract tool-level error message from Nova verify response objects."""
    try:
        if hasattr(result, "status"):
            status = getattr(result, "status", "")
            status = status.value if hasattr(status, "value") else str(status)
            if status.upper() == "ERROR":
                error_kind = str(getattr(result, "error_kind", "")).strip()
                reasoning = str(getattr(result, "reasoning", "")).strip()
                if error_kind and reasoning:
                    return f"Nova verify {error_kind}: {reasoning}"
                if error_kind:
                    return f"Nova verify {error_kind}"
                if reasoning:
                    return reasoning
                return "Nova verify returned ERROR"

        if isinstance(result, dict):
            status = str(result.get("status", "")).upper()
            if status == "ERROR":
                error_kind = str(result.get("error_kind", "")).strip()
                reason = str(result.get("reason", result.get("reasoning", ""))).strip()
                if error_kind and reason:
                    return f"Nova verify {error_kind}: {reason}"
                if error_kind:
                    return f"Nova verify {error_kind}"
                if reason:
                    return reason
                return "Nova verify returned ERROR"
    except Exception:
        pass

    return ""
