"""Structured logging context manager for deep_think_mcp.

Provides context variables for job_id, pass_num, provider, model, and timestamp.
Logs include these fields in JSON format for easy parsing and alerting.
"""

import contextvars
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

# Context variables
_job_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("job_id", default=None)
_pass_num: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("pass_num", default=None)
_provider: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("provider", default=None)
_model: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("model", default=None)
_perspective: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("perspective", default=None)


@contextmanager
def job_context(
    job_id: str,
    provider: str = "",
    model: str = "",
    pass_num: Optional[int] = None,
    perspective: str = "",
):
    """Context manager for structured job execution logging.
    
    Sets context variables that are automatically included in all log messages
    within this scope. Use with 'async with' for async functions or regular
    'with' for sync functions.
    
    Args:
        job_id: UUID of the job being executed
        provider: Provider name (ollama, anthropic, copilot)
        model: Model ID being used
        pass_num: Pass number (1-indexed) if applicable
        perspective: Perspective name for fan-out jobs
    """
    tokens = []
    
    try:
        tokens.append((_job_id, _job_id.set(job_id)))
        if provider:
            tokens.append((_provider, _provider.set(provider)))
        if model:
            tokens.append((_model, _model.set(model)))
        if pass_num is not None:
            tokens.append((_pass_num, _pass_num.set(pass_num)))
        if perspective:
            tokens.append((_perspective, _perspective.set(perspective)))
        yield
    finally:
        for var, token in reversed(tokens):
            try:
                var.reset(token)
            except Exception:
                pass


def get_context() -> dict:
    """Get current context as a dictionary for log enrichment."""
    ctx = {}
    if job_id := _job_id.get():
        ctx["job_id"] = job_id
    if pass_num := _pass_num.get():
        ctx["pass_num"] = pass_num
    if provider := _provider.get():
        ctx["provider"] = provider
    if model := _model.get():
        ctx["model"] = model
    if perspective := _perspective.get():
        ctx["perspective"] = perspective
    ctx["timestamp"] = datetime.now(timezone.utc).isoformat()
    return ctx


class StructuredFormatter(logging.Formatter):
    """Log formatter that includes context variables as JSON metadata.
    
    For ERROR and CRITICAL logs, includes the context dict.
    For other levels, includes context dict if any context variables are set.
    """
    
    def format(self, record: logging.LogRecord) -> str:
        ctx = get_context()
        
        # Build base message
        base_msg = super().format(record)
        
        # Add context as JSON for ERROR/CRITICAL or if context is non-empty
        if record.levelno >= logging.ERROR or ctx:
            ctx_str = json.dumps(ctx, default=str)
            return f"{base_msg} [ctx={ctx_str}]"
        return base_msg


def setup_structured_logging() -> None:
    """Configure logging with structured context formatter.
    
    Call once at application startup to enable structured logging for all
    loggers in the deep-think module.
    """
    formatter = StructuredFormatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    # Apply to root logger and deep-think namespace
    for logger_name in [None, "deep_think_mcp", "deep_think_mcp.engine",
                       "deep_think_mcp.worker", "deep_think_mcp.store"]:
        logger = logging.getLogger(logger_name)
        for handler in logger.handlers:
            handler.setFormatter(formatter)
        if logger_name is None:
            for handler in logging.root.handlers:
                handler.setFormatter(formatter)
