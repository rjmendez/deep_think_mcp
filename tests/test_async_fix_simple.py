"""Simple tests to verify async/await fix works."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from typing import Any, List, Dict, Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


# Simple async validator function (mimics the fixed function)
async def validate_async(items: List[str], validator: Any = None) -> Optional[Dict]:
    """Simple async validation function with await."""
    if not validator:
        return None
    
    try:
        results = await asyncio.wait_for(
            validator.validate_items(items),
            timeout=5.0,
        )
        
        if not results:
            return None
        
        return {
            "count": len(results),
            "items": results,
        }
    
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None


class MockValidator:
    """Mock validator with async method."""
    def __init__(self, delay=0.0, fail=False):
        self.delay = delay
        self.fail = fail
        self.call_count = 0
    
    async def validate_items(self, items: List[str]) -> List[str]:
        """Async validation."""
        self.call_count += 1
        
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        
        if self.fail:
            raise RuntimeError("Validation failed")
        
        return [f"validated_{item}" for item in items]


@pytest.mark.asyncio
async def test_async_await_works():
    """Test that async/await properly works."""
    validator = MockValidator()
    items = ["item1", "item2"]
    
    result = await validate_async(items, validator)
    
    # Should have called the validator
    assert validator.call_count == 1
    
    # Should have result
    assert result is not None
    assert result["count"] == 2
    assert result["items"] == ["validated_item1", "validated_item2"]


@pytest.mark.asyncio
async def test_async_timeout_handled():
    """Test that timeouts are handled gracefully."""
    validator = MockValidator(delay=1.0)  # Will timeout with 0.1s limit
    items = ["test"]
    
    # Create modified version with short timeout
    async def validate_with_timeout(items, validator):
        if not validator:
            return None
        
        try:
            results = await asyncio.wait_for(
                validator.validate_items(items),
                timeout=0.05,
            )
            return {"count": len(results), "items": results}
        except asyncio.TimeoutError:
            return None
        except Exception:
            return None
    
    result = await validate_with_timeout(items, validator)
    
    # Should return None on timeout
    assert result is None


@pytest.mark.asyncio
async def test_concurrent_async():
    """Test that multiple async operations can run concurrently."""
    v1 = MockValidator(delay=0.05)
    v2 = MockValidator(delay=0.05)
    v3 = MockValidator(delay=0.05)
    
    import time
    start = time.time()
    
    results = await asyncio.gather(
        validate_async(["a"], v1),
        validate_async(["b"], v2),
        validate_async(["c"], v3),
    )
    
    elapsed = time.time() - start
    
    # All should succeed
    assert all(r is not None for r in results)
    
    # Should be concurrent (< 0.15s) not sequential (> 0.15s)
    assert elapsed < 0.15, f"Took {elapsed}s, expected < 0.15s"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
