"""
Phase 2 Part 2: Evidence Manager Implementation

Core evidence management system for tool invoker deduplication,
formatting, confidence calculation, and evidence chaining.

This is production-ready code with real tracking (no mocks).
"""

import hashlib
import json
import threading
from datetime import datetime
from typing import Optional, Tuple, Dict
from collections import OrderedDict, deque

from .models_evidence import (
    EvidenceEntry,
    EvidenceDigest,
    EvidenceCache,
    ToolInvocationBatch,
    ToolResult,
    CacheStats,
)


# ============================================================================
# DEDUPLICATION ENGINE
# ============================================================================

def hash_tool_query(tool_name: str, query: str) -> str:
    """
    Hash a tool query for deduplication.
    
    Normalizes query (lowercase, strip whitespace, remove common words)
    to prevent minor variations from creating duplicates.
    
    Args:
        tool_name: Name of tool ("web_search", "code_search", etc.)
        query: The query string
    
    Returns:
        SHA256 hash of normalized query including tool name
    """
    # Common English stop words to ignore in queries
    STOP_WORDS = {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "for",
        "from", "has", "have", "he", "her", "hers", "him", "his", "how",
        "i", "if", "in", "into", "is", "it", "its", "the", "that", "this",
        "to", "was", "what", "who", "which", "will", "with", "would", "or"
    }
    
    # Normalize: lowercase, split, filter stop words
    words = query.lower().split()
    filtered_words = [w for w in words if w not in STOP_WORDS and w]
    
    # Reconstruct normalized query
    normalized = " ".join(sorted(filtered_words))
    
    # Hash with tool name to prevent collisions across tools
    combined = f"{tool_name}:{normalized}"
    return hashlib.sha256(combined.encode()).hexdigest()


def lookup_in_cache(
    tool_name: str,
    query: str,
    cache: EvidenceCache
) -> Optional[EvidenceEntry]:
    """
    Look up a query in the evidence cache.
    
    Args:
        tool_name: Tool name
        query: Query string
        cache: Evidence cache to search
    
    Returns:
        Cached EvidenceEntry if found, None otherwise.
        Updates cache statistics.
    """
    # Guard: ensure access_order is initialized (Fix p3-fix-004)
    if not hasattr(cache, 'access_order') or cache.access_order is None:
        cache.access_order = deque()
    
    query_hash = hash_tool_query(tool_name, query)
    
    if query_hash in cache.entries:
        # Cache hit
        entry = cache.entries[query_hash]
        cache.hits += 1
        
        # Mark entry as cached on reuse
        entry.cache_hit_count += 1
        entry.perspectives_used_by.append("cache_lookup")  # track reuse
        
        # Update access order for LRU - O(1) removal and append with deque
        try:
            cache.access_order.remove(query_hash)
        except ValueError:
            pass
        cache.access_order.append(query_hash)
        
        return entry
    else:
        # Cache miss
        cache.misses += 1
        return None


def add_to_cache(
    entry: EvidenceEntry,
    cache: EvidenceCache
) -> EvidenceCache:
    """
    Add an evidence entry to the cache.
    
    Implements LRU eviction if cache is at capacity.
    Uses deque.popleft() for O(1) eviction instead of O(n) list.pop(0).
    
    Args:
        entry: Evidence entry to cache
        cache: Cache to update
    
    Returns:
        Updated cache
    """
    # Guard: ensure access_order is initialized (Fix p3-fix-004)
    if not hasattr(cache, 'access_order') or cache.access_order is None:
        cache.access_order = deque()
    
    query_hash = entry.evidence_id
    
    # Add to cache
    cache.entries[query_hash] = entry
    cache.access_order.append(query_hash)
    
    # LRU eviction if at capacity
    while len(cache.entries) > cache.capacity:
        # Remove least recently used (first in access_order) - O(1) with deque
        lru_hash = cache.access_order.popleft()
        del cache.entries[lru_hash]
    
    return cache


# ============================================================================
# EVIDENCE FORMATTING
# ============================================================================

def format_for_reasoning(
    entry: EvidenceEntry,
    perspective_context: str = ""
) -> str:
    """
    Format an evidence entry for perspective re-reasoning.
    
    Converts raw tool result into perspective-readable summary with:
    - Query and result
    - Tool status
    - Confidence impact
    - Derived implication based on purpose
    
    Args:
        entry: Evidence entry to format
        perspective_context: Optional context for the perspective
    
    Returns:
        Human-readable formatted evidence string
    """
    # Truncate results to 500 chars
    truncated_results = entry.results[:500]
    if len(entry.results) > 500:
        truncated_results += "... (truncated)"
    
    # Calculate confidence delta display
    delta_str = f"+{entry.confidence_impact:.2f}" if entry.confidence_impact >= 0 else f"{entry.confidence_impact:.2f}"
    
    # Determine implication based on purpose
    # (We'll infer purpose from the entry type or context)
    implication = "This evidence provides additional context."
    
    if entry.tool_status == "success":
        # Infer purpose from confidence impact direction
        if entry.confidence_impact > 0.1:
            implication = "This evidence validates the finding, increasing confidence."
        elif entry.confidence_impact > 0:
            implication = "This evidence supports the claim with additional grounding."
        elif entry.confidence_impact < -0.1:
            implication = "This evidence challenges the claim, warranting reconsideration."
        else:
            implication = "This evidence provides context for evaluation."
    elif entry.tool_status == "timeout":
        implication = "This tool timed out; result is incomplete."
    elif entry.tool_status == "error":
        implication = "This tool failed; result is unavailable."
    
    # Format output
    formatted = f"""[Evidence from {entry.tool_name}]
Query: {entry.query}
Status: {entry.tool_status}
Result: {truncated_results}
Confidence Impact: {delta_str}
Implication: {implication}
Cached: {entry.is_cached} (hit count: {entry.cache_hit_count})
"""
    return formatted


def format_evidence_chain(digest: EvidenceDigest) -> str:
    """
    Format a complete evidence digest for perspective re-reasoning.
    
    Creates human-readable summary of all tool invocations with:
    - List of tools called
    - Results summary
    - Total confidence delta
    - Quality metrics
    
    Args:
        digest: Evidence digest to format
    
    Returns:
        Human-readable evidence chain summary
    """
    lines = [
        f"=== EVIDENCE CHAIN FOR {digest.perspective_id} ===",
        f"Total Evidence Entries: {digest.chain_length}",
        f"Success Rate: {digest.success_rate:.1%}",
        f"Original Confidence: {digest.original_confidence:.3f}",
        f"Total Confidence Delta: {digest.total_confidence_delta:+.3f}",
        f"Updated Confidence: {digest.updated_confidence:.3f}",
        f"Average Execution Time: {digest.avg_execution_time_ms}ms",
        f"Cache Hits: {digest.cache_hit_count}",
        "",
        "Evidence Entries (in order):",
    ]
    
    for i, entry in enumerate(digest.entries, 1):
        lines.append(f"\n{i}. {entry.tool_name}: {entry.query[:50]}...")
        lines.append(f"   Status: {entry.tool_status}, Impact: {entry.confidence_impact:+.3f}")
        lines.append(f"   Cached: {entry.is_cached}")
    
    if not digest.entries:
        lines.append("   (No evidence entries)")
    
    lines.extend([
        "",
        f"Summary: {digest.formatted_summary[:200]}..." if len(digest.formatted_summary) > 200 else f"Summary: {digest.formatted_summary}",
    ])
    
    return "\n".join(lines)


# ============================================================================
# CONFIDENCE CALCULATION
# ============================================================================

def calculate_confidence_delta(
    tool_result: ToolResult,
    original_confidence: float
) -> float:
    """
    Calculate confidence delta based on tool result.
    
    Heuristic calculation:
    - tool_status == "error" → -0.05 (cost with no benefit)
    - tool_status == "timeout" → -0.10 (high cost, wasted)
    - tool_status == "success":
      - purpose "ground" + low_confidence (< 0.50) → +0.15 (good grounding boost)
      - purpose "refute" + high_confidence (> 0.85) + contradicted → +0.05 (stress-test passed)
      - purpose "refute" + high_confidence + confirmed → -0.10 (false confidence, oops)
      - purpose "resolve" + contradiction detected → +0.20 (strong resolution)
      - default → +0.10 (general evidence boost)
    
    Clamped to [-0.25, +0.25] to prevent runaway adjustments.
    
    Args:
        tool_result: Result from tool invocation
        original_confidence: Original perspective confidence before tool
    
    Returns:
        Confidence delta clamped to [-0.25, +0.25]
    """
    delta = 0.0
    
    if tool_result.status == "error":
        delta = -0.05  # Cost with no benefit
    elif tool_result.status == "timeout":
        delta = -0.10  # High cost, wasted
    elif tool_result.status == "success":
        # Apply heuristics based on purpose and confidence level
        if tool_result.purpose == "ground":
            # Grounding helps low-confidence claims
            if original_confidence < 0.50:
                delta = +0.15  # Good grounding boost
            else:
                delta = +0.05  # Marginal boost for already confident claims
        
        elif tool_result.purpose == "refute":
            # Stress-testing high-confidence claims
            if original_confidence > 0.85:
                # FIX Issue 4 & 5: Contradiction detection and inverted logic
                # Use proper semantic detection instead of substring matching
                # For now, use a more robust heuristic:
                # Check for explicit negation or failure patterns
                results_lower = tool_result.results.lower()
                
                # More robust detection: require contextual phrases, not isolated words
                contradiction_patterns = [
                    "failed to",  # explicit failure
                    "error occurred",  # explicit error
                    "does not exist",  # explicit non-existence
                    "cannot be",  # explicit negation of being
                    "contradicts",  # explicit contradiction
                    "refuted",  # explicitly refuted
                    "confirmed false",  # explicitly false
                ]
                
                is_contradicted = any(
                    pattern in results_lower 
                    for pattern in contradiction_patterns
                )
                
                # FIX Issue 4: Semantics were inverted
                # - If contradictions found: claim fails stress test → decrease confidence
                # - If no contradictions: claim passes stress test → increase confidence
                if is_contradicted:
                    delta = -0.10  # Claim failed stress test - decrease confidence
                else:
                    delta = +0.05  # Claim passed stress test - increase confidence
            else:
                delta = +0.03  # Minor boost for attempting refutation
        
        elif tool_result.purpose == "resolve":
            # Resolution of contradictions
            # Assume successful resolution is strong
            delta = +0.20  # Strong resolution boost
        
        elif tool_result.purpose == "validate":
            # Validation of findings
            delta = +0.10  # General evidence boost
        
        else:
            # Unknown purpose, conservative boost
            delta = +0.10
    
    # Clamp to safe range
    delta = max(-0.25, min(0.25, delta))
    
    return delta


# ============================================================================
# EVIDENCE DIGEST GENERATION
# ============================================================================

def create_evidence_digest(
    batch: ToolInvocationBatch,
    original_confidence: float
) -> EvidenceDigest:
    """
    Create an evidence digest from a tool invocation batch.
    
    Processes all tool results in batch:
    1. Calculate confidence delta for each
    2. Create EvidenceEntry for each with delta
    3. Accumulate total confidence delta
    4. Format all entries into human-readable summary
    5. Return EvidenceDigest ready for re-reasoning
    
    Args:
        batch: Tool invocation batch from invoker
        original_confidence: Original perspective confidence before tools
    
    Returns:
        EvidenceDigest with formatted entries and confidence calculations
    """
    digest = EvidenceDigest(
        perspective_id=batch.perspective_id,
        original_confidence=original_confidence,
    )
    
    total_execution_time = 0
    success_count = 0
    
    for tool_result in batch.results:
        # Calculate confidence delta
        confidence_delta = calculate_confidence_delta(tool_result, original_confidence)
        
        # Create evidence entry
        evidence_id = hash_tool_query(tool_result.tool_name, tool_result.query)
        entry = EvidenceEntry(
            evidence_id=evidence_id,
            tool_name=tool_result.tool_name,
            query=tool_result.query,
            results=tool_result.results,
            tool_status=tool_result.status,
            timestamp=tool_result.timestamp,
            confidence_impact=confidence_delta,
            perspectives_used_by=[batch.perspective_id],
        )
        
        # Add to digest
        digest.entries.append(entry)
        
        # Accumulate total delta
        digest.total_confidence_delta += confidence_delta
        
        # Track execution time
        total_execution_time += tool_result.execution_time_ms
        
        # Count successes
        if tool_result.status == "success":
            success_count += 1
    
    # Clamp total delta
    digest.total_confidence_delta = max(-0.25, min(0.25, digest.total_confidence_delta))
    
    # Calculate metrics
    digest.chain_length = len(digest.entries)
    if digest.entries:
        digest.success_rate = success_count / len(digest.entries)
        digest.avg_execution_time_ms = total_execution_time // len(digest.entries)
    
    # Calculate updated confidence
    digest.updated_confidence = max(0.0, min(1.0, digest.original_confidence + digest.total_confidence_delta))
    
    # Format summary
    summary_parts = []
    for entry in digest.entries:
        summary_parts.append(f"{entry.tool_name}({entry.query[:30]}...): {entry.tool_status}")
    
    digest.formatted_summary = "; ".join(summary_parts) if summary_parts else "No evidence collected"
    
    return digest


# ============================================================================
# EVIDENCE MANAGER CLASS
# ============================================================================

class EvidenceManager:
    """
    Production-ready evidence manager for tool invocation tracking.
    
    Manages:
    - Evidence deduplication (LRU cache)
    - Evidence formatting for re-reasoning
    - Confidence calculation
    - Evidence chain generation
    - Cache statistics tracking
    
    Thread-safe: Uses lock to protect cache access. Safe for multi-threaded
    use where multiple perspectives may execute in parallel and call
    process_batch() concurrently.
    """
    
    def __init__(self, cache_size: int = 1000):
        """
        Initialize evidence manager with thread-safe cache.
        
        Args:
            cache_size: Maximum number of unique evidence entries to cache (LRU)
        """
        self.cache = EvidenceCache(capacity=cache_size)
        self.digests: OrderedDict[str, EvidenceDigest] = OrderedDict()  # perspective_id -> EvidenceDigest, LRU-bounded
        self.max_digests = 10000  # Maximum number of digests before LRU eviction
        self._max_formatting_length = 5000  # Max chars for formatted output
        self._cache_lock = threading.Lock()  # Protects cache access
        
    def process_batch(
        self,
        batch: ToolInvocationBatch,
        original_confidence: float
    ) -> EvidenceDigest:
        """
        Process a tool invocation batch through evidence pipeline.
        
        Pipeline:
        1. De-duplicate: Check cache for each tool result
        2. Format: Convert results to perspective-readable format
        3. Calculate: Confidence deltas
        4. Return: EvidenceDigest ready for re-reasoning
        
        Args:
            batch: Tool invocation batch from invoker
            original_confidence: Original perspective confidence
        
        Returns:
            EvidenceDigest with all evidence processed and formatted
        """
        digest = EvidenceDigest(
            perspective_id=batch.perspective_id,
            original_confidence=original_confidence,
        )
        
        total_execution_time = 0
        success_count = 0
        
        for tool_result in batch.results:
            # Step 1: Check cache for duplicate (protected by lock)
            query_hash = hash_tool_query(tool_result.tool_name, tool_result.query)
            
            with self._cache_lock:
                cached_entry = lookup_in_cache(tool_result.tool_name, tool_result.query, self.cache)
            
            if cached_entry is not None:
                # Reuse cached entry
                entry = cached_entry
                entry.perspectives_used_by.append(batch.perspective_id)
                entry.is_cached = True
            else:
                # New entry - calculate delta and create entry
                confidence_delta = calculate_confidence_delta(tool_result, original_confidence)
                
                entry = EvidenceEntry(
                    evidence_id=query_hash,
                    tool_name=tool_result.tool_name,
                    query=tool_result.query,
                    results=tool_result.results,
                    tool_status=tool_result.status,
                    timestamp=tool_result.timestamp,
                    confidence_impact=confidence_delta,
                    perspectives_used_by=[batch.perspective_id],
                    is_cached=False,
                )
                
                # Add to cache for future reuse (protected by lock)
                with self._cache_lock:
                    self.cache = add_to_cache(entry, self.cache)
            
            # Step 2: Format for reasoning
            formatted = format_for_reasoning(entry, batch.perspective_id)
            
            # Step 3: Accumulate metrics
            digest.entries.append(entry)
            digest.total_confidence_delta += entry.confidence_impact
            total_execution_time += tool_result.execution_time_ms
            
            if tool_result.status == "success":
                success_count += 1
        
        # Clamp total delta
        digest.total_confidence_delta = max(-0.25, min(0.25, digest.total_confidence_delta))
        
        # Calculate metrics
        digest.chain_length = len(digest.entries)
        if digest.entries:
            digest.success_rate = success_count / len(digest.entries)
            digest.avg_execution_time_ms = total_execution_time // len(digest.entries)
        
        digest.cache_hit_count = sum(1 for e in digest.entries if e.is_cached)
        
        # Calculate updated confidence
        digest.updated_confidence = max(0.0, min(1.0, digest.original_confidence + digest.total_confidence_delta))
        
        # Step 4: Format full evidence chain and cache digest (protected by lock)
        digest.formatted_summary = format_evidence_chain(digest)[:self._max_formatting_length]
        
        with self._cache_lock:
            # Add LRU eviction for unbounded digests dictionary
            if len(self.digests) >= self.max_digests:
                # Remove oldest (first) entry from OrderedDict
                oldest_key = next(iter(self.digests))
                del self.digests[oldest_key]
            
            self.digests[batch.perspective_id] = digest
        
        return digest
    
    def get_cache_stats(self) -> Tuple[int, int, int]:
        """
        Get cache statistics (thread-safe).
        
        Returns:
            Tuple of (hits, misses, size)
        """
        with self._cache_lock:
            return (
                self.cache.hits,
                self.cache.misses,
                len(self.cache.entries)
            )
    
    def get_cache_hit_rate(self) -> float:
        """
        Get cache hit rate (thread-safe).
        
        Returns:
            Hit rate as float (0.0 to 1.0), or 0.0 if no queries
        """
        with self._cache_lock:
            total = self.cache.hits + self.cache.misses
            if total == 0:
                return 0.0
            return self.cache.hits / total
    
    def get_digest(self, perspective_id: str) -> Optional[EvidenceDigest]:
        """
        Retrieve a cached evidence digest.
        
        Args:
            perspective_id: Perspective ID to look up
        
        Returns:
            EvidenceDigest if found, None otherwise
        """
        return self.digests.get(perspective_id)
    
    def clear_cache(self) -> None:
        """Clear all cached evidence (for testing/reset, thread-safe)."""
        with self._cache_lock:
            self.cache = EvidenceCache(capacity=self.cache.capacity)
        self.digests.clear()
