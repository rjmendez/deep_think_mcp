"""Defensive validation for deep_think parameters.

Prevents crashes from corrupted slice objects and other invalid input types
that may be sent by FastMCP or other upstream systems.
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

MAX_QUESTION_LENGTH = 10000


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
    if value is None:
        return 1

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
    if value is None:
        return 3

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
    if value is None:
        return 1

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


def validate_question(value, *, max_length: int = MAX_QUESTION_LENGTH) -> str:
    """Validate and normalize question-like input fields."""
    if not isinstance(value, str):
        raise ValidationError(f"question must be a string, got {type(value).__name__}")

    question = value.strip()
    if not question:
        raise ValidationError("question must not be empty")
    if len(question) > max_length:
        raise ValidationError(f"question exceeds maximum length ({max_length} characters)")
    return question


def validate_adaptive_config(value) -> Optional[dict]:
    """Validate adaptive fan-out tool-loop config shape and scalar types."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError("adaptive_config must be an object")

    allowed_keys = {
        "max_tool_calls_global",
        "max_tool_calls_per_perspective",
        "tool_timeout",
    }
    unknown_keys = sorted(set(value.keys()) - allowed_keys)
    if unknown_keys:
        raise ValidationError(
            f"adaptive_config has unsupported keys: {', '.join(unknown_keys)}"
        )

    validated = {}
    for key, raw in value.items():
        if isinstance(raw, bool):
            raise ValidationError(f"adaptive_config.{key} must be an integer")
        try:
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"adaptive_config.{key} must be an integer: {exc}")
        if key == "tool_timeout" and parsed < 1:
            raise ValidationError("adaptive_config.tool_timeout must be >= 1")
        if key != "tool_timeout" and parsed < 0:
            raise ValidationError(f"adaptive_config.{key} must be >= 0")
        validated[key] = parsed

    return validated


def validate_web_domain_whitelist(value) -> list[str]:
    """Validate optional list of domains for web-search/domain filtering."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError("web_domain_whitelist must be an array of domains")

    normalized: list[str] = []
    for idx, domain in enumerate(value):
        if not isinstance(domain, str):
            raise ValidationError(
                f"web_domain_whitelist[{idx}] must be a string domain"
            )
        cleaned = domain.strip().lower()
        if not cleaned:
            raise ValidationError(
                f"web_domain_whitelist[{idx}] must not be empty"
            )
        if "://" in cleaned or "/" in cleaned or ":" in cleaned or " " in cleaned:
            raise ValidationError(
                f"web_domain_whitelist[{idx}] must be a bare domain (no scheme/path/port)"
            )
        normalized.append(cleaned)

    # Preserve order while deduplicating.
    deduped: list[str] = []
    seen = set()
    for domain in normalized:
        if domain not in seen:
            deduped.append(domain)
            seen.add(domain)
    return deduped
