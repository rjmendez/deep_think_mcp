"""Document fetch tool wrapper for deep_think tool invoker.

Fetches and summarizes documents from web or file paths.
"""

import logging
import os
import time
from pathlib import Path
from typing import Tuple
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def _allowed_local_roots() -> list[Path]:
    raw = os.getenv("DEEP_THINK_DOCUMENT_FETCH_ROOTS", "").strip()
    if raw:
        return [Path(p).expanduser().resolve() for p in raw.split(":") if p.strip()]
    # Safe default: repository root (deep_think_mcp)
    return [Path(__file__).resolve().parents[1]]


def _is_allowed_local_path(path: Path, roots: list[Path]) -> bool:
    path_resolved = path.expanduser().resolve()
    for root in roots:
        try:
            path_resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _allowed_domains() -> list[str]:
    raw = os.getenv("DEEP_THINK_DOCUMENT_FETCH_DOMAIN_WHITELIST", "").strip()
    if not raw:
        return []
    return [_strip_www(d.strip().lower()) for d in raw.split(",") if d.strip()]


def _strip_www(domain: str) -> str:
    return domain[4:] if domain.lower().startswith("www.") else domain


def _domain_allowed(url: str, allowed: list[str]) -> bool:
    if not allowed:
        return True
    domain = _strip_www(urlparse(url).netloc.lower())
    return any(domain == d or domain.endswith("." + d) for d in allowed)


def invoke_document_fetch(url: str, timeout: int = 5) -> Tuple[str, float, str]:
    """Fetch and summarize a document from URL or file path.
    
    Args:
        url: URL or file path to fetch
        timeout: Timeout in seconds
        
    Returns:
        Tuple of (formatted_results: str, confidence_impact: float, error_message: str)
        
    Raises:
        TimeoutError: If tool call exceeds timeout
        Exception: On tool invocation errors
    """
    import asyncio
    from functools import partial
    
    log.debug(f"Invoking document_fetch with URL: {url[:100]}...")
    start_time = time.time()
    
    try:
        # Try to fetch document
        if url.startswith("http://") or url.startswith("https://"):
            allowed_domains = _allowed_domains()
            if not _domain_allowed(url, allowed_domains):
                return "", -0.05, "Domain blocked by document fetch policy"
            result = _fetch_web_document(url, timeout)
        else:
            roots = _allowed_local_roots()
            local_path = Path(url)
            if not _is_allowed_local_path(local_path, roots):
                return "", -0.05, "Local path blocked by document fetch policy"
            result = _fetch_local_document(url, timeout)
        
        elapsed_ms = int((time.time() - start_time) * 1000)
        
        # Format and summarize
        formatted = _format_document_summary(result, url)
        confidence_impact = 0.12  # Moderate confidence boost
        
        log.info(f"document_fetch succeeded in {elapsed_ms}ms")
        return formatted, confidence_impact, ""
        
    except TimeoutError:
        elapsed_ms = int((time.time() - start_time) * 1000)
        log.warning(f"document_fetch timed out after {elapsed_ms}ms")
        return "", -0.10, "Document fetch timed out"
    except FileNotFoundError:
        log.error(f"Document not found: {url}")
        return "", -0.05, "Document not found"
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        error_msg = str(e)
        log.error(f"document_fetch failed: {error_msg}")
        return "", -0.05, error_msg


def _fetch_web_document(url: str, timeout: int) -> str:
    """Fetch document from web URL.
    
    Args:
        url: Web URL to fetch
        timeout: Timeout in seconds
        
    Returns:
        Document content as string
    """
    try:
        import requests
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        return response.text
    except ImportError:
        # Fallback: use urllib
        import urllib.request
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read().decode('utf-8')
        except Exception as e:
            raise Exception(f"Failed to fetch {url}: {e}")


def _fetch_local_document(path: str, timeout: int) -> str:
    """Fetch document from local file path.
    
    Args:
        path: Local file path
        timeout: Timeout in seconds (unused for local files)
        
    Returns:
        Document content as string
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {path}")
    except Exception as e:
        raise Exception(f"Failed to read {path}: {e}")


def _format_document_summary(content: str, source_url: str) -> str:
    """Format document content as a summary.
    
    Extracts first 500 characters for summary plus citation.
    
    Args:
        content: Full document content
        source_url: Source URL or path for citation
        
    Returns:
        Formatted summary string
    """
    try:
        # Limit to first 500 characters for summary
        max_summary_len = 500
        if len(content) > max_summary_len:
            summary = content[:max_summary_len].rstrip() + "..."
        else:
            summary = content
        
        # Clean up whitespace
        summary = "\n".join(line.strip() for line in summary.split("\n") if line.strip())
        
        # Add citation
        formatted_lines = [
            "Document Summary:",
            summary,
            f"\nSource: {source_url}"
        ]
        
        return "\n".join(formatted_lines)
        
    except Exception as e:
        log.warning(f"Error formatting document summary: {e}")
        return str(content)[:500]
