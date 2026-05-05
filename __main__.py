"""Entry point: python -m deep_think_mcp

Environment variables:
  DEEP_THINK_TRANSPORT   "streamable-http" (default) or "stdio"
  DEEP_THINK_HOST        Host to bind (default: 0.0.0.0)
  DEEP_THINK_PORT        Port to bind (default: 8002)
  LOG_LEVEL              Logging level (default: INFO)
"""

import logging
import os

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

# Monkey-patch FastMCP's schema builder to handle numeric defaults correctly
# (Tier 1 fix for slice(None, 3, None) bug in fastmcp 3.2.0)
import inspect
import json
from typing import get_origin, get_args

def _patch_fastmcp_schema_builder():
    """Prevent FastMCP from converting numeric defaults to slice objects."""
    try:
        from fastmcp.server.server import MCPServer
        original_get_schema = MCPServer.get_schema if hasattr(MCPServer, 'get_schema') else None
        
        if original_get_schema:
            def patched_get_schema(self):
                # Call original schema generation
                schema = original_get_schema(self)
                
                # Post-process: fix any slice objects in tool parameters
                if isinstance(schema, dict) and 'tools' in schema:
                    for tool in schema['tools']:
                        if 'inputSchema' in tool:
                            props = tool['inputSchema'].get('properties', {})
                            for prop_name, prop_spec in props.items():
                                # Remove any corrupted 'default' values that became slice objects
                                if 'default' in prop_spec and isinstance(prop_spec['default'], slice):
                                    del prop_spec['default']
                
                return schema
            
            MCPServer.get_schema = patched_get_schema
    except Exception as e:
        logging.warning(f"Could not patch FastMCP schema builder: {e}")

_patch_fastmcp_schema_builder()

from deep_think_mcp.server import mcp  # noqa: E402

transport = os.getenv("DEEP_THINK_TRANSPORT", "streamable-http")
host = os.getenv("DEEP_THINK_HOST", "0.0.0.0")
port = int(os.getenv("DEEP_THINK_PORT", "8002"))

if transport == "stdio":
    mcp.run(transport="stdio")
else:
    mcp.run(transport="streamable-http", host=host, port=port)
