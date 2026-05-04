"""Tests for async/await fixes in orchestrator.py.

Tests coverage:
- Unit test: validate_batch async execution
- Integration test: batch validation in async pipeline
- Edge case: validation timeout during processing
- Concurrency test: multiple batches validating simultaneously
"""

import asyncio
import pytest
from typing import Any, List, Optional, Dict
from unittest.mock import AsyncMock
from dataclasses import dataclass

# Add path for imports
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.orchestrator import (
    _validate_claims_against_ground_truth,
    _extract_claims_from_pass_output,
    _run_alarm_scan,
    ValidationData,
    _build_claim_data,
)


# ─────────────────────────────────────────────────────────────────────────────
# Mock Ground Truth Provider
# ─────────────────────────────────────────────────────────────────────────────


class MockGroundTruthProvider:
    """Mock provider that simulates async validation."""
    
    def __init__(self, delay: float = 0.0, fail: bool = False):
        self.delay = delay
        self.fail = fail
        self.call_count = 0
    
    async def validate_batch(self, claims: List[Any]) -> List[Dict[str, Any]]:
        """Simulate async validation with optional delay and failure."""
        self.call_count += 1
        
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        
        if self.fail:
            raise RuntimeError("Simulated validation failure")
        
        return [
            {
                "statement": claim.statement if hasattr(claim, 'statement') else str(claim),
                "is_hallucination": False,
                "is_contradiction": False,
                "grounding_confidence": 0.85,
            }
            for claim in claims
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Unit Tests: validate_batch async execution
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_claims_awaited():
    """Test that _validate_claims_against_ground_truth properly awaits validate_batch."""
    provider = MockGroundTruthProvider()
    
    claims = [
        _build_claim_data("The system is operational", 0.9, "fact", 1),
        _build_claim_data("CPU usage is below 50%", 0.8, "inference", 2),
    ]
    
    result = await _validate_claims_against_ground_truth(claims, provider)
    
    # Provider should be called exactly once
    assert provider.call_count == 1, f"Expected 1 call but got {provider.call_count}"
    
    # Result should be properly populated
    assert result is not None, f"Result is None, provider returned: {provider.call_count} calls"
    assert len(result.claims) == 2
    assert result.hallucination_count == 0
    assert result.overall_confidence > 0.8


@pytest.mark.asyncio
async def test_validate_claims_no_provider():
    """Test validation gracefully handles missing provider."""
    claims = [_build_claim_data("test", 0.5, "fact", 1)]
    
    result = await _validate_claims_against_ground_truth(claims, ground_truth_provider=None)
    assert result is None


@pytest.mark.asyncio
async def test_validate_claims_empty_claims():
    """Test validation gracefully handles empty claims list."""
    provider = MockGroundTruthProvider()
    
    result = await _validate_claims_against_ground_truth([], provider)
    assert result is None
    
    # Provider should not be called
    assert provider.call_count == 0


@pytest.mark.asyncio
async def test_validate_claims_timeout():
    """Test that validation timeout is properly caught."""
    provider = MockGroundTruthProvider(delay=5.0)  # Will timeout
    
    claims = [_build_claim_data("test", 0.5, "fact", 1)]
    
    result = await _validate_claims_against_ground_truth(
        claims,
        provider,
        timeout_secs=0.1,  # Very short timeout
    )
    
    # Should return None on timeout instead of raising
    assert result is None


@pytest.mark.asyncio
async def test_validate_claims_provider_error():
    """Test that provider errors are properly caught and logged."""
    provider = MockGroundTruthProvider(fail=True)
    
    claims = [_build_claim_data("test", 0.5, "fact", 1)]
    
    result = await _validate_claims_against_ground_truth(claims, provider)
    
    # Should return None on error instead of raising
    assert result is None
    assert provider.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# Integration Tests: batch validation in async pipeline
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_alarm_scan_validates_claims():
    """Test that _run_alarm_scan properly awaits validation."""
    provider = MockGroundTruthProvider()
    
    pass_output = """
    CLAIM: The system is responding to requests [CONFIDENCE: 90%]
    CLAIM: Database connections are healthy [CONFIDENCE: 85%]
    CONCLUSION: System is operational
    """
    
    result = await _run_alarm_scan(pass_output, provider)
    
    # Should have results from validation
    assert result is not None
    assert "total_claims" in result
    assert "overall_confidence" in result
    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_alarm_scan_no_claims():
    """Test alarm scan with pass output that yields no claims."""
    provider = MockGroundTruthProvider()
    
    pass_output = "This is just regular text with no structured claims."
    
    result = await _run_alarm_scan(pass_output, provider)
    
    # With no claims, validation should return None
    assert result is None
    
    # Provider should not be called
    assert provider.call_count == 0


@pytest.mark.asyncio
async def test_alarm_scan_no_provider():
    """Test alarm scan gracefully handles missing provider."""
    pass_output = "CLAIM: Test claim [CONFIDENCE: 80%]"
    
    result = await _run_alarm_scan(pass_output, ground_truth_provider=None)
    
    # Should return None when provider is not available
    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Edge Cases: validation timeout during processing
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_timeout_recovery():
    """Test that timeout in one validation doesn't affect others."""
    # First provider times out
    timeout_provider = MockGroundTruthProvider(delay=5.0)
    
    # Second provider works fine
    fast_provider = MockGroundTruthProvider(delay=0.01)
    
    claims = [_build_claim_data("test", 0.5, "fact", 1)]
    
    # First validation times out
    result1 = await _validate_claims_against_ground_truth(
        claims,
        timeout_provider,
        timeout_secs=0.05,
    )
    assert result1 is None
    
    # Second validation should succeed
    result2 = await _validate_claims_against_ground_truth(
        claims,
        fast_provider,
        timeout_secs=1.0,
    )
    assert result2 is not None
    assert len(result2.claims) == 1


@pytest.mark.asyncio
async def test_validate_partial_claims():
    """Test validation with claims built through _build_claim_data."""
    provider = MockGroundTruthProvider()
    
    claims = [
        _build_claim_data("Complete claim", 0.9, "fact", 1),
        _build_claim_data("Claim without explicit confidence", 0.8, "inference", 2),
        _build_claim_data("Claim three", 0.7, "fact", 3),
    ]
    
    result = await _validate_claims_against_ground_truth(claims, provider)
    
    assert result is not None
    assert len(result.claims) == 3
    assert provider.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# Concurrency Tests: multiple batches validating simultaneously
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_validations():
    """Test that multiple validations can run concurrently without blocking."""
    provider1 = MockGroundTruthProvider(delay=0.1)
    provider2 = MockGroundTruthProvider(delay=0.1)
    provider3 = MockGroundTruthProvider(delay=0.1)
    
    claims_set = [
        [_build_claim_data(f"Claim 1-{i}", 0.8, "fact", i) for i in range(3)],
        [_build_claim_data(f"Claim 2-{i}", 0.7, "inference", i) for i in range(2)],
        [_build_claim_data(f"Claim 3-{i}", 0.9, "fact", i) for i in range(4)],
    ]
    
    # Run all validations concurrently
    import time
    start_time = time.time()
    
    results = await asyncio.gather(
        _validate_claims_against_ground_truth(claims_set[0], provider1),
        _validate_claims_against_ground_truth(claims_set[1], provider2),
        _validate_claims_against_ground_truth(claims_set[2], provider3),
    )
    
    elapsed = time.time() - start_time
    
    # All should complete successfully
    assert all(r is not None for r in results)
    assert len(results[0].claims) == 3
    assert len(results[1].claims) == 2
    assert len(results[2].claims) == 4
    
    # Should take ~0.1 seconds (concurrent), not 0.3 seconds (sequential)
    assert elapsed < 0.25, f"Took {elapsed}s, expected < 0.25s for concurrent execution"


@pytest.mark.asyncio
async def test_concurrent_validations_with_mix():
    """Test concurrent validations with both successful and failed providers."""
    good_provider = MockGroundTruthProvider(delay=0.05)
    bad_provider = MockGroundTruthProvider(delay=0.05, fail=True)
    timeout_provider = MockGroundTruthProvider(delay=2.0)
    
    claims = [_build_claim_data("test", 0.5, "fact", 1)]
    
    results = await asyncio.gather(
        _validate_claims_against_ground_truth(claims, good_provider),
        _validate_claims_against_ground_truth(claims, bad_provider),
        _validate_claims_against_ground_truth(
            claims,
            timeout_provider,
            timeout_secs=0.1,
        ),
    )
    
    # First succeeds
    assert results[0] is not None
    assert len(results[0].claims) == 1
    
    # Second fails gracefully
    assert results[1] is None
    
    # Third times out gracefully
    assert results[2] is None


# ─────────────────────────────────────────────────────────────────────────────
# Regression Tests: ensure fixes don't break existing functionality
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_claims_still_works():
    """Test that claim extraction still works with validation changes."""
    pass_output = """
    The system shows these key findings:
    (key finding): Memory usage is stable at 45%
    CLAIM: All services are responding within SLA [CONFIDENCE: 92%]
    CONCLUSION: System is healthy
    """
    
    claims = _extract_claims_from_pass_output(pass_output)
    
    # Should extract multiple claims
    assert len(claims) > 0
    assert isinstance(claims, list)
    assert all(isinstance(c, dict) for c in claims)


@pytest.mark.asyncio
async def test_validation_result_structure():
    """Test that ValidationData is properly structured after async validation."""
    provider = MockGroundTruthProvider()
    
    claims = [
        _build_claim_data("Test 1", 0.9, "fact", 1),
        _build_claim_data("Test 2", 0.7, "inference", 2),
    ]
    
    result = await _validate_claims_against_ground_truth(claims, provider)
    
    # Should have all expected fields
    assert len(result.claims) == 2
    assert result.hallucination_count == 0
    assert isinstance(result.overall_confidence, float)
    assert isinstance(result.contradictions, list)
    assert isinstance(result.validation_results, list)


# ─────────────────────────────────────────────────────────────────────────────
# Run Tests
# ─────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
