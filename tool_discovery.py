"""
Phase 3 Part 2: Tool Discovery and Registry

Exposes available tools to the reasoning system for adaptive tool use.

Components:
- ToolCapability: Dataclass describing a tool's interface
- ToolRegistry: Central registry for all tools (built-in + custom)
- discover_available_tools(): List all available tools
- get_tool_schema(): Get schema for specific tool
- validate_tool_directive(): Validate tool invocation directive
- register_tool(): Register custom tools

Built-in tools:
- web_search: General web search
- code_search: GitHub code search
- document_fetch: Fetch specific documents
- nova_search: Great Library semantic search (if available)
"""

import inspect
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable, Protocol, runtime_checkable
from enum import Enum
import logging

try:
    from .defaults import (
        DEFAULT_CODE_SEARCH_TIMEOUT_SECS,
        DEFAULT_DOCUMENT_FETCH_TIMEOUT_SECS,
        DEFAULT_TOOL_TIMEOUT_SECS,
        DEFAULT_NOVA_SEARCH_TIMEOUT_SECS,
        DEFAULT_WEB_SEARCH_TIMEOUT_SECS,
    )
except ImportError:  # pragma: no cover - support direct module imports in tests
    from defaults import (
        DEFAULT_CODE_SEARCH_TIMEOUT_SECS,
        DEFAULT_DOCUMENT_FETCH_TIMEOUT_SECS,
        DEFAULT_TOOL_TIMEOUT_SECS,
        DEFAULT_NOVA_SEARCH_TIMEOUT_SECS,
        DEFAULT_WEB_SEARCH_TIMEOUT_SECS,
    )

logger = logging.getLogger(__name__)


# ============================================================================
# ENUMS
# ============================================================================

class ToolCategory(Enum):
    """Categories of tools for adaptive selection."""
    SEARCH = "search"
    FETCH = "fetch"
    ANALYSIS = "analysis"
    COMPUTE = "compute"


# ============================================================================
# DATACLASSES
# ============================================================================

@dataclass
class ToolCapability:
    """
    Describes a single tool's capabilities, schema, and constraints.
    
    Attributes:
        name: Unique identifier (e.g., "web_search")
        description: Human-readable description of what the tool does
        category: ToolCategory enum
        input_schema: JSON Schema for tool inputs
        output_schema: JSON Schema for tool outputs
        requires_auth: Whether tool needs authentication
        timeout_seconds: Maximum execution time
        cost_estimate: Budget units consumed per invocation
        examples: Example input/output pairs for documentation
        safety_notes: Important usage restrictions
    """
    
    name: str
    description: str
    category: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    requires_auth: bool = False
    timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECS
    cost_estimate: float = 1.0
    examples: List[Dict[str, Any]] = field(default_factory=list)
    safety_notes: Optional[str] = None
    
    def __post_init__(self):
        """Validate tool capability."""
        if not self.name:
            raise ValueError("Tool name cannot be empty")
        
        if not self.description:
            raise ValueError("Tool description cannot be empty")
        
        if self.timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be > 0, got {self.timeout_seconds}")
        
        if self.cost_estimate < 0.0:
            raise ValueError(f"cost_estimate must be >= 0, got {self.cost_estimate}")
        
        # Validate category is valid ToolCategory
        valid_categories = {e.value for e in ToolCategory}
        if self.category not in valid_categories:
            raise ValueError(f"category must be one of {valid_categories}, got {self.category}")
        
        # Validate schemas are dict-like
        if not isinstance(self.input_schema, dict):
            raise ValueError(f"input_schema must be dict, got {type(self.input_schema)}")
        
        if not isinstance(self.output_schema, dict):
            raise ValueError(f"output_schema must be dict, got {type(self.output_schema)}")


@dataclass
class ToolDirective:
    """
    A directive from reasoning system to invoke a tool.
    
    Attributes:
        tool_name: Name of tool to invoke
        arguments: Dict of arguments matching tool's input_schema
        priority: How important is this tool invocation? (0.0-1.0)
        max_results: For search tools, limit results
    """
    
    tool_name: str
    arguments: Dict[str, Any]
    priority: float = 0.5
    max_results: Optional[int] = None
    
    def __post_init__(self):
        """Validate directive."""
        if not self.tool_name:
            raise ValueError("tool_name cannot be empty")
        
        if not isinstance(self.arguments, dict):
            raise ValueError(f"arguments must be dict, got {type(self.arguments)}")
        
        if not 0.0 <= self.priority <= 1.0:
            raise ValueError(f"priority must be in [0, 1], got {self.priority}")
        
        if self.max_results is not None and self.max_results < 1:
            raise ValueError(f"max_results must be >= 1, got {self.max_results}")


@runtime_checkable
class ToolProtocol(Protocol):
    """Runtime contract for custom tool handlers."""

    def execute(self, query: str, timeout: int, **kwargs: Any) -> Any:
        """Execute the tool and return a result."""
        ...


# ============================================================================
# TOOL REGISTRY
# ============================================================================

class ToolRegistry:
    """
    Central registry for all available tools (built-in + custom).
    
    Manages:
    - Registration of tools with their schemas
    - Discovery of available tools
    - Schema lookup
    - Directive validation
    """
    
    def __init__(self):
        """Initialize with empty registry."""
        self._tools: Dict[str, ToolCapability] = {}
        self._handlers: Dict[str, Callable] = {}
        self._initialize_builtin_tools()
    
    def _initialize_builtin_tools(self):
        """Register all built-in tools."""
        
        # web_search: General web search
        self.register_tool(
            name="web_search",
            schema=ToolCapability(
                name="web_search",
                description="Search the web for current information and news",
                category=ToolCategory.SEARCH.value,
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query"
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results (default: 10)",
                            "minimum": 1,
                            "maximum": 50
                        }
                    },
                    "required": ["query"]
                },
                output_schema={
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "url": {"type": "string"},
                            "snippet": {"type": "string"}
                        }
                    }
                },
                requires_auth=False,
                timeout_seconds=DEFAULT_WEB_SEARCH_TIMEOUT_SECS,
                cost_estimate=1.0,
                examples=[
                    {
                        "input": {"query": "latest Python features 2024"},
                        "output": [
                            {
                                "title": "Python 3.13 Released",
                                "url": "https://example.com/python313",
                                "snippet": "New features in Python 3.13..."
                            }
                        ]
                    }
                ],
                safety_notes="Respects robots.txt and rate limits. Results may contain misinformation."
            )
        )
        
        # code_search: GitHub code search
        self.register_tool(
            name="code_search",
            schema=ToolCapability(
                name="code_search",
                description="Search GitHub repositories for code examples and implementations",
                category=ToolCategory.SEARCH.value,
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query (code patterns, function names, etc.)"
                        },
                        "language": {
                            "type": "string",
                            "description": "Programming language filter (e.g., 'python', 'javascript')",
                            "enum": ["python", "javascript", "typescript", "go", "rust", "java", "cpp", "csharp", "ruby", ""]
                        },
                        "repo": {
                            "type": "string",
                            "description": "Optional: specific repository (owner/repo format)"
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results (default: 10)",
                            "minimum": 1,
                            "maximum": 100
                        }
                    },
                    "required": ["query"]
                },
                output_schema={
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string"},
                            "language": {"type": "string"},
                            "code": {"type": "string"},
                            "url": {"type": "string"}
                        }
                    }
                },
                requires_auth=False,
                timeout_seconds=DEFAULT_CODE_SEARCH_TIMEOUT_SECS,
                cost_estimate=1.5,
                examples=[
                    {
                        "input": {"query": "async/await error handling", "language": "python"},
                        "output": [
                            {
                                "file": "async_handler.py",
                                "language": "python",
                                "code": "async def handler():\n    try:\n        await do_something()\n    except Exception as e:\n        handle_error(e)",
                                "url": "https://github.com/example/repo/blob/main/async_handler.py"
                            }
                        ]
                    }
                ],
                safety_notes="Searches local repository context in this runtime. No external API auth required."
            )
        )
        
        # document_fetch: Fetch specific documents
        self.register_tool(
            name="document_fetch",
            schema=ToolCapability(
                name="document_fetch",
                description="Fetch and parse specific documents from URLs",
                category=ToolCategory.FETCH.value,
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL of document to fetch"
                        },
                        "format": {
                            "type": "string",
                            "description": "Desired output format",
                            "enum": ["text", "markdown", "json", "html"]
                        }
                    },
                    "required": ["url"]
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "metadata": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "source": {"type": "string"},
                                "fetch_time": {"type": "string"}
                            }
                        }
                    }
                },
                requires_auth=False,
                timeout_seconds=DEFAULT_DOCUMENT_FETCH_TIMEOUT_SECS,
                cost_estimate=0.5,
                examples=[
                    {
                        "input": {"url": "https://example.com/article", "format": "markdown"},
                        "output": {
                            "content": "# Article Title\n\nContent here...",
                            "metadata": {
                                "title": "Article Title",
                                "source": "https://example.com/article",
                                "fetch_time": "2024-01-15T10:30:00Z"
                            }
                        }
                    }
                ],
                safety_notes="Respects robots.txt, user-agent rules. Timeout 10s for large documents."
            )
        )
        
        # nova_search: Great Library semantic search
        self.register_tool(
            name="nova_search",
            schema=ToolCapability(
                name="nova_search",
                description="Search the Great Library for semantically relevant content (requires Nova)",
                category=ToolCategory.SEARCH.value,
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural language search query"
                        },
                        "top": {
                            "type": "integer",
                            "description": "Number of results (default: 8)",
                            "minimum": 1,
                            "maximum": 20
                        },
                        "profile": {
                            "type": "string",
                            "description": "Retrieval profile",
                            "enum": ["auto", "operational", "research", "memory", "mixed"]
                        }
                    },
                    "required": ["query"]
                },
                output_schema={
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "source": {"type": "string"},
                            "relevance": {"type": "number"}
                        }
                    }
                },
                requires_auth=True,
                timeout_seconds=DEFAULT_NOVA_SEARCH_TIMEOUT_SECS,
                cost_estimate=2.0,
                examples=[
                    {
                        "input": {"query": "authentication mechanisms", "top": 5},
                        "output": [
                            {
                                "content": "JWT is a stateless authentication method...",
                                "source": "auth-guide.md",
                                "relevance": 0.95
                            }
                        ]
                    }
                ],
                safety_notes="Requires Nova service to be available. Slower than other search tools but more semantically relevant."
            )
        )
        
        logger.info(f"Initialized tool registry with {len(self._tools)} built-in tools")
    
    def register_tool(
        self,
        name: str,
        schema: ToolCapability,
        handler: Optional[Callable] = None
    ) -> None:
        """
        Register a tool with the registry.
        
        Args:
            name: Unique tool name
            schema: ToolCapability describing the tool
            handler: Optional callable to execute the tool
        
        Raises:
            ValueError: If tool name is already registered
        """
        if name in self._tools:
            raise ValueError(f"Tool '{name}' already registered")
        
        if schema.name != name:
            raise ValueError(f"Schema name '{schema.name}' doesn't match tool name '{name}'")

        if handler is not None:
            if inspect.isfunction(handler) or inspect.ismethod(handler):
                pass
            elif not isinstance(handler, ToolProtocol) or not callable(
                getattr(handler, "execute", None)
            ):
                raise ValueError(
                    "Tool handler must be a function/method or implement ToolProtocol.execute()"
                )
        
        self._tools[name] = schema
        if handler:
            self._handlers[name] = handler
        
        logger.debug(f"Registered tool: {name}")
    
    def discover_available_tools(self) -> List[ToolCapability]:
        """
        Discover all available tools.
        
        Returns:
            List of ToolCapability objects for all registered tools
        """
        return list(self._tools.values())
    
    def get_tool_schema(self, tool_name: str) -> Optional[ToolCapability]:
        """
        Get the schema for a specific tool.
        
        Args:
            tool_name: Name of tool
        
        Returns:
            ToolCapability if found, None otherwise
        """
        return self._tools.get(tool_name)
    
    def has_tool(self, tool_name: str) -> bool:
        """
        Check if a tool is registered.
        
        Args:
            tool_name: Name of tool
        
        Returns:
            True if tool is registered
        """
        return tool_name in self._tools
    
    def validate_tool_directive(self, directive: ToolDirective) -> tuple[bool, Optional[str]]:
        """
        Validate that a tool directive can be executed.
        
        Checks:
        - Tool exists
        - Arguments match schema
        - Required fields present
        
        Args:
            directive: ToolDirective to validate
        
        Returns:
            Tuple of (is_valid, error_message)
        """
        # Check tool exists
        if not self.has_tool(directive.tool_name):
            return False, f"Tool '{directive.tool_name}' not found"
        
        schema = self.get_tool_schema(directive.tool_name)
        if not schema:
            return False, f"Cannot retrieve schema for '{directive.tool_name}'"
        
        # Check required fields
        input_schema = schema.input_schema
        if "required" in input_schema:
            required_fields = set(input_schema["required"])
            provided_fields = set(directive.arguments.keys())
            missing = required_fields - provided_fields
            if missing:
                return False, f"Missing required arguments: {missing}"
        
        # Check that all provided arguments are in schema properties
        if "properties" in input_schema:
            valid_properties = set(input_schema["properties"].keys())
            provided_properties = set(directive.arguments.keys())
            invalid = provided_properties - valid_properties
            if invalid:
                return False, f"Invalid arguments: {invalid}"
        
        # Validate argument types (basic validation)
        if "properties" in input_schema:
            for arg_name, arg_value in directive.arguments.items():
                prop_schema = input_schema["properties"].get(arg_name, {})
                expected_type = prop_schema.get("type")
                
                if expected_type:
                    actual_type = type(arg_value).__name__
                    if expected_type == "string" and not isinstance(arg_value, str):
                        return False, f"Argument '{arg_name}' must be string, got {actual_type}"
                    elif expected_type == "integer" and not isinstance(arg_value, int):
                        return False, f"Argument '{arg_name}' must be integer, got {actual_type}"
                    elif expected_type == "number" and not isinstance(arg_value, (int, float)):
                        return False, f"Argument '{arg_name}' must be number, got {actual_type}"
                    elif expected_type == "boolean" and not isinstance(arg_value, bool):
                        return False, f"Argument '{arg_name}' must be boolean, got {actual_type}"
        
        return True, None
    
    def get_tool_handler(self, tool_name: str) -> Optional[Callable]:
        """
        Get the handler function for a tool.
        
        Args:
            tool_name: Name of tool
        
        Returns:
            Handler callable if registered, None otherwise
        """
        return self._handlers.get(tool_name)


# ============================================================================
# SINGLETON INSTANCE
# ============================================================================

_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    """
    Get or create the global tool registry.
    
    Returns:
        Singleton ToolRegistry instance
    """
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


def discover_available_tools() -> List[ToolCapability]:
    """
    Discover all available tools.
    
    Public API for discovering what tools are available to reasoning system.
    
    Returns:
        List of ToolCapability objects
    
    Example:
        >>> tools = discover_available_tools()
        >>> for tool in tools:
        ...     print(f"{tool.name}: {tool.description}")
    """
    registry = get_tool_registry()
    return registry.discover_available_tools()


def get_tool_schema(tool_name: str) -> Optional[ToolCapability]:
    """
    Get schema for a specific tool.
    
    Args:
        tool_name: Name of tool to retrieve
    
    Returns:
        ToolCapability if found, None otherwise
    
    Example:
        >>> schema = get_tool_schema("web_search")
        >>> print(schema.input_schema)
    """
    registry = get_tool_registry()
    return registry.get_tool_schema(tool_name)


def validate_tool_directive(directive: ToolDirective) -> tuple[bool, Optional[str]]:
    """
    Validate a tool invocation directive.
    
    Args:
        directive: ToolDirective to validate
    
    Returns:
        Tuple of (is_valid, error_message)
    
    Example:
        >>> directive = ToolDirective("web_search", {"query": "Python"})
        >>> is_valid, error = validate_tool_directive(directive)
        >>> if is_valid:
        ...     print("Ready to invoke tool")
    """
    registry = get_tool_registry()
    return registry.validate_tool_directive(directive)


def register_custom_tool(
    name: str,
    schema: ToolCapability,
    handler: Optional[Callable] = None
) -> None:
    """
    Register a custom tool at runtime.
    
    Args:
        name: Unique tool name
        schema: ToolCapability describing the tool
        handler: Optional callable to execute the tool
    
    Raises:
        ValueError: If tool name is already registered
    
    Example:
        >>> schema = ToolCapability(
        ...     name="custom_tool",
        ...     description="My custom tool",
        ...     category="compute",
        ...     input_schema={"type": "object"},
        ...     output_schema={"type": "object"}
        ... )
        >>> register_custom_tool("custom_tool", schema)
    """
    registry = get_tool_registry()
    registry.register_tool(name, schema, handler)
