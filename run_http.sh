#!/bin/bash
# Run deep_think_mcp in HTTP/SSE mode for DAMA NovaMcpClient and other HTTP consumers.
# Configure DEEP_THINK_HOST and DEEP_THINK_PORT in .env (defaults: 0.0.0.0:8080).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load local .env if present
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
  set +a
fi

export PYTHONPATH="$(dirname "$SCRIPT_DIR"):${PYTHONPATH:-}"
export DEEP_THINK_TRANSPORT=streamable-http

exec python3 -m deep_think_mcp.server
