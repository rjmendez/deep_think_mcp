#!/bin/bash
# Startup wrapper for deep_think_mcp in stdio mode.
# Resolves GitHub Copilot OAuth token at launch (not stored in config files).
# Copy .env.example to .env and fill in your values — this script loads it automatically.

export GITHUB_COPILOT_OAUTH_TOKEN=$(gh auth token 2>/dev/null || echo "")
export PYTHONPATH="/Users/roberto.mendez/Carl:${PYTHONPATH}"
export DEEP_THINK_TRANSPORT=stdio

# Load local .env if present (overrides defaults, never committed)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
  set +a
fi

exec python3 -m deep_think_mcp
