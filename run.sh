#!/bin/bash
# Startup wrapper for deep_think_mcp in stdio mode.
# Copy .env.example to .env and fill in your values — this script loads it automatically.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Parent of the repo dir goes on PYTHONPATH so `import deep_think_mcp` resolves
export PYTHONPATH="$(dirname "$SCRIPT_DIR"):${PYTHONPATH:-}"
export DEEP_THINK_TRANSPORT=stdio

# Load local .env if present (overrides defaults, never committed)
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
  set +a
fi

exec python3 -m deep_think_mcp
