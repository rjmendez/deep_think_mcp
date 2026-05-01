# Deep Think MCP — Integration Wiring Diagram

This document describes the data flow, module dependencies, and type boundaries in the refactored deep_think_mcp architecture.

---

## Module Architecture

```
deep_think_mcp/
├── core.py                    ← PUBLIC API SURFACE (clean imports)
│   ├── imports from engine/
│   ├── imports from validation/
│   └── provides wiring functions (run_reasoning_with_validation, etc.)
│
├── engine/
│   ├── __init__.py            ← Engine package exports
│   │   └── re-exports from ../engine.py
│   └── [future refactoring: split into passes/, providers/, models/]
│
├── validation/
│   ├── __init__.py            ← Validation package exports
│   │   └── re-exports from ../ground_truth.py
│   └── [future refactoring: split into providers/, results/, extractors/]
│
├── engine.py                  ← MONOLITHIC (legacy, to be refactored)
│   ├── ProviderConfig
│   ├── build_provider_config()
│   ├── deep_think_passes()
│   ├── run_fan_out()
│   ├── classify_task()
│   ├── TASK_CLASS_PROFILES
│   ├── PERSPECTIVE_MANDATES
│   └── ... provider logic (anthropic, copilot, ollama) ...
│
├── ground_truth.py            ← MONOLITHIC (legacy, to be refactored)
│   ├── Claim
│   ├── SensorSnapshot
│   ├── ValidationResult
│   ├── PassValidationResult
│   ├── GroundTruthProvider (Protocol)
│   ├── create_ground_truth_provider()
│   └── ... implementations (Nova, MQTT) ...
│
└── server.py                  ← FastMCP SERVER
    ├── imports from engine/
    ├── imports from validation/
    └── exposes: deep_think_async, deep_think_fan_out, get_thinking_result, etc.
```

---

## Data Flow: Request → Engine → Validation → Next Pass

### 1. Request Intake (server.py)

```python
# HTTP/MCP call
deep_think_async(
    question="What is X?",
    passes=3,
    task_class="general",
    data_policy="any",
    provider_config={"model": "claude-opus-4.7"},
)
```

**Input Processing** (server.py):
- Merge `provider_config` with CLI parameters
- Call `engine.build_provider_config(pc)` to resolve provider/model
- Call `engine.TASK_CLASS_PROFILES` to look up routing hints
- Call `engine.model_summary(cfg, task_class)` for display

**Control Flow**:
```
server.py::deep_think_async()
    → build_provider_config(overrides)     [engine.__init__.py]
    → model_summary(cfg, task_class)       [engine.__init__.py]
    → store.create_job(...)                [store.py]
    → return job_id (immediate)
```

### 2. Job Processing (worker.py)

The worker loop (asynchronously) processes queued jobs:

```python
# worker.py retrieves job from store
job = store.get_job(job_id)
pc = json.loads(job.provider_config_json)
cfg = engine.build_provider_config(pc)

# Route to appropriate reasoning engine
if job.fan_out:
    result = await engine.run_fan_out(question, width=3, height=2, cfg=cfg, ...)
else:
    result = await engine.deep_think_passes(question, passes=3, cfg=cfg, ...)

# Store result
store.update_job(job_id, result=result, status="complete")
```

**Data Flow**:
```
worker.py::worker_loop()
    → engine.deep_think_passes()  (standard reasoning)
       OR
    → engine.run_fan_out()        (perspective fan-out)
    
    ↓ (for each pass)
    
    → _call_provider(cfg, question, tier, model, ...)
       → _call_anthropic() | _call_copilot() | _call_ollama()
       
    ↓ (receive pass output)
    
    → Optional: validate_claims_against_ground_truth()
       [if ground_truth_provider is available]
    
    ↓ (next pass or synthesis)
    
    → Store result in job.result (JSON)
```

### 3. Reasoning Engine (engine.py)

The engine implements multi-pass reasoning:

```python
async def deep_think_passes(
    question: str,
    passes: int,
    cfg: ProviderConfig,
    verify: bool = False,
) -> dict:
    """Run sequential reasoning passes."""
    
    results = []
    
    for pass_num in range(passes):
        # Select framing (adaptive, task-class routing, etc.)
        framing = _select_adaptive_framing(question, pass_num, task_class)
        
        # Choose tier (light/medium/heavy) based on pass number
        tier = _pass_tier(pass_num, passes)
        
        # Resolve model for this tier and provider
        model = _model_for_tier(cfg, tier, task_class)
        
        # Call provider API
        output = await _call_provider(
            cfg, question, tier, model, framing=framing
        )
        
        # Optional: Extract claims and validate
        if ground_truth_provider:
            claims = await _extract_claims_from_pass_output(output)
            validation = await ground_truth_provider.validate_claims(claims)
            # Log validation but continue reasoning
        
        results.append({
            "pass": pass_num,
            "output": output,
            "model": model,
            "tier": tier,
            "validation": validation or None,
        })
    
    # Optional: verification pass (RYS)
    if verify:
        final_pass = await deep_think_passes(
            question, 1, cfg, verify=False, prompt="Verify these claims..."
        )
        results.append({"pass": "verification", "output": final_pass})
    
    return {
        "final_answer": results[-1]["output"],
        "passes": results,
    }
```

**Provider Resolution** (per tier):
```
_model_for_tier(cfg, "heavy", "code_review")
    ↓
    1. Check cfg.model (explicit override)
    2. Check cfg.heavy / cfg.heavy_provider (per-tier overrides)
    3. Check DEEP_THINK_ANTHROPIC_HEAVY env var
    4. Check TASK_CLASS_PROFILES["code_review"]["heavy"] (routing hint)
    5. Check _ANTHROPIC_DEFAULTS["heavy"]
    6. Fallback: auto-detect from env (ANTHROPIC_API_KEY, etc.)
    
    ← Return: "claude-opus-4.7" or "qwen2.5-coder" or similar
```

### 4. Ground Truth Validation (optional)

**When validation is active** (requires `ground_truth_provider`):

```python
# Inside each reasoning pass
claims = await _extract_claims_from_pass_output(pass_output)
# [extractors/llm-based claim distillation]

validation_results = await ground_truth_provider.validate_claims(claims)
# Returns: List[ValidationResult]
# Structure:
# {
#     "claim_id": "claim_1",
#     "is_valid": true/false,
#     "ground_truth_value": 3.14,
#     "confidence": 0.92,
#     "evidence": [...],
#     "contradiction_source": "sensor_xyz" or None,
# }

# Log validation (non-blocking) — reasoning continues
# Future: could feed validation back into next pass framing
```

**Validation Implementations**:
- **NovaGroundTruthProvider** — queries Nova (Great Library)
- **MQTTGroundTruthProvider** — subscribes to MQTT sensor topics

---

## Type Boundaries

### Engine (engine.py)

**Input Types**:
```python
class ProviderConfig:
    provider: str           # "anthropic" | "copilot" | "ollama"
    model: str              # "claude-opus-4.7" | "gpt-5.4" | "llama2"
    light: str              # per-tier override
    medium: str
    heavy: str
    base_url: str           # Ollama endpoint
    data_policy: str        # "any" | "local" | "cloud"
```

**Output Types**:
```python
dict: {
    "final_answer": str,        # The synthesized answer
    "passes": [
        {
            "pass": int,
            "model": str,
            "tier": str,            # "light" | "medium" | "heavy"
            "output": str,          # Raw model output
            "framing": str,         # Instruction used
            "validation": {...},    # Optional: ValidationResult
        }
    ],
    "verification_pass": {...} or None,  # If verify=True
}
```

### Validation (ground_truth.py)

**Input Types**:
```python
class Claim:
    id: str                     # unique ID
    statement: str              # the claim text
    claim_type: str             # "telemetry_staleness" | "code_defect" | ...
    expected_value: Any         # what should be true
    confidence_model: float     # model's confidence (0-1)
```

**Output Types**:
```python
class ValidationResult:
    claim_id: str
    is_valid: bool              # matches ground truth?
    ground_truth_value: Any
    evidence: List[Dict]        # sensor readings, code analysis, etc.
    confidence: float           # validation confidence (0-1)
    contradiction_source: str or None
    metadata: Dict              # source, freshness, reliability

class PassValidationResult:
    pass_num: int
    claims_extracted: List[Claim]
    validation_results: List[ValidationResult]
    hallucination_count: int
    hallucination_details: List[Dict]
    overall_confidence: float
    contradiction_with_prior: List[Dict]
```

---

## Import Hierarchy

```
core.py (public API)
    ├── from .engine (clean re-exports)
    │   └── engine/__init__.py
    │       └── engine.py (monolithic)
    │
    ├── from .validation (clean re-exports)
    │   └── validation/__init__.py
    │       └── ground_truth.py (monolithic)
    │
    └── wiring functions (run_reasoning_with_validation, etc.)

server.py (FastMCP entry point)
    ├── from .engine (direct imports for performance)
    ├── from .validation (optional, if ground truth integration needed)
    ├── from . import store, worker, discover
    │
    └── exports MCP tools

worker.py (async job processor)
    ├── from . import engine
    ├── from .validation import (optional)
    ├── from . import store, core
    │
    └── worker_loop() processes jobs async
```

### Import Strategy

**Core module**: 
- Clean re-exports for external users
- `from .engine import ProviderConfig` works
- `from .validation import Claim` works

**Server module**:
- Direct imports from engine/ and validation/
- No circular dependencies
- Fast initialization (importlib handles monolithic modules only once)

**Internal modules**:
- Can import from engine.py or validation.py directly if needed
- Can also use core.py for public API

---

## Future Refactoring Path

### Phase 1 (Current)
- ✅ Create clean module structure (engine/, validation/)
- ✅ Package monolithic files via __init__.py re-exports
- ✅ Update server.py to use new imports

### Phase 2
- Split engine.py:
  - `engine/passes.py` — deep_think_passes, run_fan_out logic
  - `engine/providers.py` — _call_anthropic, _call_copilot, _call_ollama
  - `engine/routing.py` — TASK_CLASS_PROFILES, _select_adaptive_framing
  - `engine/config.py` — ProviderConfig, build_provider_config

- Split ground_truth.py:
  - `validation/providers.py` — GroundTruthProvider protocol
  - `validation/implementations/nova.py` — NovaGroundTruthProvider
  - `validation/implementations/mqtt.py` — MQTTGroundTruthProvider
  - `validation/types.py` — Claim, ValidationResult, etc.

### Phase 3
- Update __init__.py files to import from refactored modules
- Monolithic files become optional (kept for compatibility during transition)
- Core.py becomes the primary public interface

---

## Dependency Summary

| Module | Imports From | Exports To |
|--------|--------------|-----------|
| **core.py** | engine/, validation/ | external users, docs, tests |
| **engine/__init__.py** | engine.py (importlib) | core.py, server.py, worker.py |
| **validation/__init__.py** | ground_truth.py (importlib) | core.py, worker.py |
| **server.py** | engine/, validation/, store, worker, discover | FastMCP entrypoint |
| **worker.py** | engine, validation, core, store | async job processing |
| **engine.py** | ground_truth (optional), providers (external APIs) | engine/__init__.py |
| **ground_truth.py** | nova (optional), mqtt (optional) | validation/__init__.py |

---

## Checklist: What's Undefined/Stubbed

### In engine.py
- [x] ProviderConfig class
- [x] build_provider_config() function
- [x] deep_think_passes() function
- [x] run_fan_out() function
- [x] classify_task() function
- [x] TASK_CLASS_PROFILES constant
- [x] PERSPECTIVE_MANDATES constant
- [x] Provider calling functions (_call_anthropic, etc.)

### In ground_truth.py
- [x] Claim class
- [x] SensorSnapshot class
- [x] ValidationResult class
- [x] PassValidationResult class
- [x] GroundTruthProvider protocol
- [x] create_ground_truth_provider() function
- [x] NovaGroundTruthProvider implementation
- [x] MQTTGroundTruthProvider implementation

### In core.py
- [x] run_reasoning_with_validation() function (stubbed, returns result without validation)
- [ ] Integration with actual ground truth providers (future work)
- [ ] Claim extraction pipeline (future work)

### In server.py
- [x] All routes preserved (deep_think_async, deep_think_fan_out, get_thinking_result, etc.)
- [x] All handler signatures unchanged
- [x] Comments showing where new modules come from

### In engine/__init__.py
- [x] Re-exports from engine.py via importlib

### In validation/__init__.py
- [x] Re-exports from ground_truth.py via importlib
