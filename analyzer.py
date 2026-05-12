"""
Phase 1 Part 2: Perspective Output Analyzer
Analyzes diverse reasoning outputs, extracts claims, detects contradictions,
and computes quality scores for adaptive multi-agent reasoning.
"""

import json
import re
import statistics
from typing import Dict, List, Optional, Tuple
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
import xml.etree.ElementTree as ET

from .models_adaptive import (
    Claim,
    Uncertainty,
    Contradiction,
    PerspectiveAnalysis,
    ContradictionType,
    ClaimCategory,
    ClaimSource,
    DEFAULT_ADAPTIVE_CONFIG,
)


# ============================================================================
# 1. FORMAT DETECTION
# ============================================================================

def detect_format(text: str) -> str:
    """
    Detect the format of reasoning output text.
    
    Returns: 'json' | 'markdown' | 'xml' | 'free_text'
    """
    if not text or not text.strip():
        return "free_text"
    
    text_stripped = text.strip()
    
    # Try JSON
    if text_stripped.startswith('{') or text_stripped.startswith('['):
        try:
            json.loads(text_stripped)
            return "json"
        except json.JSONDecodeError:
            pass
    
    # Try XML
    if text_stripped.startswith('<'):
        try:
            ET.fromstring(text_stripped)
            return "xml"
        except ET.ParseError:
            pass
    
    # Try Markdown (check for markdown headers, lists, code blocks)
    if re.search(r'(^#{1,6}\s|^[-*]\s|```)', text_stripped, re.MULTILINE):
        return "markdown"
    
    # Default to free text
    return "free_text"


# ============================================================================
# 2. FORMAT-SPECIFIC PARSERS
# ============================================================================

def parse_json_format(text: str) -> Dict:
    """Parse JSON-formatted reasoning output."""
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            # If it's a list, wrap it
            data = {"items": data}
        return data
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse failed: {str(e)}", "raw_text": text}


def parse_markdown_format(text: str) -> Dict:
    """
    Parse Markdown-formatted reasoning output.
    Extracts sections, lists, and code blocks.
    """
    result = {
        "sections": [],
        "claims": [],
        "metadata": {},
    }
    
    lines = text.split('\n')
    current_section = None
    current_content = []
    
    for line in lines:
        # Detect headers
        header_match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if header_match:
            if current_section and current_content:
                result["sections"].append({
                    "title": current_section,
                    "content": '\n'.join(current_content).strip()
                })
            current_section = header_match.group(2)
            current_content = []
            continue
        
        # Detect list items as potential claims
        list_match = re.match(r'^[-*+]\s+(.+)$', line)
        if list_match:
            claim_text = list_match.group(1)
            result["claims"].append({"text": claim_text, "format": "list_item"})
            current_content.append(line)
            continue
        
        if current_section is not None:
            current_content.append(line)
    
    # Append final section
    if current_section and current_content:
        result["sections"].append({
            "title": current_section,
            "content": '\n'.join(current_content).strip()
        })
    
    return result


def parse_xml_format(text: str) -> Dict:
    """Parse XML-formatted reasoning output."""
    try:
        root = ET.fromstring(text)
        result = {
            "tag": root.tag,
            "attributes": root.attrib,
            "children": [],
            "text": root.text,
        }
        
        for child in root:
            result["children"].append({
                "tag": child.tag,
                "attributes": child.attrib,
                "text": child.text,
            })
        
        return result
    except ET.ParseError as e:
        return {"error": f"XML parse failed: {str(e)}", "raw_text": text}


def parse_free_text_format(text: str) -> Dict:
    """
    Parse free-text reasoning output.
    Extracts sentences, claims, and keywords.
    """
    result = {
        "full_text": text,
        "sentences": [],
        "paragraphs": [],
        "keywords": [],
    }
    
    # Split into paragraphs
    paragraphs = text.split('\n\n')
    result["paragraphs"] = [p.strip() for p in paragraphs if p.strip()]
    
    # Split into sentences
    sentences = re.split(r'[.!?]+', text)
    result["sentences"] = [s.strip() for s in sentences if s.strip()]
    
    # Extract keywords (capitalized words or multi-word phrases)
    # Simple heuristic: look for capitalized starts or quoted phrases
    quoted = re.findall(r'"([^"]+)"', text)
    result["keywords"] = quoted
    
    return result


# ============================================================================
# 3. CLAIM EXTRACTION
# ============================================================================

def extract_claims(parsed: Dict, reasoning_text: str) -> List[Claim]:
    """
    Extract claims from parsed reasoning output.
    Integrates format-specific extraction with text-based analysis.
    """
    claims = []
    
    # Format-specific extraction
    if "claims" in parsed and isinstance(parsed["claims"], list):
        for item in parsed["claims"]:
            if isinstance(item, dict) and "text" in item:
                claim_text = item["text"]
            elif isinstance(item, str):
                claim_text = item
            else:
                continue
            
            confidence = infer_confidence_from_text(claim_text)
            claim = Claim(
                text=claim_text,
                confidence=confidence,
                importance=0.7,  # Default importance
                category=ClaimCategory.FACTUAL.value,
                source=ClaimSource.EXTRACTED.value,
                justification_tokens=[]
            )
            claims.append(claim)
    
    # Extract from sections if available
    if "sections" in parsed:
        for section in parsed["sections"]:
            if isinstance(section, dict):
                content = section.get("content", "")
                if content:
                    section_claims = extract_claims_via_summarization(content)
                    claims.extend(section_claims)
    
    # Extract from full text as fallback or supplement
    if len(claims) < 5:
        text_claims = extract_claims_via_summarization(reasoning_text)
        claims.extend(text_claims)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_claims = []
    for claim in claims:
        claim_key = claim.text.lower().strip()
        if claim_key not in seen:
            seen.add(claim_key)
            unique_claims.append(claim)
    
    return unique_claims[:20]  # Cap at 20 claims


def extract_claims_via_summarization(text: str) -> List[Claim]:
    """
    Extract claims from raw text via heuristic summarization.
    Identifies sentences that make assertions (contain verbs, have subjects).
    """
    claims = []
    
    # Split into sentences
    sentences = re.split(r'[.!?]+', text)
    
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence or len(sentence) < 10:
            continue
        
        # Heuristic: skip questions (end with ?) and imperatives
        if sentence.endswith('?') or re.match(r'^(do|does|did|can|could|should|would|is|are)', sentence, re.IGNORECASE):
            continue
        
        # Look for common assertion patterns
        if re.search(r'\b(is|are|was|were|be|been|being|has|have|had|do|does|did|will|would|should|can|could|may|might)\b', sentence, re.IGNORECASE):
            confidence = infer_confidence_from_text(sentence)
            importance = estimate_importance(sentence)
            category_enum = infer_claim_category(sentence)
            
            claim = Claim(
                text=sentence,
                confidence=confidence,
                importance=importance,
                category=category_enum.value if hasattr(category_enum, 'value') else category_enum,
                source=ClaimSource.EXTRACTED.value,
                justification_tokens=[]
            )
            claims.append(claim)
    
    return claims


def infer_confidence_from_text(claim_text: str) -> float:
    """
    Infer confidence level from textual indicators.
    Ranges from 0.0 (uncertain) to 1.0 (certain).
    """
    text_lower = claim_text.lower()
    
    # Explicitly stated percentages
    percent_match = re.search(r'(\d+)\s*%', text_lower)
    if percent_match:
        return min(1.0, int(percent_match.group(1)) / 100.0)
    
    # High confidence indicators
    high_confidence_words = [
        'definitely', 'certainly', 'absolutely', 'clearly', 'obviously',
        'proven', 'verified', 'confirmed', 'must', 'undoubtedly'
    ]
    if any(word in text_lower for word in high_confidence_words):
        return 0.85
    
    # Medium-high confidence
    medium_high_words = [
        'likely', 'probably', 'generally', 'typically', 'usually',
        'appears', 'suggests', 'indicates'
    ]
    if any(word in text_lower for word in medium_high_words):
        return 0.70
    
    # Medium confidence
    medium_words = [
        'may', 'might', 'could', 'possibly', 'perhaps', 'seems'
    ]
    if any(word in text_lower for word in medium_words):
        return 0.55
    
    # Low confidence
    low_confidence_words = [
        'unlikely', 'questionable', 'doubtful', 'uncertain', 'unclear'
    ]
    if any(word in text_lower for word in low_confidence_words):
        return 0.30
    
    # Default to medium confidence
    return 0.60


def estimate_importance(text: str) -> float:
    """Estimate importance of a claim based on text features."""
    # Longer sentences tend to be more important
    length_score = min(1.0, len(text.split()) / 20.0)
    
    # Claims with numbers/data are more important
    data_score = 0.3 if re.search(r'\d+', text) else 0.0
    
    # Qualitative score
    return min(1.0, (length_score * 0.5 + data_score + 0.2))


def infer_claim_category(text: str) -> str:
    """Infer the category of a claim."""
    text_lower = text.lower()
    
    if any(word in text_lower for word in ['number', 'count', 'percentage', 'amount', 'data', 'statistic']):
        return ClaimCategory.FACTUAL
    elif any(word in text_lower for word in ['should', 'ought', 'recommend', 'suggest', 'best', 'better']):
        return ClaimCategory.PROCEDURAL
    elif any(word in text_lower for word in ['because', 'due to', 'caused by', 'reason', 'explanation']):
        return ClaimCategory.PROCEDURAL
    elif any(word in text_lower for word in ['think', 'believe', 'feel', 'opinion', 'seems', 'appears']):
        return ClaimCategory.OPINION
    else:
        return ClaimCategory.FACTUAL


# ============================================================================
# 4. CONFIDENCE AGGREGATION
# ============================================================================

def compute_aggregate_confidence(claims: List[Claim]) -> Dict:
    """
    Compute aggregate confidence metrics across claims.
    
    Returns dict with:
    - mean: average confidence
    - median: median confidence
    - min: minimum confidence
    - max: maximum confidence
    - stdev: standard deviation
    - distribution: binned distribution
    """
    if not claims:
        return {
            "mean": 0.0,
            "median": 0.0,
            "min": 0.0,
            "max": 0.0,
            "stdev": 0.0,
            "distribution": [0] * 10,
        }
    
    confidences = [c.confidence for c in claims]
    
    # Compute statistics
    mean = statistics.mean(confidences)
    median = statistics.median(confidences)
    min_conf = min(confidences)
    max_conf = max(confidences)
    
    stdev = statistics.stdev(confidences) if len(confidences) > 1 else 0.0
    
    # Create 10-bin distribution
    distribution = [0] * 10
    for conf in confidences:
        bin_idx = min(9, int(conf * 10))
        distribution[bin_idx] += 1
    
    return {
        "mean": round(mean, 3),
        "median": round(median, 3),
        "min": round(min_conf, 3),
        "max": round(max_conf, 3),
        "stdev": round(stdev, 3),
        "distribution": distribution,
    }


# ============================================================================
# 5. UNCERTAINTY DETECTION
# ============================================================================

def extract_uncertainties(text: str, claims: List[Claim] = None) -> List[Uncertainty]:
    """
    Extract uncertainty statements from reasoning text.
    Identifies expressions of doubt, ambiguity, or open questions.
    """
    # Guard: Handle None or empty input
    if not text:
        return []
    
    uncertainties = []
    
    # Patterns indicating uncertainty
    uncertainty_patterns = [
        r"uncertain(?:ty)?(?:\s+about|\s+whether)?\s+(.+?)(?:[.!?]|$)",
        r"not\s+sure\s+(?:if|whether|about)?\s+(.+?)(?:[.!?]|$)",
        r"unclear\s+(?:whether|if)?\s+(.+?)(?:[.!?]|$)",
        r"(?:may|might)\s+(?:not\s+)?(?:be|have)\s+(.+?)(?:[.!?]|$)",
        r"(?:could|can't)\s+(?:determine|tell|say)\s+(.+?)(?:[.!?]|$)",
    ]
    
    matches = []
    for pattern in uncertainty_patterns:
        matches.extend(re.finditer(pattern, text, re.IGNORECASE))
    
    for match in matches:
        statement = match.group(0).strip()
        about_claim = match.group(1).strip() if match.groups() else None
        
        # Estimate severity based on confidence words
        severity = 0.7  # Default
        if any(word in statement.lower() for word in ['major', 'significant', 'important']):
            severity = 0.9
        elif any(word in statement.lower() for word in ['minor', 'slight']):
            severity = 0.3
        
        uncertainty = Uncertainty(
            statement=statement,
            about_claim=about_claim,
            severity=severity
        )
        uncertainties.append(uncertainty)
    
    # Link uncertainties to claims if claims provided
    if claims:
        for uncertainty in uncertainties:
            for claim in claims:
                if uncertainty.about_claim and uncertainty.about_claim.lower() in claim.text.lower():
                    uncertainty.about_claim = claim.text
                    break
    
    return uncertainties


# ============================================================================
# 6. CONTRADICTION DETECTION (4-LAYER)
# ============================================================================

def detect_internal_contradictions(claims: List[Claim]) -> List[Contradiction]:
    """
    Detect contradictions between claims using 4-layer analysis:
    1. DIRECT_NEGATION: One claim directly negates another
    2. NUMERIC_CONFLICT: Conflicting numbers/quantities
    3. LOGICAL: Violates logical consistency (A ∧ ¬A)
    4. CONSISTENCY: General consistency check
    """
    contradictions = []
    
    for i, claim_a in enumerate(claims):
        for claim_b in claims[i+1:]:
            contradiction = detect_pairwise_contradiction(claim_a, claim_b)
            if contradiction:
                contradictions.append(contradiction)
    
    return contradictions


def detect_pairwise_contradiction(claim_a: Claim, claim_b: Claim) -> Optional[Contradiction]:
    """Detect contradiction between two specific claims."""
    
    text_a = claim_a.text.lower()
    text_b = claim_b.text.lower()
    
    # Layer 1: DIRECT NEGATION
    negation_patterns = [
        (r'\bnot\s+', 'negation prefix'),
        (r'no\s+', 'negation prefix'),
        (r'doesn\'t\s+', 'negation prefix'),
        (r'isn\'t\s+', 'negation prefix'),
    ]
    
    a_negated = any(re.search(pattern, text_a) for pattern, _ in negation_patterns)
    b_negated = any(re.search(pattern, text_b) for pattern, _ in negation_patterns)
    
    # Check if one is negation of the other
    text_a_clean = re.sub(r'\b(not|no|doesn\'t|isn\'t|doesn\'t|isn\'t)\s+', '', text_a)
    text_b_clean = re.sub(r'\b(not|no|doesn\'t|isn\'t|doesn\'t|isn\'t)\s+', '', text_b)
    
    similarity = compute_text_similarity(text_a_clean, text_b_clean)
    if similarity > 0.7 and a_negated != b_negated:
        return Contradiction(
            claim_a=claim_a.text,
            claim_b=claim_b.text,
            contradiction_type=ContradictionType.DIRECT_NEGATION.value,
            severity=0.9,
            evidence="Negation patterns detected"
        )
    
    # Layer 2: NUMERIC CONFLICT
    numbers_a = re.findall(r'\d+(?:\.\d+)?', text_a)
    numbers_b = re.findall(r'\d+(?:\.\d+)?', text_b)
    
    if numbers_a and numbers_b:
        # If claims mention same subject with different numbers
        subject_match = compute_text_similarity(text_a_clean, text_b_clean) > 0.6
        if subject_match and numbers_a != numbers_b:
            return Contradiction(
                claim_a=claim_a.text,
                claim_b=claim_b.text,
                contradiction_type=ContradictionType.NUMERIC_CONFLICT.value,
                severity=0.8,
                evidence=f"Different numbers: {numbers_a} vs {numbers_b}"
            )
    
    # Layer 3: LOGICAL CONSISTENCY
    # Check for explicit logical conflicts
    logical_keywords = ['all', 'none', 'some', 'and', 'or']
    if any(kw in text_a for kw in logical_keywords) and any(kw in text_b for kw in logical_keywords):
        if _violates_logical_consistency(text_a, text_b):
            return Contradiction(
                claim_a=claim_a.text,
                claim_b=claim_b.text,
                contradiction_type=ContradictionType.LOGICAL.value,
                severity=0.75,
                evidence="Logical consistency violation"
            )
    
    # Layer 4: SEMANTIC CONSISTENCY (general consistency check)
    consistency_score = _compute_consistency_score(claim_a, claim_b)
    if consistency_score < 0.3:
        return Contradiction(
            claim_a=claim_a.text,
            claim_b=claim_b.text,
            contradiction_type=ContradictionType.UNKNOWN.value,
            severity=0.5,
            evidence=f"Low consistency score: {consistency_score}"
        )
    
    return None


def compute_text_similarity(text_a: str, text_b: str) -> float:
    """Compute jaccard similarity between two texts."""
    words_a = set(text_a.split())
    words_b = set(text_b.split())
    
    if not words_a or not words_b:
        return 0.0
    
    intersection = len(words_a & words_b)
    union = len(words_a | words_b)
    
    return intersection / union if union > 0 else 0.0


def _violates_logical_consistency(text_a: str, text_b: str) -> bool:
    """Check if two statements violate logical consistency."""
    # Simple heuristic: check for "all" vs "none" patterns
    all_a = 'all' in text_a and 'none' in text_b
    all_b = 'all' in text_b and 'none' in text_a
    
    if all_a or all_b:
        return True
    
    # Check for contradictory quantifiers
    if ('some' in text_a and 'none' in text_b) or ('some' in text_b and 'none' in text_a):
        return True
    
    return False


def _compute_consistency_score(claim_a: Claim, claim_b: Claim) -> float:
    """
    Compute consistency score between two claims.
    Higher score = more consistent (0.0-1.0).
    """
    # Factor 1: Text similarity
    similarity = compute_text_similarity(claim_a.text.lower(), claim_b.text.lower())
    
    # Factor 2: Confidence alignment
    conf_diff = abs(claim_a.confidence - claim_b.confidence)
    confidence_alignment = 1.0 - (conf_diff * 0.5)
    
    # Factor 3: Category alignment
    category_match = 1.0 if claim_a.category == claim_b.category else 0.7
    
    # Weighted average
    return (similarity * 0.4 + confidence_alignment * 0.3 + category_match * 0.3)


# ============================================================================
# 7. QUALITY SCORING
# ============================================================================

def compute_quality_score(
    confidence: float,
    contradiction_count: int,
    uncertainty_ratio: float,
    completeness: float,
    reasoning_depth: float,
    weights: Optional[Dict[str, float]] = None
) -> Dict:
    """
    Compute overall quality score for perspective analysis.
    
    Quality breakdown:
    - confidence: aggregate confidence of claims [0-1]
    - consistency: 1 - (contradictions / claims), penalizes internal contradictions
    - depth: reasoning_depth score [0-1]
    - completeness: ratio of coverage [0-1]
    - uncertainty_ratio: severity of unexplained uncertainties [0-1]
    
    Args:
        weights: Optional weights dict with keys: confidence, consistency, depth, completeness
                Default from DEFAULT_ADAPTIVE_CONFIG
    """
    if weights is None:
        weights = {
            "confidence": 0.4,
            "consistency": 0.3,
            "depth": 0.15,
            "completeness": 0.15,
        }
    
    # Consistency score: penalize contradictions
    consistency = max(0.0, 1.0 - (contradiction_count * 0.15))
    
    # Uncertainty penalty
    uncertainty_penalty = uncertainty_ratio * 0.3
    
    # Compute weighted score
    components = {
        "confidence": confidence,
        "consistency": consistency,
        "depth": reasoning_depth,
        "completeness": completeness,
    }
    
    overall_score = sum(
        components.get(key, 0.0) * weight
        for key, weight in weights.items()
    )
    
    # Apply uncertainty penalty
    overall_score = max(0.0, overall_score - uncertainty_penalty)
    
    return {
        "overall": round(min(1.0, overall_score), 3),
        "components": {key: round(value, 3) for key, value in components.items()},
        "uncertainty_penalty": round(uncertainty_penalty, 3),
    }


# ============================================================================
# 8. MAIN ANALYZER ORCHESTRATION
# ============================================================================

def analyze_perspective(
    reasoning_output: str,
    perspective_id: str,
    height: int,
    model_tier: str = "light",
    weights: Optional[Dict[str, float]] = None
) -> PerspectiveAnalysis:
    """
    Orchestrate complete analysis of a single perspective's reasoning output.
    
    Args:
        reasoning_output: Raw reasoning text in any supported format
        perspective_id: Unique identifier for this perspective
        height: Number of reasoning passes
        model_tier: "light", "medium", or "heavy"
        weights: Optional quality score weights
    
    Returns:
        PerspectiveAnalysis with all analysis results integrated
    """
    
    # Guard: Handle None input
    if reasoning_output is None:
        return PerspectiveAnalysis(
            perspective_id=perspective_id,
            height=height,
            model_tier=model_tier,
            claims=[],
            aggregate_confidence=0.0
        )
    
    # Step 1: Detect and parse format
    format_type = detect_format(reasoning_output)
    
    if format_type == "json":
        parsed = parse_json_format(reasoning_output)
    elif format_type == "markdown":
        parsed = parse_markdown_format(reasoning_output)
    elif format_type == "xml":
        parsed = parse_xml_format(reasoning_output)
    else:
        parsed = parse_free_text_format(reasoning_output)
    
    # Step 2: Extract claims
    claims = extract_claims(parsed, reasoning_output)
    
    # Step 3: Extract uncertainties
    uncertainties = extract_uncertainties(reasoning_output, claims)
    
    # Step 4: Detect contradictions
    internal_contradictions = detect_internal_contradictions(claims)
    
    # Step 5: Compute aggregate metrics
    confidence_metrics = compute_aggregate_confidence(claims)
    aggregate_confidence = confidence_metrics["mean"]
    
    uncertainty_ratio = len(uncertainties) / len(claims) if claims else 0.0
    
    # Step 6: Estimate completeness and depth
    completeness = min(1.0, len(claims) / 5.0)  # Scale: 5+ claims = 100% complete
    reasoning_depth = min(1.0, len(reasoning_output.split()) / 500.0)  # Scale: 500+ words
    
    # Step 7: Compute quality score
    quality_scores = compute_quality_score(
        confidence=aggregate_confidence,
        contradiction_count=len(internal_contradictions),
        uncertainty_ratio=uncertainty_ratio,
        completeness=completeness,
        reasoning_depth=reasoning_depth,
        weights=weights
    )
    
    # Step 8: Create PerspectiveAnalysis object
    analysis = PerspectiveAnalysis(
        perspective_id=perspective_id,
        height=height,
        model_tier=model_tier,
        claims=claims,
        aggregate_confidence=aggregate_confidence,
        confidence_distribution=confidence_metrics,
        uncertainties=uncertainties,
        internal_contradictions=internal_contradictions,
        quality_score=quality_scores["overall"],
        quality_breakdown=quality_scores["components"],
        completeness_score=completeness,
        reasoning_depth=len(reasoning_output.split()),
    )
    
    return analysis


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def batch_analyze_perspectives(
    reasoning_outputs: Dict[str, str],
    heights: Dict[str, int],
    model_tiers: Dict[str, str] = None,
    weights: Optional[Dict[str, float]] = None
) -> Dict[str, PerspectiveAnalysis]:
    """
    Analyze multiple perspectives in batch.
    
    Args:
        reasoning_outputs: {perspective_id: reasoning_text}
        heights: {perspective_id: height}
        model_tiers: {perspective_id: tier}, defaults to "light"
        weights: Optional quality score weights
    
    Returns:
        {perspective_id: PerspectiveAnalysis}
    """
    if model_tiers is None:
        model_tiers = {pid: "light" for pid in reasoning_outputs.keys()}
    
    results = {}
    for perspective_id, output in reasoning_outputs.items():
        height = heights.get(perspective_id, 1)
        tier = model_tiers.get(perspective_id, "light")
        
        analysis = analyze_perspective(
            reasoning_output=output,
            perspective_id=perspective_id,
            height=height,
            model_tier=tier,
            weights=weights
        )
        results[perspective_id] = analysis
    
    return results
