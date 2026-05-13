# deep_think_mcp

Licensed under the [MIT License](LICENSE).

Standalone async multi-pass reasoning MCP server with automatic model routing.

The server selects specialist models for each task type — code review uses code-tuned models, security investigations use evidence-weighing directives, and safety tasks run a guardian pre-check — without the caller needing to know model names.

Jobs are submitted instantly, run in the background, and polled for results. No Redis, no Postgres — just Python and a local SQLite file.

## Task Classes

| Class | Description | Specialist models |
|---|---|---|
| `general` | Default reasoning — use when no other class fits | phi4-mini / llama3.1 / claude-opus |
| `code_review` | Bug detection, security review, code quality | **qwen2.5-coder** (ollama) / **gpt-5.2-codex** (copilot) |
| `investigation` | Evidence weighing, IOC triage, incident response | Evidence inventory → hypothesis matrix → prosecution/defense → synthesis |
| `safety` | Risk detection, harm mapping, guardrail evaluation | Runs **granite3-guardian** pre-check (local only) |
| `extraction` | Structured JSON output, entity recognition | Lighter models — extraction is pattern matching, not reasoning |
| `synthesis` | Writing, summarization, report drafting | Full model stack with narrative stress-test pass |
| `reasoning` | Complex logical / mathematical reasoning | Biases toward largest available models |
| `auto` | Lightweight classifier picks the best class (no confidence-threshold gate currently enforced) | — |

## Skill Files

Predefined Deep Think skills are loaded from `skills/*.yaml` at startup. Each skill is auditable on disk, imported by the MCP server, and normalized into the in-memory routing registry used by the async and fan-out engines.

Minimal file shape:

```yaml
kind: deep-think-skill
version: 1
id: code_review
task_class: code_review
description: Code analysis, bug detection, security review, and interface scrutiny.
routing:
  directive_set: code_review
  mandate_set: code_review
controls:
  verification_mode: review
models:
  ollama:
    light: qwen2.5-coder:7b
    medium: qwen2.5-coder:7b
    heavy: qwen2.5-coder:7b
```

Supported top-level fields:

| Field | Purpose |
|---|---|
| `id` | Stable skill identifier used by MCP callers |
| `task_class` | Base semantic class for enforcement and reporting |
| `routing.directive_set` | Named built-in directive set (`general`, `investigation`, `planning`, etc.) |
| `routing.mandate_set` | Named built-in fan-out mandate set |
| `directives` | Optional inline directives instead of a named directive set |
| `fan_out.mandates` | Optional inline mandate definitions |
| `controls` | Profile controls such as `safety_precheck`, `force_local`, `block_research_tools` |
| `models` | Per-provider, per-tier model preferences |

Built-in shipped skills now include the original task classes plus `adversarial`, `research`, and `planning`.

### Data Policy

Control which providers are allowed to receive data — important for sensitive security investigations:

| Policy | Behaviour |
|---|---|
| `any` (default) | Use any configured provider including cloud |
| `local` | **Ollama ONLY** — no data sent to cloud providers |
| `cloud` | Cloud-only routing; Ollama is blocked for all tiers |

Set globally: `DEEP_THINK_DATA_POLICY=local`  
Set per-call: `data_policy` parameter on `deep_think_async`

### Model Routing Table

With a mixed Ollama (light/medium) + Copilot (heavy) setup:

| Task class | Light | Medium | Heavy |
|---|---|---|---|
| general | phi4-mini | qwen3:8b | claude-opus-4.7 |
| code_review | **qwen2.5-coder:7b** | **qwen2.5-coder:7b** | claude-opus-4.7 |
| investigation | phi4-mini | claude-sonnet-4.6 | claude-opus-4.7 |
| safety | **granite3-guardian:2b** | claude-sonnet-4.6 | claude-opus-4.7 |
| extraction | phi4-mini | **claude-sonnet-4** | **claude-sonnet-4.6** |
| synthesis | phi4-mini | claude-sonnet-4.6 | claude-opus-4.7 |
| reasoning | phi4-mini | claude-sonnet-4.6 | claude-opus-4.7 |

> Models marked **bold** differ from the `general` defaults.
> Profile recommendations fall back to tier defaults if a model isn't available on your Ollama server (discovered at startup via `/api/tags`).
> See [`docs/ollama-lineup.md`](docs/ollama-lineup.md) for the full recommended local model set.

### Confirmed-working Copilot model IDs (as of 2026-04-30)

These model IDs work with the `/chat/completions` endpoint and `Copilot-Integration-Id: vscode-chat`:

| Model ID | Tier | Notes |
|---|---|---|
| `gpt-4o-mini` | light | Fastest, lowest cost |
| `claude-sonnet-4` | medium | Lighter Sonnet — good for extraction/classification |
| `claude-sonnet-4.6` | medium/heavy | Best balance for most tasks |
| `claude-opus-4.5` | heavy | Available but superseded |
| `claude-opus-4.6` | heavy | Available but superseded |
| `claude-opus-4.7` | heavy | Default heavy — best quality |

**Not accessible** via `/chat/completions`: `gpt-5.x-codex` (different endpoint), `o1/o3/o4-mini` (reasoning endpoint), `gpt-4o`, `gpt-5.x`, `claude-sonnet-4.5`.
If your org enables additional models, override with `DEEP_THINK_COPILOT_LIGHT/MEDIUM/HEAVY` env vars.

## Quick start

```bash
pip install -r requirements.txt
```

> **Note on Python import path:** The repo is named `deep-think-mcp` (hyphens) but the Python
> package is `deep_think_mcp` (underscores). Create a symlink so Python can find it:
> ```bash
> ln -sf ~/Dev/deep-think-mcp ~/Dev/deep_think_mcp
> ```
> This is a one-time setup step. `run.sh` and `run_http.sh` set `PYTHONPATH` to the parent
> directory automatically. A future `pyproject.toml` will make this unnecessary.

```bash
python -m deep_think_mcp          # starts on http://0.0.0.0:8002
```

For local MCP clients (Claude Desktop, Copilot CLI):

```bash
python -m deep_think_mcp          # stdio mode
# or set DEEP_THINK_TRANSPORT=stdio
```

## Docker

```bash
docker build -t deep-think-mcp .
docker run -p 8002:8002 \
  -v $(pwd)/data:/data \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  deep-think-mcp
```

Jobs are persisted to `/data/jobs.db` inside the container.

## Configuration

All configuration via environment variables.

### Provider & secrets

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key — enables `anthropic` provider |
| `GITHUB_COPILOT_OAUTH_TOKEN` | GitHub Copilot OAuth token — enables `copilot` provider |
| `OLLAMA_BASE_URL` | Ollama endpoint (required) |

Provider is auto-detected: Anthropic key → Anthropic; Copilot token → Copilot; otherwise Ollama.

The `copilot` provider also reads from `~/.config/gh/hosts.yml` if you have the `gh` CLI installed and are logged in.

### Per-tier provider overrides (mixed-provider setup)

Route cheap passes to local Ollama and expensive synthesis to a remote API:

| Variable | Description |
|---|---|
| `DEEP_THINK_LIGHT_PROVIDER` | Provider for light tier (e.g. `"ollama"`) |
| `DEEP_THINK_MEDIUM_PROVIDER` | Provider for medium tier |
| `DEEP_THINK_HEAVY_PROVIDER` | Provider for heavy/synthesis tier (e.g. `"anthropic"`) |

Example — local Qwen for drafts, Anthropic Opus for synthesis:
```bash
DEEP_THINK_LIGHT_PROVIDER=ollama
DEEP_THINK_MEDIUM_PROVIDER=ollama
DEEP_THINK_HEAVY_PROVIDER=anthropic
OLLAMA_BASE_URL=http://my-gpu-box:11434
DEEP_THINK_MODEL_LIGHT=qwen3:8b
DEEP_THINK_MODEL_MEDIUM=qwen3:14b
ANTHROPIC_API_KEY=sk-ant-...
```

### Qwen thinking mode

Ollama's Qwen3 models run extended chain-of-thought by default, which adds latency and tokens. Extended thinking is **disabled automatically** for any model whose name contains `qwen`.

| Variable | Behavior |
|---|---|
| unset | Disable thinking for `qwen*` models, enable for all others |
| `DEEP_THINK_OLLAMA_THINK=false` | Disable thinking for all Ollama models |
| `DEEP_THINK_OLLAMA_THINK=true` | Enable thinking for all Ollama models |



Each reasoning pass uses a tier: `light` (cheap/fast), `medium` (analysis), `heavy` (final synthesis).

| Variable | Default |
|---|---|
| `DEEP_THINK_ANTHROPIC_LIGHT` | `claude-haiku-4-5` |
| `DEEP_THINK_ANTHROPIC_MEDIUM` | `claude-sonnet-4-5` |
| `DEEP_THINK_ANTHROPIC_HEAVY` | `claude-opus-4-5` |
| `DEEP_THINK_COPILOT_LIGHT` | `claude-sonnet-4.5` |
| `DEEP_THINK_COPILOT_MEDIUM` | `claude-sonnet-4.6` |
| `DEEP_THINK_COPILOT_HEAVY` | `claude-opus-4.7` |
| `DEEP_THINK_MODEL_LIGHT` | `phi4-mini` |
| `DEEP_THINK_MODEL_MEDIUM` | `qwen3:8b` |
| `DEEP_THINK_MODEL_HEAVY` | `deepseek-r1:8b` |

### Server

| Variable | Default | Description |
|---|---|---|
| `DEEP_THINK_TRANSPORT` | `streamable-http` | `streamable-http` or `stdio` |
| `DEEP_THINK_HOST` | `0.0.0.0` | Bind host |
| `DEEP_THINK_PORT` | `8002` | Bind port |
| `DEEP_THINK_DB` | `~/.deep_think/jobs.db` | SQLite database path |
| `DEEP_THINK_MAX_CONCURRENCY` | `2` | Max simultaneous reasoning jobs |
| `LOG_LEVEL` | `INFO` | Logging level |

### Portable / no-Nova mode

If you want to run `deep_think_mcp` in an environment without Nova, disable the Nova-dependent layers explicitly:

| Switch | Scope | Effect |
|---|---|---|
| `DEEP_THINK_NOVA_VERIFY=0` | server env | Disables the post-run Nova fact-check / verification pipeline in `worker.py` |
| `SKIP_VALIDATION=1` | server env | Disables pass-level ground-truth validation hooks in `engine/orchestrator.py` |
| `enable_research=false` | per-call parameter | Disables research-tool injection for most classes; `code_review` requires research enabled |

Recommended minimal portable setup:

```bash
DEEP_THINK_NOVA_VERIFY=0
SKIP_VALIDATION=1
```

Then submit jobs with:

```json
{
  "enable_research": false
}
```

This keeps the core async reasoning engine usable without Nova credentials, Nova `/verify`, or Nova-backed research services.

### Applying config changes

If `deep_think_mcp` is running as a systemd user service, environment or code changes are not live until the service is restarted:

```bash
systemctl --user restart deep-think-mcp.service
```

After restarting the service, reload/reconnect your MCP client so it binds to the fresh server process.

## Tools

### `deep_think_async`

Queue a reasoning job. Returns `job_id` immediately.

```json
{
  "question": "Is this authentication code vulnerable to timing attacks?",
  "skill": "code_review",
  "task_class": "code_review",
  "passes": 4,
  "data_policy": "local"
}
```

```json
{
  "question": "Analyze these login anomalies for signs of credential stuffing",
  "task_class": "investigation",
  "data_policy": "local",
  "passes": 4
}
```

```json
{
  "question": "Summarize Q3 incident trends for the executive report",
  "task_class": "synthesis",
  "passes": 3
}
```

Parameters:

| Parameter | Default | Description |
|---|---|---|
| `question` | — | The question or problem to reason about |
| `passes` | `3` | Number of reasoning passes (2–6) |
| `task_class` | `"general"` | Task class — see table above |
| `skill` | `null` | Optional predefined skill ID loaded from `skills/*.yaml` |
| `data_policy` | `"any"` | `"any"` \| `"local"` \| `"cloud"` |
| `model` | `""` | Override all tiers with one model ID |
| `provider_config` | `null` | Per-call overrides (no secrets — use env vars) |
| `enable_research` | `true` | Inject research tools when the task class permits them |

`provider_config` keys (no secrets):

| Key | Description |
|---|---|
| `provider` | `"anthropic"` \| `"copilot"` \| `"ollama"` |
| `base_url` | Ollama endpoint override |
| `model` | Single model ID for all tiers |
| `light` / `medium` / `heavy` | Per-tier model ID overrides |
| `light_provider` / `medium_provider` / `heavy_provider` | Per-tier provider overrides |
| `temperature` | Sampling temperature for supported providers |
| `top_p` / `top_k` | Sampling controls for supported providers |
| `max_tokens` | Output token cap (`num_predict` for Ollama) |
| `seed` | Deterministic seed for Ollama |
| `custom_params` | Nested provider-specific sampling params |
| `options` | Ollama-native options object (merged with flat keys) |

### `get_thinking_result`

Poll a job by `job_id`. Status: `queued → running → complete | failed`.

When complete, returns the `result` (final answer + metadata).

| Parameter | Default | Description |
|---|---|---|
| `job_id` | — | Job ID returned by `deep_think_async` or `deep_think_fan_out` |
| `include_reasoning_chain` | `false` | When `true`, attaches a `reasoning_chain` field with all intermediate pass outputs from `pass_cache`, grouped by perspective. Use for forensic review, debugging, or generating full reasoning reports. Omit for normal polling — response stays compact. |

`reasoning_chain` shape (when included):
```json
[
  {
    "perspective": "main",
    "passes": [
      {"pass_num": 1, "framing": "decomposition", "tier": "light",
       "model_used": "phi4-mini:latest", "provider": "ollama", "output": "..."},
      {"pass_num": 4, "framing": "synthesis", "tier": "heavy",
       "model_used": "claude-opus-4.7", "provider": "copilot", "output": "..."}
    ]
  },
  {
    "perspective": "defense",
    "passes": [...]
  }
]
```

For fan-out jobs, perspectives are named (`defense`, `prosecution`, `forensics`, etc.). For standard jobs, the single perspective is named `main`. Perspectives that were served from `perspective_cache` (cache hits from prior runs) appear with a single stub pass: `framing = "perspective_cache_hit"`, `tier/model/provider = "cached"`.

### `list_thinking_jobs`

List recent jobs. Filter by `status` (`all`, `queued`, `running`, `complete`, `failed`).

## Lessons from Live Security Incident Triage

The following was learned running `investigation` class jobs against real GreyMatter incidents (RQ41335165, RQ41295056). Findings shaped several bug fixes and design decisions.

### Deep think vs fan-out — when to use each

| | Deep think (4 passes) | Fan-out (6 perspectives × 2 passes) |
|---|---|---|
| **Best for** | Clear-cut incidents, initial triage, time-sensitive | Ambiguous incidents where disposition is contested |
| **Output** | Single coherent narrative | Explicit `converged_claims` + `contested_areas` |
| **Value** | Clean single story | Surfaces genuine disagreement — the "prosecution vs defense" split is the real signal |
| **Confidence** | Usually higher (single narrative) | Lower, more honest — contested areas cap the score |
| **Cost** | ~7 min (Sonnet 4.6 heavy) | ~8.5 min (Sonnet 4.6 heavy) |

Run **deep think first** for fast triage. Add **fan-out** when deep think returns moderate confidence (50–70%) or when you need to know *why* analysts would disagree.

### Confidence calibration

- **93%** — Clear false positive (RQ41335165: Nessus scanner hitting npm package.json, all signals aligned)
- **62%** — Genuinely ambiguous incident (RQ41295056: AV exclusion + lsass read, missing script source, prior FP pattern present but not applicable)

A 62% result from fan-out is **correct** on a hard incident. Do not tune the system to chase higher numbers on ambiguous evidence — that produces false confidence, not better analysis.

### Mixed provider setup (Ollama light/medium + Copilot heavy)

When using per-tier provider overrides (`DEEP_THINK_LIGHT_PROVIDER=ollama`, `DEEP_THINK_HEAVY_PROVIDER=copilot`) with `DEEP_THINK_OLLAMA_TIMEOUT_FALLBACK=true`:

- Light/medium passes are attempted on Ollama first. If they time out, the call falls back to Copilot automatically.
- Prior to fix `2d91ac4`, the `provider` column in `pass_cache` recorded the *intended* provider (`"ollama"`) even when the fallback to Copilot actually served the call. `model_used` was always correct. The fix propagates a 3-tuple `(text, model, provider_used)` from `_call_provider` through all callers.
- **Practical implication**: if your Ollama server is remote and occasionally slow, all passes may end up served by Copilot. Check `pass_cache.provider` after a job to confirm actual routing.

### Pass cache and perspective cache interaction

Fan-out jobs use two caches: `perspective_cache` (full perspective output, content-addressed) and `pass_cache` (per-pass intermediate outputs, job-scoped).

- Perspective cache hits are **cross-job**: if two fan-out jobs ask the same question with the same model, perspectives from job 1 are replayed in job 2 without re-running.
- Prior to fix `2d91ac4`, perspective cache hits skipped `pass_cache` writes entirely, leaving those perspectives absent from the reasoning chain. The fix writes a stub `pass_cache` row (framing=`"perspective_cache_hit"`) so all perspectives always appear in reports.
- When generating full reasoning reports, stubs indicate "this perspective ran in a prior job and was reused" — the final answer is identical but intermediate passes are not available for the reused execution.

### Sonnet 4.6 vs Opus 4.7 as heavy model

On a complex security incident (RQ41295056):
- **Opus 4.7 fan-out**: 11m 58s, synthesis was truncated (hit token limit mid-sentence)
- **Sonnet 4.6 fan-out**: 8m 33s (~30% faster), synthesis was complete, same 62% confidence

Sonnet 4.6 is the recommended default heavy model for `investigation` class in production. Reserve Opus 4.7 for synthesis tasks where answer completeness is critical and latency is acceptable.

## Architecture

```
MCP client
    │
    ▼
deep_think_async()  ──→  SQLite (queued)
                              │
                         worker_loop()
                              │
                    ┌─────────▼──────────┐
                    │  engine.py          │
                    │  Pass 1 (light)     │
                    │  Pass 2 (medium)    │
                    │  Pass 3 (medium)    │
                    │  Pass 4 (heavy)     │
                    └─────────┬──────────┘
                              │
                         SQLite (complete)
                              │
                    get_thinking_result()
```

- Worker loop runs inside the same process as the MCP server (asyncio task)
- SQLite + WAL mode handles concurrent reads/writes safely
- Stale `running` jobs are automatically requeued on startup (crash recovery)
- Job history persists indefinitely; query with `list_thinking_jobs`

---

## How to Use Deep-Think Effectively: Lessons Learned

After extensive use in real production scenarios (DAMA sensor ant code review, deep-think infrastructure fixes, Sprint 2 planning), the following patterns emerged as critical for success.

### ❌ **ANTI-PATTERN: Embedding File Paths Instead of Code**

**WRONG:**
```json
{
  "question": "Fix the bugs in orchestrator.py lines 94-98, store.py lines 556-594, and engine.py lines 2113-2118. What's the right fix?",
  "task_class": "code_review"
}
```

**Result:** Deep-think cannot access your filesystem. Returns vague errors or incorrect suggestions because it's working blind.

**CORRECT:**
```json
{
  "question": "ACTUAL CODE (orchestrator.py lines 94-98): ... [full code block] ... BUG DESCRIPTION: Claim() constructor mismatch. Current call passes (text, confidence, category) but dataclass expects (id, statement, claim_type, subject, expected_value). QUESTION: What's the minimal fix?",
  "task_class": "code_review"
}
```

**Result:** Deep-think sees the actual code, identifies the fix precisely.

### ❌ **ANTI-PATTERN: Embedding Unresolved Open Questions**

**WRONG:**
```json
{
  "question": "Should we refactor the registry? How do we handle backward compatibility? What about concurrency? How many collectors should we convert? What's the migration path? When should we feature flag this?",
  "passes": 4
}
```

**Result:** All passes return `[ERROR: ]`. Deep-think cannot reason about 10 unsolved design questions simultaneously.

**CORRECT:**
```json
{
  "question": "FACTS: AntModelRegistry is compile-time only (no runtime registration). Registry refactor requires: (A) full conversion of 30 collectors, (B) partial conversion of 5 collectors, or (C) deferral to Sprint 3. DEPENDENCIES: Registry unblocks inference threading and window aggregators for Sprint 3. CONSTRAINT: Metrics export (v0.6.0) must not break. QUESTION: Which option maximally unblocks Sprint 3 while maintaining v0.6.0 stability?",
  "passes": 3
}
```

**Result:** Deep-think weighs tradeoffs explicitly, returns actionable recommendation with confidence score.

### ✅ **PATTERN: Parallelized Focused Jobs**

Instead of one monolithic job with 10 open questions, queue 3-4 parallel jobs with one specific question each.

**Example: 3 parallel DAMA Sprint 2 jobs**

Job 1 (Priority):
```
FACTS: Registry refactor blocks 2 downstream items. Error handling unblocks verification triggers.
DEPENDENCIES: [explicit] 
QUESTION: Which two items should be in Sprint 2?
```

Job 2 (Registry staging):
```
FACTS: 30 collectors in switch/case. New interface pattern proposed.
QUESTION: Should refactor be phased (interface + 5 samples) or completed (all 30)?
```

Job 3 (Error handling sequence):
```
FACTS: Need: metrics, budgets, logging. Integration points: [explicit].
QUESTION: What's the safest implementation sequence?
```

**Result:** All 3 complete in 60-90 seconds with high confidence (85%+).
**Failure mode:** One 30-question mega-job → all passes `[ERROR: ]` → nothing learned.

### ✅ **PATTERN: Facts-Only Prompts**

Structure every prompt as: **FACTS → QUESTION**

**FACTS section:**
- Actual source code (not file paths)
- Confirmed values, measurements, constraints
- Dependencies (explicit relationships)
- Test results, benchmarks
- Prior decisions and their rationale

**QUESTION section:**
- One solvable, specific question
- Clear decision options (A/B/C)
- "Why" framing invites deeper reasoning
- Avoid "is this right?" — ask "which of these approaches..."

**Example structure:**
```
FACTS (embedded code):
```java
public final class AntModelRegistry {
    public static final int WINDOW_SIZE = 20;
    public static final class AntSpec { ... }
}
```

FACT (measurement):
- Current: Hardcoded to 20 timesteps
- Affected: All 30 collectors
- Blocker: Window aggregators need per-ant sizes

FACT (dependency):
- Registry refactor → inference threading (runtimes map)
- Registry refactor → window aggregators (WINDOW_SIZE)

QUESTION:
Should we do full registry refactor + error handling (Option A) or error handling only (Option B)?
```

### ✅ **PATTERN: Provider Configuration**

Always include explicit provider in `provider_config` to avoid "provider is REQUIRED" errors:

```python
deep_think_async(
  question="...",
  provider_config={"provider": "anthropic"},
  passes=3
)
```

Or set globally:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### ✅ **PATTERN: Calibrate Task Class and Passes**

| Task | Class | Passes | Rationale |
|------|-------|--------|-----------|
| Code review audit | `code_review` | 2-3 | Quicker, code-tuned models sufficient |
| Architecture decision | `reasoning` | 3-4 | Needs deeper tradeoff analysis |
| Planning (complex) | `general` | 3-4 | Parallelizable into sub-questions better |
| Writing/synthesis | `synthesis` | 2-3 | One draft pass + refine sufficient |

### ✅ **PATTERN: Validate Confidence Scores**

Deep-think returns `confidence` (0-100%). Interpret as:
- **85%+** — High confidence, actionable recommendation
- **62-85%** — Medium confidence, likely correct but check assumptions
- **<50%** — Low confidence, surface fundamental disagreement (use fan-out to explore)

On genuinely ambiguous problems (e.g., RQ41295056 security incident), 62% from fan-out is **correct** — don't tune for false confidence.

### ❌ **ANTI-PATTERN: Embedding Unknowns in the Question**

**WRONG:**
```
"We need to fix 10 bugs but I'm not sure if they're all related. 
Also, I don't know if the root cause is X, Y, or Z. 
And the user hasn't told me what the priority is. 
Should we fix all 10?"
```

**CORRECT:**
```
FACTS:
- 6 bugs confirmed with code examples (attach snippets)
- Relationships: Bug 1→2→5 (serial chain), Bug 3-4 parallel
- Priority: critical (blocking production test), high (affecting performance)
- User needs 6 bugs fixed by Friday

QUESTION: What's the minimal set to fix first?
```

### ✅ **Pattern: Parallel Implementation (Not Just Reasoning)**

After deep-think recommends a plan, queue implementation agents in parallel:

```python
# Deep-think: decides which 2 items for Sprint 2
dt_job = deep_think_async(question="...", passes=3)

# Wait for decision, then:
agent1 = task(name="registry-refactor", ...)  # Implement option A-1
agent2 = task(name="error-handling", ...)     # Implement option B-1
agent3 = task(name="infrastructure", ...)     # In parallel

# All complete in ~400 seconds
```

### ✅ **Pattern: Streaming Output During Long Jobs**

For jobs running >5 minutes, poll with `include_reasoning_chain=true` to see intermediate pass outputs:

```python
result = get_thinking_result(job_id, include_reasoning_chain=True)
for perspective in result['reasoning_chain']:
  for pass in perspective['passes']:
    print(f"Pass {pass['pass_num']}: {pass['output'][:200]}...")
```

### 📊 **Real-World Results (DAMA Sprint 1-2)**

| Scenario | Approach | Result |
|----------|----------|--------|
| Monolithic 10-question job | 1 large job, 4 passes | `[ERROR: ]` on all passes |
| Parallelized 3-question jobs | 3 focused jobs, 2 passes each | 3/3 complete, 85% confidence, 86 seconds |
| Code review + planning + fixes | Code review agent + deep-think + 4 implementation agents | 18 files, ~2,850 LOC, zero regressions, 4×400 seconds parallel |

---

## Troubleshooting

### Job Returns `[ERROR: ]` on All Passes

**Cause:** Unresolved open questions or unstructured facts in prompt.

**Fix:**
1. Extract explicit facts (code, measurements, constraints)
2. Remove unanswered questions — replace with assumption + reasoning
3. Break into 2-3 focused sub-jobs
4. Embed actual code, not file paths

### Provider Selection Behavior

`provider_config.provider` is optional. If omitted, provider defaults are resolved from `data_policy` and environment/discovered availability.

```python
provider_config={"provider": "anthropic"}  # explicit override
provider_config={}                         # valid; provider auto-resolves
```

### "Timeout on Ollama" → Fallback to Copilot

If using mixed providers with Ollama timeout fallback:
```bash
DEEP_THINK_LIGHT_PROVIDER=ollama
DEEP_THINK_HEAVY_PROVIDER=copilot
DEEP_THINK_OLLAMA_TIMEOUT_FALLBACK=true
```

Check `pass_cache.provider` after the job to see which provider actually served each pass. Ollama timeouts are rare but fatal — fallback prevents job failure.

### Job Stuck in `running` State

Jobs marked `running` for >10 minutes are requeued on worker restart. Check:
```bash
python -c "from deep_think_mcp import store; import sqlite3; 
  db = sqlite3.connect('~/.deep_think/jobs.db'); 
  print(db.execute('SELECT job_id, status, created_at FROM thinking_jobs ORDER BY created_at DESC LIMIT 5').fetchall())"
```

If a job is legitimately stuck, delete it manually and requeue.
