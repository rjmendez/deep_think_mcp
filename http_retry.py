"""HTTP error handling and retry logic for deep_think_mcp.

Differentiates between transient (timeout, 5xx) and permanent (4xx) errors.
Implements exponential backoff + jitter for retryable errors.
"""

import asyncio
import logging
import random
from typing import Optional

log = logging.getLogger(__name__)


class TransientError(Exception):
    """Retryable HTTP error (timeout, 5xx status)."""
    pass


class PermanentError(Exception):
    """Non-retryable HTTP error (4xx status)."""
    pass


async def call_with_retry(
    fn,
    max_attempts: int = 3,
    initial_backoff: float = 1.0,
    max_backoff: float = 32.0,
    job_id: str = "",
    pass_num: Optional[int] = None,
    provider: str = "",
    model: str = "",
) -> any:
    """Call an async function with exponential backoff retry on transient errors.
    
    Args:
        fn: Async callable that may raise TransientError or PermanentError
        max_attempts: Maximum number of attempts (default 3)
        initial_backoff: Initial backoff in seconds (default 1.0)
        max_backoff: Maximum backoff in seconds (default 32.0)
        job_id: Job ID for logging context
        pass_num: Pass number for logging context
        provider: Provider name for logging context
        model: Model ID for logging context
    
    Returns:
        Result of fn() on success
        
    Raises:
        TransientError if all retries exhausted
        PermanentError immediately without retry
    """
    backoff = initial_backoff
    last_error = None
    
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except PermanentError as e:
            # Permanent errors fail immediately — no retry
            log.error(
                "Permanent HTTP error on %s (job=%s pass=%s provider=%s model=%s): %s",
                fn.__name__, job_id or "?", pass_num or "?", provider or "?", model or "?",
                str(e),
            )
            raise
        except TransientError as e:
            last_error = e
            if attempt >= max_attempts:
                # Out of retries
                log.error(
                    "Transient error exhausted all %d attempts (job=%s pass=%s provider=%s model=%s): %s",
                    max_attempts, job_id or "?", pass_num or "?", provider or "?", model or "?",
                    str(e),
                )
                raise
            
            # Wait before retry
            jitter = random.uniform(0, backoff * 0.1)
            wait = backoff + jitter
            log.warning(
                "Transient error on attempt %d/%d, retrying in %.2fs (job=%s pass=%s provider=%s model=%s): %s",
                attempt, max_attempts, wait, job_id or "?", pass_num or "?",
                provider or "?", model or "?", str(e),
            )
            await asyncio.sleep(wait)
            
            # Exponential backoff with cap
            backoff = min(backoff * 2, max_backoff)
    
    # Should not reach here, but fail safely
    if last_error:
        raise last_error
    raise RuntimeError("Unexpected retry exhaustion")
