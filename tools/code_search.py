"""Code search tool wrapper for deep_think tool invoker.

Searches the local repository tree for code-backed evidence and formats the
matches for reasoning context.
"""

import logging
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Tuple

log = logging.getLogger(__name__)

_MAX_CANDIDATE_RESULTS = 48
_MAX_RESULTS_PER_FILE = 2
_MAX_MATCHED_TERMS = 24
_GENERIC_PROMPT_TERMS = {
    "perform",
    "deep",
    "whole",
    "whole_repository",
    "whole-repository",
    "repository",
    "correctness",
    "security",
    "reliability",
    "defects",
    "cover",
    "entire",
    "codebase",
    "perspective",
}


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
    ranked_terms = _ranked_candidate_terms(query)
    seen: set[str] = set()
    candidates = []
    started = time.time()

    files = []
    for pattern in ("*.py", "*.ts", "*.tsx", "*.js", "*.go", "*.rs", "*.java", "*.md", "*.txt"):
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
            score, matched_terms = _score_line(lower_line, ranked_terms)
            if score <= 0:
                continue
            key = f"{path}:{lineno}:{line}"
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "path": str(path.relative_to(root)),
                    "line": str(lineno),
                    "text": line.strip(),
                    "repository": root.name,
                    "score": round(score, 3),
                    "matched_terms": matched_terms[:6],
                }
            )
            if len(candidates) >= _MAX_CANDIDATE_RESULTS:
                break
        if len(candidates) >= _MAX_CANDIDATE_RESULTS:
            break

    results = _select_diverse_results(candidates, limit=8)
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


def _ranked_candidate_terms(query: str) -> list[tuple[str, float]]:
    terms = _candidate_terms(query)
    if not terms:
        return []

    weighted: list[tuple[str, float, int]] = []
    split_index = max(len(terms) // 2, 1)
    for idx, term in enumerate(terms):
        weight = 1.0
        if idx >= split_index:
            weight += 0.9
        if term in _GENERIC_PROMPT_TERMS:
            weight -= 0.6
        if any(ch in term for ch in ("/", ".", "_", ":")):
            weight += 0.5
        if len(term) >= 10:
            weight += 0.2
        weighted.append((term, max(weight, 0.1), idx))

    weighted.sort(key=lambda item: (-item[1], item[2]))
    return [(term, score) for term, score, _ in weighted]


def _score_line(line_lower: str, ranked_terms: list[tuple[str, float]]) -> tuple[float, list[str]]:
    matched_terms: list[str] = []
    score = 0.0

    for term, weight in ranked_terms[:_MAX_MATCHED_TERMS]:
        if term in line_lower:
            matched_terms.append(term)
            score += weight

    if len(matched_terms) >= 2:
        score += 0.2
    return score, matched_terms


def _select_diverse_results(candidates: list[dict], limit: int = 8) -> list[dict]:
    if not candidates:
        return []

    ranked = sorted(
        candidates,
        key=lambda item: (
            -float(item.get("score", 0.0)),
            str(item.get("path", "")),
            int(item.get("line", 0)),
        ),
    )

    selected: list[dict] = []
    per_file_count: dict[str, int] = defaultdict(int)

    for item in ranked:
        path = str(item.get("path", ""))
        if per_file_count[path] >= _MAX_RESULTS_PER_FILE:
            continue
        selected.append(item)
        per_file_count[path] += 1
        if len(selected) >= limit:
            return selected

    for item in ranked:
        if item in selected:
            continue
        selected.append(item)
        if len(selected) >= limit:
            break

    return selected


def _format_code_search_results(result: any) -> str:
    try:
        items = result.get("results", [])
        if not items:
            return "No code matches found"

        formatted_lines = ["Code search results:"]
        for i, item in enumerate(items[:8], 1):
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
