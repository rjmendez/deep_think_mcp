# MQTT Local-Only LLM Enforcement

## Overview

This document describes the security hardening implementation for deep_think_mcp MQTT colony operations. The system now enforces local-only LLM usage (Ollama exclusively) for all MQTT-related deep_think operations, preventing accidental or malicious data leakage to cloud providers.

## Architecture

### Security Model

**Goal**: Prevent sensitive device telemetry from MQTT devices (ants, phones) from being sent to cloud LLM providers.

**Enforcement Levels**:
1. **Standard** (`DEEP_THINK_FORCE_LOCAL=1` default): Local-only for MQTT operations, graceful fallback on failure
2. **Strict** (`OLLAMA_ONLY_MODE=1`): Hard failure if Ollama unavailable or cloud provider attempted

### Core Components

#### 1. Security Functions (engine/provider.py)

```python
class SecurityError(Exception):
    """Raised when security policy is violated."""

def _validate_provider_is_local(provider: str, force_local: bool) -> None:
    """Validate provider is Ollama-only when force_local=True.
    
    Blocks: anthropic, copilot, azure, openai
    Allows: ollama
    """

async def _check_ollama_available(base_url: str = "") -> bool:
    """Check if Ollama is reachable at startup with at least 1 model."""

async def _validate_and_enforce_local_models(
    cfg: ProviderConfig,
    force_local: bool,
    device_id: str = "",
) -> None:
    """Enforce local-only policy for MQTT operations.
    
    Actions:
    - Sets data_policy="local"
    - Validates all tiers (light/medium/heavy) route to Ollama
    - Checks Ollama availability
    - Logs enforcement action with device_id
    """
```

#### 2. Function Signatures (engine/orchestrator.py)

`deep_think_passes()` now accepts:
```python
force_local_models: bool = False  # Enforce local-only
device_id: str = ""              # For logging (e.g., "ant_001")
```

`run_fan_out()` now accepts:
```python
force_local_models: bool = False  # Enforce local-only
device_id: str = ""              # For logging (e.g., "ant_001")
```

#### 3. Worker Integration (worker.py)

The job queue handler automatically detects MQTT operations:
```python
device_id = provider_config.pop("device_id", "")
force_local_models = provider_config.pop("force_local_models", False)

# Auto-enable for MQTT
if device_id or force_local_models:
    force_local_models = True
    log.info(f"[MQTT] Detected MQTT job (device_id={device_id}), enabling local-only models")
```

## Configuration

### Environment Variables

#### `DEEP_THINK_FORCE_LOCAL`
- **Default**: `"1"` (true)
- **Effect**: Force local-only models for MQTT operations
- **Set to `"0"`**: Allow cloud providers (not recommended for MQTT)
- **Cannot be overridden**: Security flag in production

#### `OLLAMA_ONLY_MODE`
- **Default**: `"0"` (false)
- **Effect**: Strict enforcement mode
- **Set to `"1"`**: Hard-fail on:
  - Ollama unavailable at startup
  - Any cloud provider attempt
  - Implicitly sets `DEEP_THINK_FORCE_LOCAL=1`
- **Use case**: Production MQTT deployments

### .env.example Updates

```bash
# MQTT LOCAL-ONLY LLM ENFORCEMENT (Security hardening)

# Force local-only models for MQTT operations (prevent cloud provider leakage)
# "1" = enforced (default for MQTT jobs); "0" = allow cloud providers
# DEEP_THINK_FORCE_LOCAL=1

# Strictest enforcement mode — fail hard on any cloud provider attempt
# "1" = OLLAMA_ONLY, no fallback to cloud; "0" = graceful degradation
# Implies DEEP_THINK_FORCE_LOCAL=1
# OLLAMA_ONLY_MODE=0
```

## Usage

### For MQTT Operations

#### Via provider_config JSON:
```json
{
  "device_id": "ant_001",
  "force_local_models": true,
  "task_class": "investigation"
}
```

#### Via direct call:
```python
result = await engine.deep_think_passes(
    question="Analyze this telemetry: ...",
    force_local_models=True,
    device_id="ant_001",
)
```

#### Via fan-out:
```python
result = await engine.run_fan_out(
    question="Multi-perspective analysis of ...",
    force_local_models=True,
    device_id="ant_001",
    width=3,
    height=2,
)
```

### Logging Output

**Standard operation**:
```
[MQTT] Detected MQTT job (device_id=ant_001), enabling local-only models
[MQTT] Local-only enforcement active for ant_001
[MQTT] Ollama validated: 3 models available at http://localhost:11434
[MQTT] Running local-only deep_think for device ant_001
```

**Cloud provider blocked**:
```
[SECURITY] Cloud provider 'copilot' blocked in local-only mode. 
force_local_models=True requires Ollama-only. Set DEEP_THINK_FORCE_LOCAL=0 to allow cloud providers.
SecurityError: Cloud provider 'copilot' blocked in local-only mode.
```

**Strict mode failure**:
```
[MQTT] Ollama unavailable at http://localhost:11434: Connection refused
[SECURITY] [MQTT] Ollama unavailable — failing hard (OLLAMA_ONLY_MODE=1)
SecurityError: [MQTT] Ollama unavailable
```

## Security Guarantees

### Guarantee 1: Provider Routing Validation
✅ **Before** any deep_think call with `force_local_models=True`:
- All tiers (light, medium, heavy) are validated to route to Ollama
- Any cloud provider in the routing raises `SecurityError` immediately

### Guarantee 2: Ollama Availability Check
✅ **At startup** when `force_local_models=True`:
- Ollama endpoint is queried: `GET /api/tags`
- Must return at least 1 model
- Failure mode depends on `OLLAMA_ONLY_MODE`:
  - `OLLAMA_ONLY_MODE=0` (default): Log warning, attempt graceful recovery
  - `OLLAMA_ONLY_MODE=1` (strict): Raise `SecurityError`, fail hard

### Guarantee 3: Data Policy Enforcement
✅ **Always** when `force_local_models=True`:
- `cfg.data_policy = "local"` is set
- All nested deep_think_passes calls inherit this policy
- Fan-out perspectives also enforce local-only

### Guarantee 4: Device-Aware Logging
✅ **All logs** include `device_id` tag:
- `[MQTT] Running local-only deep_think for device {device_id}`
- Enables audit trails and debugging for specific MQTT devices

## Testing

### Unit Tests

Located in `test_mqtt_local_enforcement.py`:

```bash
# Run provider validation tests
pytest test_mqtt_local_enforcement.py::TestProviderValidation -v

# Run Ollama availability check tests
pytest test_mqtt_local_enforcement.py::TestOllamaAvailabilityCheck -v

# Run environment variable override tests
pytest test_mqtt_local_enforcement.py::TestEnvironmentVariableOverrides -v
```

### Manual Verification

```python
from deep_think_mcp.engine import (
    SecurityError,
    _validate_provider_is_local,
)

# Test 1: Block anthropic
_validate_provider_is_local("anthropic", force_local=True)  # Raises SecurityError

# Test 2: Allow ollama
_validate_provider_is_local("ollama", force_local=True)  # OK

# Test 3: Allow cloud when force_local=False
_validate_provider_is_local("anthropic", force_local=False)  # OK
```

## Implementation Details

### Changes Made

#### 1. engine/provider.py (~150 lines)
- Added `SecurityError` exception class
- Added `_validate_provider_is_local()` function
- Added `_check_ollama_available()` function
- Added `_validate_and_enforce_local_models()` function

#### 2. engine/orchestrator.py (~30 lines)
- Updated `deep_think_passes()` signature: +2 parameters
- Added environment variable checks for `DEEP_THINK_FORCE_LOCAL` and `OLLAMA_ONLY_MODE`
- Added enforcement call at function entry
- Updated `run_fan_out()` signature: +2 parameters
- Added enforcement for fan-out and all nested perspectives

#### 3. engine/__init__.py (~10 lines)
- Exported `SecurityError` and enforcement functions
- Updated docstring

#### 4. worker.py (~15 lines)
- Extract `device_id` and `force_local_models` from job config
- Auto-enable local-only for MQTT jobs
- Pass both parameters to `deep_think_passes()` and `run_fan_out()`

#### 5. .env.example (~15 lines)
- Documented `DEEP_THINK_FORCE_LOCAL` environment variable
- Documented `OLLAMA_ONLY_MODE` environment variable
- Provided use case guidance

### Total Code Addition: ~220 lines
- Security functions: ~150 lines
- Integration points: ~30 lines  
- Configuration: ~40 lines

## Failure Modes & Recovery

| Scenario | DEEP_THINK_FORCE_LOCAL | OLLAMA_ONLY_MODE | Behavior |
|----------|------------------------|--------------------|----------|
| Cloud provider in config | 1 (default) | 0 (default) | Raise `SecurityError`, fail job |
| Cloud provider in config | 1 (default) | 1 (strict) | Raise `SecurityError`, fail job |
| Cloud provider in config | 0 (disabled) | 0 (default) | Allow (not MQTT mode) |
| Ollama unreachable | 1 (default) | 0 (default) | Log warning, retry with graceful degradation |
| Ollama unreachable | 1 (default) | 1 (strict) | Raise `SecurityError`, fail job |
| Ollama unreachable | 0 (disabled) | - | Not enforced (non-MQTT mode) |

## Production Deployment

### Recommended Setup

```bash
# MQTT colony mode (recommended)
export DEEP_THINK_FORCE_LOCAL=1      # Enable enforcement
export OLLAMA_ONLY_MODE=0             # Graceful fallback
export OLLAMA_BASE_URL=http://ollama-cluster:11434

# Strict production mode (alternative)
export DEEP_THINK_FORCE_LOCAL=1      # Enable enforcement
export OLLAMA_ONLY_MODE=1             # Hard failure
export OLLAMA_BASE_URL=http://ollama-cluster:11434

# Non-MQTT mode (cloud allowed)
export DEEP_THINK_FORCE_LOCAL=0      # Allow cloud
export OLLAMA_ONLY_MODE=0             # No enforcement
```

### Monitoring

1. **Audit logs**: Check for `[SECURITY]` and `[MQTT]` tagged logs
2. **Metrics**: Track MQTT jobs with `force_local_models=True`
3. **Alerts**: Set threshold on `SecurityError` exceptions

## Future Enhancements

- [ ] Metrics collection for MQTT local-only enforcement
- [ ] Integration with agent telemetry (DAMA)
- [ ] Hardware acceleration detection (GPU) for Ollama
- [ ] Model preloading at startup for cold-start optimization
- [ ] Provider fallback chain (Ollama → Ollama-secondary → fail)
- [ ] Cost tracking and billing per MQTT device

## References

- **Main Engine**: `engine/orchestrator.py`
- **Provider Module**: `engine/provider.py`
- **Worker**: `worker.py`
- **Tests**: `test_mqtt_local_enforcement.py`
- **Configuration**: `.env.example`
