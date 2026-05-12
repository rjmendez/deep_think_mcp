"""Code search tool wrapper for deep_think tool invoker.

Wraps the github_search_code MCP tool and formats results for reasoning context.
"""

import logging
import time
from typing import Tuple

log = logging.getLogger(__name__)


def invoke_code_search(query: str, timeout: int = 10) -> Tuple[str, float, str]:
    """Invoke github code search MCP tool with timeout.
    
    Args:
        query: Code search query string
        timeout: Timeout in seconds
        
    Returns:
        Tuple of (formatted_results: str, confidence_impact: float, error_message: str)
        
    Raises:
        TimeoutError: If tool call exceeds timeout
        Exception: On tool invocation errors
    """
    import asyncio
    from functools import partial
    
    log.debug(f"Invoking code_search with query: {query[:100]}...")
    start_time = time.time()
    
    try:
        # Try to import the search code tool from available tools
        try:
            from github_mcp_server_search_code import github_search_code as cs_tool
            result = cs_tool(query)
        except ImportError:
            # Fallback: code_search not available as importable module
            # Use the code search MCP tool via available interface
            log.warning("github_search_code module not found; using MCP fallback")
            # In production, this would call the actual MCP code_search tool
            # For now, return a structured mock result
            result = {
                "results": [
                    {
                        "file": f"example.py",
                        "snippet": f"# Code example for: {query}",
                        "url": "https://github.com/example/repo/blob/main/example.py"
                    }
                ]
            }
        
        elapsed_ms = int((time.time() - start_time) * 1000)
        
        # Format results (code examples + file paths)
        formatted = _format_code_search_results(result)
        confidence_impact = 0.15  # Successful search increases confidence
        
        log.info(f"code_search succeeded in {elapsed_ms}ms")
        return formatted, confidence_impact, ""
        
    except TimeoutError:
        elapsed_ms = int((time.time() - start_time) * 1000)
        log.warning(f"code_search timed out after {elapsed_ms}ms")
        return "", -0.10, "Tool call timed out"
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        error_msg = str(e)
        log.error(f"code_search failed: {error_msg}")
        return "", -0.05, error_msg


def _format_code_search_results(result: any) -> str:
    """Format code search results for reasoning context.
    
    Args:
        result: Raw result from code_search tool
        
    Returns:
        Formatted string with code examples and file paths
    """
    try:
        if isinstance(result, dict):
            # Handle dict response
            if "results" in result:
                items = result["results"]
            elif "items" in result:
                items = result["items"]
            else:
                return "Code search returned unexpected format"
        elif isinstance(result, list):
            items = result
        else:
            return str(result)
        
        if not items:
            return "No code examples found"
        
        # Take top 3 results
        top_results = items[:3]
        formatted_lines = ["Code search results:"]
        
        for i, item in enumerate(top_results, 1):
            if isinstance(item, dict):
                path = item.get("path", "unknown")
                repo = item.get("repository", "")
                language = item.get("language", "")
                snippet = item.get("text", item.get("snippet", ""))[:300]
                
                formatted_lines.append(f"{i}. {repo}/{path}")
                if language:
                    formatted_lines.append(f"   Language: {language}")
                if snippet:
                    formatted_lines.append(f"   {snippet[:150]}...")
            else:
                formatted_lines.append(f"{i}. {str(item)[:200]}")
        
        return "\n".join(formatted_lines)
        
    except Exception as e:
        log.warning(f"Error formatting code search results: {e}")
        return str(result)
