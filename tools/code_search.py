"""Code search tool wrapper for deep_think tool invoker.

Searches the local repository tree for code-backed evidence and formats the
matches for reasoning context.
"""

import logging
import os
import re
import time
from pathlib import Path
from typing import Tuple

log = logging.getLogger(__name__)


def invoke_code_search(query: str, timeout: int = 10) -> Tuple[str, float, str]:
    """Search the local repo for code evidence and format matching lines."""
    log.debug(f"Invoking code_search with query: {query[:100]}...")
    start_time = time.time()

    try:
        result = _search_local_repo(query, timeout)
        elapsed_ms = int((time.time() - start_time) * 1000)
        formatted = _format_code_search_results(result)

        if not result.get("results"):
            log.info("code_search found no local matches in %dms", elapsed_ms)
            return formatted, 0.0, ""

        log.info("code_search succeeded in %dms", elapsed_ms)
        return formatted, 0.15, ""
    except TimeoutError:
        elapsed_ms = int((time.time() - start_time) * 1000)
        log.warning("code_search timed out after %dms", elapsed_ms)
        return "", -0.10, "Tool call timed out"
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        error_msg = str(e)
        log.error("code_search failed after %dms: %s", elapsed_ms, error_msg)
        return "", -0.05, error_msg


def _search_local_repo(query: str, timeout: int) -> dict:
    root = Path(os.getenv("DEEP_THINK_CODE_SEARCH_ROOT", Path(__file__).resolve().parents[1]))
    terms = _candidate_terms(query)
    seen: set[str] = set()
    results = []
    started = time.time()

    files = []
    for pattern in ("*.py", "*.md", "*.txt"):
        files.extend(root.rglob(pattern))

    for path in files:
        if time.time() - started > max(1, timeout):
            break
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        for lineno, line in enumerate(text.splitlines(), 1):
            if time.time() - started > max(1, timeout):
                break
            lower_line = line.lower()
            if not any(term in lower_line for term in terms[:8]):
                continue
            key = f"{path}:{lineno}:{line}"
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "path": str(path.relative_to(root)),
                    "line": str(lineno),
                    "text": line.strip(),
                    "repository": root.name,
                }
            )
            if len(results) >= 8:
                break
        if len(results) >= 8:
            break

    return {"results": results}


def _candidate_terms(query: str) -> list[str]:
    stopwords = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "into",
        "your",
        "are",
        "was",
        "were",
        "but",
        "not",
        "all",
        "any",
        "why",
        "how",
        "what",
        "real",
        "current",
        "review",
        "please",
        "code",
        "implementation",
        "handling",
        "current",
    }
    raw_terms = re.findall(r"[A-Za-z0-9_./:-]+", query.lower())
    terms = []
    for term in raw_terms:
        cleaned = term.strip(".,:;()[]{}<>\"'")
        if len(cleaned) < 3 or cleaned in stopwords:
            continue
        terms.append(cleaned)
        if "-" in cleaned:
            terms.append(cleaned.replace("-", "_"))
    return list(dict.fromkeys(terms))


def _format_code_search_results(result: any) -> str:
    try:
        items = result.get("results", [])
        if not items:
            return "No code matches found"

        formatted_lines = ["Code search results:"]
        for i, item in enumerate(items[:5], 1):
            path = item.get("path", "unknown")
            line = item.get("line", "")
            text = item.get("text", "")[:200]
            formatted_lines.append(f"{i}. {path}:{line}")
            if text:
                formatted_lines.append(f"   {text}")
        return "\n".join(formatted_lines)
    except Exception as e:
        log.warning("Error formatting code search results: %s", e)
        return str(result)
