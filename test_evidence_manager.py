from collections import deque

from evidence_manager import add_to_cache
from models_evidence import EvidenceCache, EvidenceEntry


def _entry(evidence_id: str) -> EvidenceEntry:
    return EvidenceEntry(
        evidence_id=evidence_id,
        tool_name="code_search",
        query=evidence_id,
        results="result",
        tool_status="success",
    )


def test_add_to_cache_replaces_existing_without_duplicate_lru_markers():
    cache = EvidenceCache(capacity=3)

    add_to_cache(_entry("dup-key"), cache)
    add_to_cache(_entry("dup-key"), cache)

    assert list(cache.access_order).count("dup-key") == 1
    assert len(cache.entries) == 1


def test_add_to_cache_eviction_skips_stale_access_order_keys():
    cache = EvidenceCache(capacity=1)
    add_to_cache(_entry("old"), cache)

    cache.access_order = deque(["stale-key", "old"])
    add_to_cache(_entry("new"), cache)

    assert "new" in cache.entries
    assert "old" not in cache.entries
