"""Web search tool wrapper for deep_think tool invoker.

Wraps the grounded web search provider and formats results for reasoning context.
"""

import asyncio
import logging
import time
from typing import Tuple

log = logging.getLogger(__name__)


def invoke_web_search(query: str, timeout: int = 10) -> Tuple[str, float, str]:
    """Invoke the grounded web search provider with timeout.
    
    Args:
        query: Search query string
        timeout: Timeout in seconds
        
    Returns:
        Tuple of (formatted_results: str, confidence_impact: float, error_message: str)
        
    Raises:
        TimeoutError: If tool call exceeds timeout
        Exception: On tool invocation errors
    """
    log.debug(f"Invoking web_search with query: {query[:100]}...")
    start_time = time.time()
    
    try:
        from nova_factcheck.research_tools import web_search as research_web_search

        async def _run() -> object:
            return await asyncio.wait_for(
                research_web_search(query, job_id="", task_class=""),
                timeout=timeout,
            )

        result = asyncio.run(_run())
        
        elapsed_ms = int((time.time() - start_time) * 1000)
        
        # Format results (top 3-5 results with title, snippet, url)
        formatted = _format_web_search_results(result)
        confidence_impact = 0.15  # Successful search increases confidence
        
        log.info(f"web_search succeeded in {elapsed_ms}ms")
        return formatted, confidence_impact, ""
        
    except TimeoutError:
        elapsed_ms = int((time.time() - start_time) * 1000)
        log.warning(f"web_search timed out after {elapsed_ms}ms")
        return "", -0.10, "Tool call timed out"
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        error_msg = str(e)
        log.error(f"web_search failed: {error_msg}")
        return "", -0.05, error_msg


def _format_web_search_results(result: any) -> str:
    """Format web search results for reasoning context.
    
    Args:
        result: Raw result from web_search tool
        
    Returns:
        Formatted string with top results (title, snippet, url)
    """
    try:
        if hasattr(result, "results") and hasattr(result, "query"):
            items = getattr(result, "results", [])
        elif isinstance(result, dict):
            # Handle dict response
            if "results" in result:
                items = result["results"]
            elif "items" in result:
                items = result["items"]
            else:
                return "Web search returned unexpected format"
        elif isinstance(result, list):
            items = result
        else:
            return str(result)
        
        if not items:
            return "No results found"
        
        # Take top 3-5 results
        top_results = items[:5]
        formatted_lines = ["Web search results:"]
        
        for i, item in enumerate(top_results, 1):
            if hasattr(item, "title") and hasattr(item, "url"):
                title = getattr(item, "title", "No title")
                snippet = getattr(item, "snippet", "")[:200]
                url = getattr(item, "url", "")
                formatted_lines.append(f"{i}. {title}")
                if snippet:
                    formatted_lines.append(f"   {snippet}...")
                if url:
                    formatted_lines.append(f"   URL: {url}")
            elif isinstance(item, dict):
                title = item.get("title", "No title")
                snippet = item.get("snippet", "")[:200]  # Limit snippet length
                url = item.get("url", "")
                formatted_lines.append(f"{i}. {title}")
                if snippet:
                    formatted_lines.append(f"   {snippet}...")
                if url:
                    formatted_lines.append(f"   URL: {url}")
            else:
                formatted_lines.append(f"{i}. {str(item)[:200]}")
        
        return "\n".join(formatted_lines)
        
    except Exception as e:
        log.warning(f"Error formatting web search results: {e}")
        return str(result)
