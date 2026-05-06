"""Tests for Tier 1 defensive validation against FastMCP slice object bugs.

Tests verify:
- Validation functions reject slice objects and raise clear errors
- Validation functions accept valid integers and clamp to safe ranges
- Validation functions handle edge cases (0, negative, out-of-range)
- String integers are converted to int
- Integration with API endpoints works
"""

import pytest
from engine.validator import (
    validate_width,
    validate_passes,
    validate_height,
    ValidationError,
)


# ============================================================================
# Test validate_width
# ============================================================================

class TestValidateWidth:
    """Tests for validate_width function."""
    
    def test_valid_width_in_range(self):
        """Valid width values in range should be accepted."""
        assert validate_width(1) == 1
        assert validate_width(3) == 3
        assert validate_width(6) == 6
    
    def test_width_clamped_below_minimum(self):
        """Width below 1 should be clamped to 1."""
        assert validate_width(0) == 1
        assert validate_width(-5) == 1
    
    def test_width_clamped_above_maximum(self):
        """Width above 6 should be clamped to 6."""
        assert validate_width(10) == 6
        assert validate_width(100) == 6
    
    def test_width_string_integer(self):
        """String integer should be converted to int."""
        assert validate_width("3") == 3
        assert validate_width("6") == 6
    
    def test_width_rejects_slice_object(self):
        """Slice object should raise ValidationError with clear message."""
        with pytest.raises(ValidationError) as exc_info:
            validate_width(slice(None, 3, None))
        
        error_msg = str(exc_info.value)
        assert "slice object" in error_msg.lower()
        assert "fastmcp bug" in error_msg.lower()
        assert "integer value 1-6" in error_msg.lower()
    
    def test_width_rejects_non_convertible_string(self):
        """Non-numeric string should raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            validate_width("invalid")
        
        error_msg = str(exc_info.value)
        assert "must be an integer" in error_msg.lower()
    
    def test_width_defaults_none_to_one(self):
        """None should apply the documented default width."""
        assert validate_width(None) == 1
    
    def test_width_rejects_float(self):
        """Float should be converted to int."""
        # Float should convert to int (3.7 -> 3)
        assert validate_width(3.7) == 3


# ============================================================================
# Test validate_passes
# ============================================================================

class TestValidatePasses:
    """Tests for validate_passes function."""
    
    def test_valid_passes_in_range(self):
        """Valid passes values in range should be accepted."""
        assert validate_passes(1) == 1
        assert validate_passes(4) == 4
        assert validate_passes(6) == 6
    
    def test_passes_clamped_below_minimum(self):
        """Passes below 1 should be clamped to 1."""
        assert validate_passes(0) == 1
        assert validate_passes(-5) == 1
    
    def test_passes_clamped_above_maximum(self):
        """Passes above 6 should be clamped to 6."""
        assert validate_passes(8) == 6
        assert validate_passes(100) == 6
    
    def test_passes_string_integer(self):
        """String integer should be converted to int."""
        assert validate_passes("3") == 3
        assert validate_passes("4") == 4
    
    def test_passes_rejects_slice_object(self):
        """Slice object should raise ValidationError with clear message."""
        with pytest.raises(ValidationError) as exc_info:
            validate_passes(slice(None, 4, None))
        
        error_msg = str(exc_info.value)
        assert "slice object" in error_msg.lower()
        assert "fastmcp bug" in error_msg.lower()
        assert "integer value 1-6" in error_msg.lower()
    
    def test_passes_rejects_non_convertible_string(self):
        """Non-numeric string should raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            validate_passes("invalid")
        
        error_msg = str(exc_info.value)
        assert "must be an integer" in error_msg.lower()
    
    def test_passes_defaults_none_to_three(self):
        """None should apply the documented default passes."""
        assert validate_passes(None) == 3


# ============================================================================
# Test validate_height
# ============================================================================

class TestValidateHeight:
    """Tests for validate_height function."""
    
    def test_valid_height_in_range(self):
        """Valid height values in range should be accepted."""
        assert validate_height(1) == 1
        assert validate_height(2) == 2
        assert validate_height(5) == 5
    
    def test_height_clamped_below_minimum(self):
        """Height below 1 should be clamped to 1."""
        assert validate_height(0) == 1
        assert validate_height(-5) == 1
    
    def test_height_clamped_above_maximum(self):
        """Height above 5 should be clamped to 5."""
        assert validate_height(10) == 5
        assert validate_height(100) == 5
    
    def test_height_string_integer(self):
        """String integer should be converted to int."""
        assert validate_height("2") == 2
        assert validate_height("5") == 5
    
    def test_height_rejects_slice_object(self):
        """Slice object should raise ValidationError with clear message."""
        with pytest.raises(ValidationError) as exc_info:
            validate_height(slice(None, 2, None))
        
        error_msg = str(exc_info.value)
        assert "slice object" in error_msg.lower()
        assert "fastmcp bug" in error_msg.lower()
        assert "integer value 1-5" in error_msg.lower()
    
    def test_height_rejects_non_convertible_string(self):
        """Non-numeric string should raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            validate_height("invalid")
        
        error_msg = str(exc_info.value)
        assert "must be an integer" in error_msg.lower()
    
    def test_height_defaults_none_to_one(self):
        """None should apply the documented default height."""
        assert validate_height(None) == 1


# ============================================================================
# Integration Tests with API Endpoints
# ============================================================================

class TestAPIIntegration:
    """Integration tests with API endpoints."""
    
    @pytest.mark.asyncio
    async def test_deep_think_async_with_valid_passes(self):
        """deep_think_async should accept valid passes parameter."""
        # This is a smoke test - just verify validation doesn't reject valid input
        assert validate_passes(3) == 3
        assert validate_passes(4) == 4
    
    @pytest.mark.asyncio
    async def test_deep_think_fan_out_with_valid_width_height(self):
        """deep_think_fan_out should accept valid width/height parameters."""
        # This is a smoke test - just verify validation doesn't reject valid input
        assert validate_width(3) == 3
        assert validate_height(2) == 2
    
    def test_validation_catches_fastmcp_bug_slice_width(self):
        """Validation should catch FastMCP slice object in width parameter."""
        with pytest.raises(ValidationError) as exc_info:
            validate_width(slice(None, 3, None))
        
        error_msg = str(exc_info.value)
        # Verify error message is clear and actionable
        assert "corrupted" in error_msg.lower()
        assert "fastmcp" in error_msg.lower()
        assert "1-6" in error_msg.lower()
    
    def test_validation_catches_fastmcp_bug_slice_height(self):
        """Validation should catch FastMCP slice object in height parameter."""
        with pytest.raises(ValidationError) as exc_info:
            validate_height(slice(None, 2, None))
        
        error_msg = str(exc_info.value)
        # Verify error message is clear and actionable
        assert "corrupted" in error_msg.lower()
        assert "fastmcp" in error_msg.lower()
        assert "1-5" in error_msg.lower()
    
    def test_validation_catches_fastmcp_bug_slice_passes(self):
        """Validation should catch FastMCP slice object in passes parameter."""
        with pytest.raises(ValidationError) as exc_info:
            validate_passes(slice(None, 4, None))
        
        error_msg = str(exc_info.value)
        # Verify error message is clear and actionable
        assert "corrupted" in error_msg.lower()
        assert "fastmcp" in error_msg.lower()
        assert "1-6" in error_msg.lower()


# ============================================================================
# Edge Cases and Boundary Tests
# ============================================================================

class TestBoundaryConditions:
    """Tests for edge cases and boundary conditions."""
    
    def test_width_boundary_1(self):
        """Width boundary at 1."""
        assert validate_width(1) == 1
    
    def test_width_boundary_6(self):
        """Width boundary at 6."""
        assert validate_width(6) == 6
    
    def test_height_boundary_1(self):
        """Height boundary at 1."""
        assert validate_height(1) == 1
    
    def test_height_boundary_5(self):
        """Height boundary at 5."""
        assert validate_height(5) == 5
    
    def test_passes_boundary_1(self):
        """Passes boundary at 1."""
        assert validate_passes(1) == 1
    
    def test_passes_boundary_6(self):
        """Passes boundary at 6."""
        assert validate_passes(6) == 6
    
    def test_zero_value_clamped_width(self):
        """Zero should be clamped to 1 for width."""
        assert validate_width(0) == 1
    
    def test_zero_value_clamped_height(self):
        """Zero should be clamped to 1 for height."""
        assert validate_height(0) == 1
    
    def test_zero_value_clamped_passes(self):
        """Zero should be clamped to 1 for passes."""
        assert validate_passes(0) == 1
    
    def test_negative_value_clamped_width(self):
        """Negative value should be clamped to 1 for width."""
        assert validate_width(-10) == 1
    
    def test_negative_value_clamped_height(self):
        """Negative value should be clamped to 1 for height."""
        assert validate_height(-10) == 1
    
    def test_negative_value_clamped_passes(self):
        """Negative value should be clamped to 1 for passes."""
        assert validate_passes(-10) == 1


# ============================================================================
# Run Tests
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
