# deep_think_mcp

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
| `auto` | Lightweight classifier picks the best class (confidence ≥ 0.75) | — |

### Data Policy

Control which providers are allowed to receive data — important for sensitive security investigations:

| Policy | Behaviour |
|---|---|
| `any` (default) | Use any configured provider including cloud |
| `local` | **Ollama ONLY** — no data sent to cloud providers |
| `cloud` | Cloud providers preferred; Ollama only for light tier |

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
| `OLLAMA_BASE_URL` | Ollama endpoint (default: `http://localhost:11434`) |

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

## Tools

### `deep_think_async`

Queue a reasoning job. Returns `job_id` immediately.

```json
{
  "question": "Is this authentication code vulnerable to timing attacks?",
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
| `data_policy` | `"any"` | `"any"` \| `"local"` \| `"cloud"` |
| `model` | `""` | Override all tiers with one model ID |
| `provider_config` | `null` | Per-call overrides (no secrets — use env vars) |

`provider_config` keys (no secrets):

| Key | Description |
|---|---|
| `provider` | `"anthropic"` \| `"copilot"` \| `"ollama"` |
| `base_url` | Ollama endpoint override |
| `model` | Single model ID for all tiers |
| `light` / `medium` / `heavy` | Per-tier model ID overrides |

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
