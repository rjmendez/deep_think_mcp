"""Private adversarial lane core (challenger-only, non-authoritative)."""

from __future__ import annotations

import re
from typing import Any

_FLAG_TYPES = {"contradiction", "evidence_gap", "exploit_path", "unverifiable_claim"}
_SEVERITY = {"low", "medium", "high", "critical"}

_EVIDENCE_MARKERS = ("[", "source", "citation", "evidence", "according to", "ref:")
_EXPLOIT_PATTERNS = (
    r"\bbypass\b",
    r"\boverride\b",
    r"\bdisable\b.{0,30}\bauth",
    r"\bignore\b.{0,50}\binstruction",
    r"\bexfiltrat",
)
_UNVERIFIABLE_PATTERNS = (
    r"\balways\b",
    r"\bnever\b",
    r"\bguaranteed?\b",
    r"\bproven\b",
    r"\bundeniable\b",
)


def _ref(source: str, snippet: str) -> dict[str, str]:
    return {"source": source, "snippet": snippet[:180]}


def _make_flag(
    flag: str,
    severity: str,
    confidence: float,
    evidence_refs: list[dict[str, str]],
) -> dict[str, Any]:
    if flag not in _FLAG_TYPES:
        raise ValueError(f"Unsupported challenge flag: {flag}")
    if severity not in _SEVERITY:
        raise ValueError(f"Unsupported severity: {severity}")
    return {
        "flag": flag,
        "severity": severity,
        "confidence": max(0.0, min(1.0, float(confidence))),
        "evidence_refs": evidence_refs,
    }


def _has_evidence_markers(text: str) -> bool:
    t = text.lower()
    return any(marker in t for marker in _EVIDENCE_MARKERS)


def _find_matches(text: str, patterns: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            out.append(text[max(0, match.start() - 30): min(len(text), match.end() + 30)].strip())
    return out


def build_private_challenge_flags(
    *,
    target_question: str = "",
    target_plan: str = "",
    target_output: str = "",
    context: str = "",
) -> dict[str, Any]:
    """Return challenge flags only; never final verdicts."""
    question = target_question or ""
    plan = target_plan or ""
    output = target_output or ""
    extra_context = context or ""

    flags: list[dict[str, Any]] = []

    if output and ("must" in output.lower()) and ("cannot" in output.lower() or "never" in output.lower()):
        flags.append(
            _make_flag(
                "contradiction",
                "medium",
                0.66,
                [_ref("target_output", "Detected both obligation and prohibition language in output.")],
            )
        )

    combined = "\n".join(part for part in (question, plan, output) if part).strip()
    if combined and not _has_evidence_markers(combined):
        flags.append(
            _make_flag(
                "evidence_gap",
                "medium",
                0.72,
                [_ref("target_bundle", "Assertions lack explicit citations or evidence markers.")],
            )
        )

    exploit_hits = _find_matches(output + "\n" + plan + "\n" + extra_context, _EXPLOIT_PATTERNS)
    if exploit_hits:
        refs = [_ref("target_context", hit) for hit in exploit_hits[:3]]
        flags.append(_make_flag("exploit_path", "high", min(0.95, 0.6 + 0.12 * len(refs)), refs))

    unverifiable_hits = _find_matches(output, _UNVERIFIABLE_PATTERNS)
    if unverifiable_hits and not _has_evidence_markers(output):
        refs = [_ref("target_output", hit) for hit in unverifiable_hits[:3]]
        flags.append(_make_flag("unverifiable_claim", "low", min(0.9, 0.55 + 0.1 * len(refs)), refs))

    return {
        "lane": "private_adversarial_challenger",
        "role": "challenger",
        "non_authoritative": True,
        "challenge_flags": flags,
    }
