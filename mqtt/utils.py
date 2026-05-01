"""MQTT utility functions and helpers.

Provides common functions used across MQTT modules including retry logic
and error handling utilities.
"""

import asyncio
import logging
from typing import Callable, Optional, TypeVar, Any

log = logging.getLogger(__name__)

T = TypeVar('T')


async def retry_with_backoff(
    func: Callable[..., Any],
    max_attempts: int = 3,
    initial_delay_sec: float = 1.0,
    max_delay_sec: float = 30.0,
    backoff_multiplier: float = 2.0,
) -> Any:
    """Retry a function with exponential backoff.
    
    Args:
        func: Async function to retry
        max_attempts: Maximum number of attempts
        initial_delay_sec: Initial delay between retries
        max_delay_sec: Maximum delay between retries
        backoff_multiplier: Multiplier for exponential backoff
        
    Returns:
        Result of the function if successful
        
    Raises:
        The last exception if all attempts fail
    """
    delay = initial_delay_sec
    last_exception = None
    
    for attempt in range(max_attempts):
        try:
            return await func()
        except Exception as e:
            last_exception = e
            if attempt < max_attempts - 1:
                log.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay:.1f}s...")
                await asyncio.sleep(delay)
                delay = min(delay * backoff_multiplier, max_delay_sec)
    
    raise last_exception


def parse_device_id_from_topic(topic: str) -> Optional[str]:
    """Extract device ID from MQTT topic path.
    
    Example:
        "dama/colony/device_1/telemetry" -> "device_1"
    """
    parts = topic.split('/')
    if len(parts) >= 3 and parts[0] == "dama" and parts[1] == "colony":
        return parts[2]
    return None
