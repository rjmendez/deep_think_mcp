# Recommended Local Ollama Lineup

Specialist models for running `deep_think_mcp` locally.
Heavy/30B+ tasks should route to a shared GPU server — these models are sized to run
RAM-resident on a 16 GB Apple Silicon machine or equivalent.

---

## Quick Install

```bash
# Standard lineup — 16 GB RAM, ~28 GB disk
ollama pull phi4-mini               # 2.3 GB  light tier / fast decomposition
ollama pull qwen3:8b                # 4.9 GB  general + tool-calling + thinking
ollama pull deepseek-r1:8b          # 4.9 GB  reasoning / investigation (CoT)
ollama pull qwen2.5-coder:7b        # 4.4 GB  code review + structured extraction
ollama pull granite3-guardian:2b    # 2.5 GB  safety — IBM (RAG + jailbreak + bias)
ollama pull llama-guard3:1b         # 1.5 GB  safety — Meta (13-category hazard taxonomy)
ollama pull qwen3-embedding:0.6b    # 0.6 GB  embeddings (MTEB multilingual #1)
ollama pull deepseek-ocr:3b         # 6.2 GB  OCR / document text extraction
```

---

## Model Reference

| Model | Size | Why this one | `task_class` |
|---|---|---|---|
| `phi4-mini` | 2.3 GB | Microsoft 3.8B — fastest local model, strong instruction following | `general` light |
| `qwen3:8b` | 4.9 GB | Only local model with **tool-calling**; hybrid thinking/non-thinking | `general` / `synthesis` / `extraction` |
| `deepseek-r1:8b` | 4.9 GB | DeepSeek-R1-0528 distilled onto Qwen3-8B — dedicated chain-of-thought | `reasoning` / `investigation` |
| `qwen2.5-coder:7b` | 4.4 GB | Purpose-built code model; excellent at structured JSON output | `code_review` / `extraction` |
| `granite3-guardian:2b` | 2.5 GB | IBM risk classifier: jailbreak, groundedness, social bias, unethical behavior | `safety` pre-check |
| `llama-guard3:1b` | 1.5 GB | Meta 13-hazard taxonomy; fastest safety classifier (~1 token output) | `safety` pre-check |
| `qwen3-embedding:0.6b` | 0.6 GB | MTEB multilingual leaderboard #1 (score 70.58, June 2025); 32 K context | vector store |
| `deepseek-ocr:3b` | 6.2 GB | Specialist OCR — skip if no document ingestion use case | ingestion pipeline |

---

## Env Vars for This Lineup

Add to your `.env` (see `.env.example`):

```bash
# Ollama endpoint
OLLAMA_BASE_URL=http://localhost:11434

# Tier assignments
DEEP_THINK_MODEL_LIGHT=phi4-mini
DEEP_THINK_MODEL_MEDIUM=qwen3:8b
DEEP_THINK_MODEL_HEAVY=deepseek-r1:8b

# Route light/medium locally, heavy to cloud
DEEP_THINK_LIGHT_PROVIDER=ollama
DEEP_THINK_MEDIUM_PROVIDER=ollama
DEEP_THINK_HEAVY_PROVIDER=copilot   # or anthropic
```

### Task-class routing with this lineup

| `task_class` | Light | Medium | Heavy |
|---|---|---|---|
| `general` | phi4-mini | qwen3:8b | _(cloud)_ |
| `code_review` | qwen2.5-coder:7b | qwen2.5-coder:7b | _(cloud)_ |
| `reasoning` | phi4-mini | deepseek-r1:8b | deepseek-r1:8b |
| `investigation` | phi4-mini | deepseek-r1:8b | _(cloud)_ |
| `safety` | granite3-guardian:2b | qwen3:8b | _(cloud)_ |
| `extraction` | phi4-mini | qwen2.5-coder:7b | qwen3:8b |
| `synthesis` | phi4-mini | qwen3:8b | _(cloud)_ |

To override a task class locally, pass `provider_config` in the call:

```python
{
    "question": "Review this Python auth handler for vulnerabilities",
    "task_class": "code_review",
    "data_policy": "local",
    "provider_config": {
        "light": "qwen2.5-coder:7b",
        "medium": "qwen2.5-coder:7b",
        "heavy": "qwen2.5-coder:7b"
    }
}
```

---

## Hardware Tiers

### 🟢 Standard — 16 GB RAM
Full lineup above. All models load into unified memory without swapping.
Run one 8B model + one 2B guard model simultaneously with headroom to spare.

### 🟡 Minimal — 8 GB RAM
```bash
ollama pull phi4-mini               # 2.3 GB
ollama pull qwen2.5-coder:7b        # 4.4 GB
ollama pull granite3-guardian:2b    # 2.5 GB
ollama pull qwen3-embedding:0.6b    # 0.6 GB
# ~9.8 GB disk, run one at a time
```

### 🔵 Full — 32 GB+ RAM (or GPU server)
Standard lineup plus the heavy code specialist:
```bash
ollama pull qwen3-coder:30b         # 17 GB — SOTA agentic coder, Claude Sonnet 4-class
```
Point `OLLAMA_BASE_URL` at your GPU server and set `DEEP_THINK_HEAVY_PROVIDER=ollama`
to route heavy passes there instead of to cloud.

---

## Notes

- `deepseek-r1:8b` is **DeepSeek-R1-0528-Qwen3-8B** — the May 2025 update that distills
  R1's reinforcement-learned reasoning onto the Qwen3-8B base. Not the older Qwen2.5 distill.
- `qwen3:8b` is the only model in this lineup with `tools` capability. Required for any
  agentic or MCP-tool-calling workflow.
- Both safety models are complementary: `granite3-guardian` covers enterprise risk categories
  (groundedness, bias, jailbreak); `llama-guard3` covers the MLCommons hazard taxonomy
  (violence, CSAM, weapons, elections, etc.).
- All models are Q4_K_M quantized via Ollama.
