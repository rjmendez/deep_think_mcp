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
| general | phi4-mini | llama3.1:8b | claude-opus-4.7 |
| code_review | **qwen2.5-coder:7b** | **qwen2.5-coder:7b** | claude-opus-4.7 |
| investigation | phi4-mini | llama3.1:8b | claude-opus-4.7 |
| safety | phi4-mini | llama3.1:8b | claude-opus-4.7 |
| extraction | phi4-mini | **mistral:7b** | **claude-sonnet-4.6** |
| synthesis | phi4-mini | llama3.1:8b | claude-opus-4.7 |
| reasoning | phi4-mini | **qwen3.5:27b** | claude-opus-4.7 |

> Models marked **bold** differ from the `general` defaults.
> Profile recommendations fall back to tier defaults if a model isn't available on your Ollama server (discovered at startup via `/api/tags`).

## Quick start

```bash
pip install -r requirements.txt
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
| `DEEP_THINK_MODEL_MEDIUM` | `llama3.1:8b` |
| `DEEP_THINK_MODEL_HEAVY` | `llama3.1:8b` |

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

When complete, returns the full `reasoning_chain` (all passes) and `final_answer`.

### `list_thinking_jobs`

List recent jobs. Filter by `status` (`all`, `queued`, `running`, `complete`, `failed`).

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
