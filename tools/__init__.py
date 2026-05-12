"""Tool wrappers for deep_think tool invoker.

Provides:
- invoke_web_search: Search the web
- invoke_code_search: Search GitHub code
- invoke_nova_verify: Verify claims with Great Library
- invoke_document_fetch: Fetch and summarize documents
"""

from .web_search import invoke_web_search
from .code_search import invoke_code_search
from .nova_verify import invoke_nova_verify
from .document_fetch import invoke_document_fetch

__all__ = [
    "invoke_web_search",
    "invoke_code_search",
    "invoke_nova_verify",
    "invoke_document_fetch",
]
