"""Defensive validation for deep_think parameters.

Prevents crashes from corrupted slice objects and other invalid input types
that may be sent by FastMCP or other upstream systems.
"""

import logging

log = logging.getLogger(__name__)


class ValidationError(ValueError):
    """Raised when parameter validation fails."""
    pass


def validate_width(value) -> int:
    """Validate and normalize width parameter for fan-out reasoning.
    
    Args:
        value: The width value (should be an int, 1-6)
        
    Returns:
        int: Validated width, clamped to 1-6
        
    Raises:
        ValidationError: If value is a slice object or cannot be converted to int
    """
    # Reject slice objects (FastMCP bug indicator)
    if isinstance(value, slice):
        max_val = 6
        raise ValidationError(
            f"Parameter corrupted with slice object. This is a FastMCP bug. "
            f"Use integer value 1-{max_val} instead."
        )
    
    # Try to convert to int
    try:
        width = int(value)
    except (TypeError, ValueError) as e:
        raise ValidationError(
            f"width must be an integer (1-6), got {type(value).__name__}: {value!r}. "
            f"Error: {e}"
        )
    
    # Clamp to valid range
    width = max(1, min(width, 6))
    
    if width != int(value):
        log.warning(f"width parameter adjusted from {value} to {width}")
    
    return width


def validate_passes(value) -> int:
    """Validate and normalize passes parameter for multi-pass reasoning.
    
    Args:
        value: The passes value (should be an int, 1-6)
        
    Returns:
        int: Validated passes, clamped to 1-6
        
    Raises:
        ValidationError: If value is a slice object or cannot be converted to int
    """
    # Reject slice objects (FastMCP bug indicator)
    if isinstance(value, slice):
        max_val = 6
        raise ValidationError(
            f"Parameter corrupted with slice object. This is a FastMCP bug. "
            f"Use integer value 1-{max_val} instead."
        )
    
    # Try to convert to int
    try:
        passes = int(value)
    except (TypeError, ValueError) as e:
        raise ValidationError(
            f"passes must be an integer (1-6), got {type(value).__name__}: {value!r}. "
            f"Error: {e}"
        )
    
    # Clamp to valid range
    passes = max(1, min(passes, 6))
    
    if passes != int(value):
        log.warning(f"passes parameter adjusted from {value} to {passes}")
    
    return passes


def validate_height(value) -> int:
    """Validate and normalize height parameter for fan-out reasoning.
    
    Args:
        value: The height value (should be an int, 1-5)
        
    Returns:
        int: Validated height, clamped to 1-5
        
    Raises:
        ValidationError: If value is a slice object or cannot be converted to int
    """
    # Reject slice objects (FastMCP bug indicator)
    if isinstance(value, slice):
        max_val = 5
        raise ValidationError(
            f"Parameter corrupted with slice object. This is a FastMCP bug. "
            f"Use integer value 1-{max_val} instead."
        )
    
    # Try to convert to int
    try:
        height = int(value)
    except (TypeError, ValueError) as e:
        raise ValidationError(
            f"height must be an integer (1-5), got {type(value).__name__}: {value!r}. "
            f"Error: {e}"
        )
    
    # Clamp to valid range
    height = max(1, min(height, 5))
    
    if height != int(value):
        log.warning(f"height parameter adjusted from {value} to {height}")
    
    return height
