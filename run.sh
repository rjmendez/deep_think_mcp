#!/bin/bash
# Startup wrapper for deep_think_mcp in stdio mode.
# Resolves GitHub Copilot OAuth token at launch (not stored in config files).
# Copy this to run.sh and fill in your values, or use .env.example as a reference.

export GITHUB_COPILOT_OAUTH_TOKEN=$(gh auth token 2>/dev/null || echo "")
export PYTHONPATH="/Users/roberto.mendez/Carl:${PYTHONPATH}"
export DEEP_THINK_TRANSPORT=stdio

# Mixed-provider setup: local Ollama for cheap passes, Copilot for synthesis.
# Set OLLAMA_BASE_URL to wherever your Ollama instance is running.
export OLLAMA_BASE_URL=http://100.73.200.19:11434
export DEEP_THINK_LIGHT_PROVIDER=ollama
export DEEP_THINK_MEDIUM_PROVIDER=ollama
export DEEP_THINK_HEAVY_PROVIDER=copilot

# Model tiers are auto-assigned by discovery at startup — override here only if needed.
# export DEEP_THINK_MODEL_LIGHT=phi4-mini:latest
# export DEEP_THINK_MODEL_MEDIUM=llama3.1:8b

# Copilot tiers (only heavy is used above, but set all in case you switch providers).
export DEEP_THINK_COPILOT_LIGHT=claude-sonnet-4.5
export DEEP_THINK_COPILOT_MEDIUM=claude-sonnet-4.6
export DEEP_THINK_COPILOT_HEAVY=claude-opus-4.7

exec python3 -m deep_think_mcp
