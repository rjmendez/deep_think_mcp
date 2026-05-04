"""ClaimExtractor — regex/NLP-based extraction of factual assertions from deep-think output.

Identifies declarative statements that make verifiable factual claims:
  - Subject–verb–object assertions ("X is Y", "X has Y", "X does Y")
  - Numerical claims ("X is N%", "X measures N")
  - Categorical claims ("X belongs to", "X is a type of")
  - Causal claims ("X causes Y", "X leads to Y")

Filters out questions, imperatives, meta-commentary, and hedge phrases.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ExtractedClaim:
    """A factual assertion extracted from reasoning text."""

    claim_id: str
    text: str                    # verbatim claim sentence
    claim_type: str              # "assertion" | "numerical" | "causal" | "categorical"
    confidence_in_text: float    # inline model confidence (0-1), or 0.5 if absent
    source_pass: Optional[int] = None  # which reasoning pass the claim came from


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Assertion verbs that indicate a factual claim
_ASSERTION_VERBS = (
    r"\bis\b", r"\bare\b", r"\bwas\b", r"\bwere\b",
    r"\bhas\b", r"\bhave\b", r"\bhad\b",
    r"\bshows?\b", r"\bindicates?\b", r"\bdemonstrates?\b",
    r"\bproves?\b", r"\bconfirms?\b", r"\bestablishes?\b",
    r"\bcontains?\b", r"\bincludes?\b",
    r"\bmeans?\b", r"\brepresents?\b",
)
_ASSERTION_VERB_RE = re.compile(
    r"(?:" + "|".join(_ASSERTION_VERBS) + r")",
    re.IGNORECASE,
)

# Causal connectives
_CAUSAL_RE = re.compile(
    r"\b(?:causes?|leads? to|results? in|produces?|triggers?|enables?|prevents?)\b",
    re.IGNORECASE,
)

# Numerical claim indicators
_NUMERICAL_RE = re.compile(
    r"\b\d+(?:\.\d+)?(?:\s*%|ms|s\b|x\b|kb|mb|gb)?",
    re.IGNORECASE,
)

# Inline confidence patterns (e.g. "[confidence: 90%]", "(95% confidence)")
_INLINE_CONF_PATTERNS = [
    re.compile(r"\[?\s*confidence[:\s]+(\d+)\s*%?\s*\]?", re.IGNORECASE),
    re.compile(r"\((\d+)\s*%\s+confidence\)", re.IGNORECASE),
    re.compile(r"with\s+(\d+)\s*%\s+confidence", re.IGNORECASE),
]

# Phrases that indicate hedging / uncertainty (not verifiable facts)
_HEDGE_PHRASES = re.compile(
    r"\b(?:perhaps|maybe|might|could|may|possibly|presumably|seem|appear|suggest|"
    r"hypothesize|speculate|wonder|unclear|unknown|not sure|uncertain)\b",
    re.IGNORECASE,
)

# Meta-commentary that should not be extracted as claims
_META_RE = re.compile(
    r"\b(?:in conclusion|to summarize|as mentioned|as stated|note that|"
    r"for example|e\.g\.|i\.e\.|see also|refer to|let me|I will|I think|"
    r"we can see|we should|please|thank you|above all)\b",
    re.IGNORECASE,
)

# Strip leading bullets, numbers, markdown
_CLEANUP_RE = re.compile(r"^[\s\-\*\•\d\.>\#]+")


# ---------------------------------------------------------------------------
# ClaimExtractor
# ---------------------------------------------------------------------------

class ClaimExtractor:
    """Extract verifiable claims from reasoning text using regex patterns.

    Args:
        min_words: Minimum word count for a candidate sentence (default 5).
        max_claims: Maximum claims to return per call (default 30).
    """

    def __init__(self, min_words: int = 5, max_claims: int = 30) -> None:
        self.min_words = min_words
        self.max_claims = max_claims

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(self, text: str, source_pass: Optional[int] = None) -> List[ExtractedClaim]:
        """Extract claims from *text*.

        Args:
            text: Raw reasoning output (may span multiple paragraphs).
            source_pass: Which reasoning pass produced this text.

        Returns:
            Deduplicated list of ExtractedClaim objects.
        """
        if not text or not text.strip():
            return []

        sentences = self._split_sentences(text)
        claims: List[ExtractedClaim] = []
        seen: set[str] = set()

        for sentence in sentences:
            clean = self._clean(sentence)
            if not clean:
                continue

            # Skip already-seen text (dedup)
            norm = clean.lower().strip()
            if norm in seen:
                continue

            if not self._is_candidate(clean):
                continue

            conf, clean = self._extract_inline_confidence(clean)
            ctype = self._classify_type(clean)

            seen.add(norm)
            claims.append(
                ExtractedClaim(
                    claim_id=str(uuid.uuid4())[:8],
                    text=clean,
                    claim_type=ctype,
                    confidence_in_text=conf,
                    source_pass=source_pass,
                )
            )

            if len(claims) >= self.max_claims:
                break

        return claims

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _split_sentences(self, text: str) -> List[str]:
        """Split text into candidate sentences."""
        # Normalise line endings
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # Split on sentence-ending punctuation OR newlines
        raw = re.split(r"(?<=[.!?])\s+|\n+", text)
        return [s.strip() for s in raw if s.strip()]

    def _clean(self, sentence: str) -> str:
        """Strip markdown, bullets, and leading noise."""
        sentence = _CLEANUP_RE.sub("", sentence).strip()
        # Remove trailing citation refs like [1], [2,3]
        sentence = re.sub(r"\[\d+(?:,\s*\d+)*\]", "", sentence).strip()
        return sentence

    def _is_candidate(self, sentence: str) -> bool:
        """Return True if *sentence* looks like a verifiable assertion."""
        words = sentence.split()
        if len(words) < self.min_words:
            return False

        # Skip questions
        if sentence.endswith("?"):
            return False

        # Skip obvious commands / imperatives starting with certain verbs
        first = words[0].lower().rstrip(".,;:")
        if first in {"let", "note", "see", "check", "consider", "assume",
                     "suppose", "imagine", "please", "use", "make", "ensure"}:
            return False

        # Skip meta-commentary
        if _META_RE.search(sentence):
            return False

        # Skip pure hedge sentences (no solid assertion)
        if _HEDGE_PHRASES.search(sentence) and not _ASSERTION_VERB_RE.search(sentence):
            return False

        # Must contain at least one assertion verb OR causal connective OR number
        has_assertion = bool(
            _ASSERTION_VERB_RE.search(sentence)
            or _CAUSAL_RE.search(sentence)
            or _NUMERICAL_RE.search(sentence)
        )
        return has_assertion

    def _extract_inline_confidence(self, sentence: str) -> tuple[float, str]:
        """Return (confidence, cleaned_sentence) after stripping inline conf markers."""
        for pattern in _INLINE_CONF_PATTERNS:
            m = pattern.search(sentence)
            if m:
                try:
                    val = float(m.group(1))
                    conf = val / 100.0 if val > 1.0 else val
                    conf = max(0.0, min(1.0, conf))
                    cleaned = pattern.sub("", sentence).strip()
                    return conf, cleaned
                except (ValueError, IndexError):
                    pass
        return 0.5, sentence

    def _classify_type(self, sentence: str) -> str:
        """Classify claim into a broad type."""
        if _CAUSAL_RE.search(sentence):
            return "causal"
        if _NUMERICAL_RE.search(sentence):
            return "numerical"
        cat_re = re.compile(
            r"\b(?:is a|are a|is an|are an|belongs? to|is of type|is classified as)\b",
            re.IGNORECASE,
        )
        if cat_re.search(sentence):
            return "categorical"
        return "assertion"
