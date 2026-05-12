"""
Phase 2 Part 2: Evidence Manager Schemas

Evidence tracking, deduplication, formatting, and confidence calculation
for tool invocation results in adaptive reasoning.

Tracks tool invocations, deduplicates repeated searches, formats evidence
summaries for perspective re-reasoning, calculates confidence deltas, and
generates evidence chains for final output.

This module is the data contract between Tool Invoker and Evidence Manager.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Dict, List, Deque
import hashlib
import json
from collections import deque

VALID_TOOL_PURPOSES = {"ground", "refute", "resolve", "validate", "unknown"}


# ============================================================================
# TOOL RESULT & INVOCATION BATCH (From Tool Invoker)
# ============================================================================

@dataclass
class ToolResult:
    """Result from a single tool invocation."""
    
    directive_id: str                # which ToolDirective produced this
    tool_name: str                   # "web_search", "code_search", etc.
    query: str                        # what was queried
    purpose: str                     # "ground", "refute", "resolve", "validate"
    status: str                      # "success", "timeout", "error"
    results: str                     # raw tool output (JSON/text)
    error_message: Optional[str] = None  # if status != "success"
    execution_time_ms: int = 0       # how long did this take?
    confidence_impact: float = 0.0   # confidence delta from the invoker wrapper
    timestamp: datetime = field(default_factory=datetime.now)
    
    def __post_init__(self):
        """Validate tool result configuration."""
        valid_statuses = {"success", "timeout", "error"}
        if self.status not in valid_statuses:
            raise ValueError(f"Status must be one of {valid_statuses}, got {self.status}")
        
        if self.purpose not in VALID_TOOL_PURPOSES:
            raise ValueError(f"Purpose must be one of {VALID_TOOL_PURPOSES}, got {self.purpose}")


@dataclass
class ToolInvocationBatch:
    """Batch of tool invocations for a single perspective."""
    
    perspective_id: str
    directives: List[Any]             # Original ToolDirective objects or dicts that were executed
    results: List[ToolResult]         # Results from executing those directives
    total_time_ms: int = 0            # Total time spent invoking tools
    budget_consumed: int = 0          # Budget tokens/calls consumed
    
    def __post_init__(self):
        """Validate batch integrity."""
        if not self.perspective_id:
            raise ValueError("perspective_id is required")


# ============================================================================
# EVIDENCE ENTRY (Core Unit of Evidence)
# ============================================================================

@dataclass
class EvidenceEntry:
    """Single piece of evidence from a tool invocation."""
    
    # Identity
    evidence_id: str                  # UUID or hash of query (for dedup)
    tool_name: str                    # "web_search", "code_search", "nova_verify"
    query: str
    
    # Results
    results: str                      # formatted tool output
    tool_status: str                  # "success", "timeout", "error"
    
    # Metadata
    timestamp: datetime = field(default_factory=datetime.now)
    perspectives_used_by: List[str] = field(default_factory=list)  # which perspectives consumed this
    
    # Confidence impact
    confidence_impact: float = 0.0    # delta to apply to perspective confidence
    
    # Cached result marker
    is_cached: bool = False           # whether this was retrieved from cache
    cache_hit_count: int = 0          # how many times reused
    
    def __post_init__(self):
        """Validate evidence entry."""
        valid_statuses = {"success", "timeout", "error"}
        if self.tool_status not in valid_statuses:
            raise ValueError(f"Status must be one of {valid_statuses}, got {self.tool_status}")
        
        if not self.evidence_id:
            raise ValueError("evidence_id is required")


# ============================================================================
# EVIDENCE DIGEST (Perspective-level Evidence Summary)
# ============================================================================

@dataclass
class EvidenceDigest:
    """Complete evidence summary for a perspective."""
    
    perspective_id: str
    entries: List[EvidenceEntry] = field(default_factory=list)
    
    # Confidence calculation
    original_confidence: float = 0.0
    total_confidence_delta: float = 0.0
    updated_confidence: float = 0.0  # original + total_delta
    
    # Summary
    formatted_summary: str = ""       # human-readable for re-reasoning
    chain_length: int = 0             # number of evidence entries
    
    # Quality metrics
    success_rate: float = 1.0         # percentage of successful tool calls
    avg_execution_time_ms: int = 0
    cache_hit_count: int = 0          # total cache hits
    
    # Timestamps
    created_at: datetime = field(default_factory=datetime.now)
    
    def __post_init__(self):
        """Compute derived metrics."""
        self.chain_length = len(self.entries)
        
        # Calculate success rate
        if self.entries:
            successes = sum(1 for e in self.entries if e.tool_status == "success")
            self.success_rate = successes / len(self.entries)
        
        # Calculate updated confidence
        self.updated_confidence = max(0.0, min(1.0, self.original_confidence + self.total_confidence_delta))
        
        # Count cache hits
        self.cache_hit_count = sum(1 for e in self.entries if e.is_cached)


# ============================================================================
# EVIDENCE CACHE (Deduplication Cache)
# ============================================================================

@dataclass
class EvidenceCache:
    """In-memory cache for deduplicating tool invocations."""
    
    # query_hash -> EvidenceEntry
    entries: Dict[str, EvidenceEntry] = field(default_factory=dict)
    
    # Metadata
    capacity: int = 1000               # LRU cache size limit
    hits: int = 0                     # successful lookups
    misses: int = 0                   # unsuccessful lookups
    
    # LRU tracking (deque for O(1) popleft)
    access_order: Deque[str] = field(default_factory=deque)
    
    def __post_init__(self):
        """Validate cache configuration."""
        if self.capacity <= 0:
            raise ValueError(f"Capacity must be > 0, got {self.capacity}")


# ============================================================================
# CACHE STATISTICS
# ============================================================================

@dataclass
class CacheStats:
    """Statistics about cache performance."""
    
    hits: int = 0
    misses: int = 0
    size: int = 0
    hit_rate: float = 0.0
    
    @property
    def total_queries(self) -> int:
        """Total cache queries."""
        return self.hits + self.misses
