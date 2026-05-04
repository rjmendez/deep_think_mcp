"""Claim verification subsystem for deep_think_mcp.

Provides synchronous and asynchronous claim verification using cloud (Anthropic)
or local (Ollama) LLM providers.
"""

from .provider import CloudProvider, LocalProvider, VerifyResult
from .queue import VerifyJob, VerifyJobQueue, VerifyWorker
from .config import load_config

__all__ = [
    "CloudProvider",
    "LocalProvider",
    "VerifyResult",
    "VerifyJob",
    "VerifyJobQueue",
    "VerifyWorker",
    "load_config",
]
