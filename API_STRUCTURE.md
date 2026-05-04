# API Module Structure

This document describes the refactored API layer for the deep-think MCP server.

## Overview

The original `server.py` (1,911 lines) has been refactored into modular API routes with clear separation of concerns.

### Directory Structure

```
deep_think_mcp/
├── server.py                    # Core app + lifespan (254 lines)
├── api/
│   ├── __init__.py             # Route registration function (39 lines)
│   ├── health.py               # Health check endpoints (112 lines)
│   ├── reasoning.py            # Core reasoning endpoints (491 lines)
│   ├── verify.py               # Claim verification endpoints (205 lines)
│   ├── self_improvement.py      # Self-improvement pipeline (537 lines)
│   ├── mcp.py                  # MCP info & capabilities (304 lines)
│   └── mqtt.py                 # MQTT health & metrics (48 lines)
```

## Module Descriptions

### server.py (254 lines)
**Purpose:** Core FastMCP application and lifespan management

**Responsibilities:**
- Create FastMCP application instance
- Lifespan context manager for startup/shutdown
- Initialize core infrastructure (store, worker, MQTT, verification, planning)
- Register all modular routes via `api.register_routes(mcp)`
- Main entry point

**No route definitions.** All routes are defined in api/ modules.

### api/__init__.py (39 lines)
**Purpose:** Central route registration

**Exports:**
- `register_routes(mcp)` - Registers all API routes with the FastMCP app

### api/health.py (112 lines)
**Purpose:** System health and diagnostics endpoints

**Routes:**
- `GET /health` - Health check with queue metrics
- `GET /health/hints` - Health with actionable recommendations

### api/mqtt.py (48 lines)
**Purpose:** MQTT adapter monitoring

**Routes:**
- `mqtt_health()` - MQTT engine health status
- `mqtt_metrics()` - Detailed MQTT metrics

### api/reasoning.py (491 lines)
**Purpose:** Core deep-think reasoning capabilities

**Routes:**
- `deep_think_async()` - Queue multi-pass reasoning job
- `get_thinking_result()` - Poll job results
- `discover_models()` - Discover and benchmark available models
- `deep_think_fan_out()` - Queue perspective-based reasoning
- `list_thinking_jobs()` - List jobs by status
- `deep_think_creative()` - Queue creative reasoning with dynamic temperature
- `get_creative_metrics()` - Creativity metrics for trend analysis

### api/verify.py (205 lines)
**Purpose:** Claim verification (synchronous and asynchronous)

**Routes:**
- `POST /verify` - Synchronous claim verification
- `POST /verify-async` - Queue asynchronous verification
- `GET /verify-status/{job_id}` - Get verification job status

### api/mcp.py (304 lines)
**Purpose:** MCP metadata, capabilities, and help

**Routes:**
- `GET /capabilities` - List available reasoning capabilities
- `POST /suggest` - Smart request routing based on query complexity
- `GET /mcp/help/{command}` - Interactive help for commands

### api/self_improvement.py (537 lines)
**Purpose:** Self-improvement plan generation, approval, implementation, and deployment

**Routes:**
- `POST /self-improvement/implement` - Orchestrate code implementation
- `GET /self-improvement/status` - Get implementation status
- `generate_self_improvement_plan()` - Generate ranked improvement plans
- `get_pending_improvement_plans()` - List pending plans
- `approve_improvement_plan()` - Approve a plan
- `POST /self-improvement/deploy` - Deploy validated code with canary rollout
- `POST /self-improvement/validate` - Validate implementation

## Route Registration Pattern

Each api module follows a standard pattern:

```python
def register(mcp):
    """Register module routes."""
    
    @mcp.tool()
    async def my_tool(...) -> dict:
        """Tool docstring..."""
        # Implementation
    
    @mcp.custom_route("/my/path", methods=["GET"])
    async def my_route(request: Request):
        """Route docstring..."""
        # Implementation
```

Routes are registered by calling:
```python
from . import api
api.register_routes(mcp)
```

## Key Design Decisions

1. **Modular Organization**: Each module handles a logical group of related endpoints
2. **Consistent Pattern**: All modules follow the same `register(mcp)` function signature
3. **No Circular Imports**: Modules only import from parent packages and standard library
4. **Shared Lifespan**: Global state (planning_engine, verify_queue, etc.) managed in server.py
5. **Reduced server.py**: Core app logic separated from route definitions

## Statistics

| Metric | Value |
|--------|-------|
| Original server.py | 1,911 lines |
| Refactored server.py | 254 lines |
| Total api/ modules | 1,736 lines |
| Reduction | 86.7% in main file |
| Largest module | self_improvement.py (537 lines) |
| Smallest module | mqtt.py (48 lines) |
| Module count | 7 (including __init__) |
| Total routes | 24 |

## Testing

All 214 tests pass with the refactored structure:
```bash
pytest tests/ -k "not test_verify_claim_real" --tb=short
# 214 passed in 4.74s
```

## Migration Notes

- No functional changes - all routes work identically
- All imports work the same from external clients
- No changes to request/response contracts
- Backward compatible with existing code

## Future Improvements

- Add api/errors.py for shared error handling utilities
- Add api/validation.py for input validation layer
- Add comprehensive API documentation
- Add request/response schema validation
- Consider breaking self_improvement.py into two modules (planning and deployment)
