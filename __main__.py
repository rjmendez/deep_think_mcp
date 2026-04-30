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

from deep_think_mcp.server import mcp  # noqa: E402

transport = os.getenv("DEEP_THINK_TRANSPORT", "streamable-http")
host = os.getenv("DEEP_THINK_HOST", "0.0.0.0")
port = int(os.getenv("DEEP_THINK_PORT", "8002"))

if transport == "stdio":
    mcp.run(transport="stdio")
else:
    mcp.run(transport="streamable-http", host=host, port=port)
