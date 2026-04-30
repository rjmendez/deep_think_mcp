"""FastMCP server — exposes deep_think_async, get_thinking_result, list_thinking_jobs."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastmcp import FastMCP

from . import engine, store, worker

from . import discover as _discover

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app):
    store.init_db()
    # Only run Ollama discovery if at least one tier is configured to use it.
    # Avoids unnecessary network calls in cloud-only deployments.
    if _ollama_in_use():
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        asyncio.create_task(_discover.run_discovery(base_url, benchmark=True))
    else:
        log.info("No Ollama provider in use — skipping model discovery")
    task = asyncio.create_task(worker.worker_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _ollama_in_use() -> bool:
    """Return True if any provider tier resolves to Ollama."""
    cfg = engine.build_provider_config()
    return any(
        engine._tier_provider(cfg, tier) == "ollama"
        for tier in ("light", "medium", "heavy")
    )


mcp = FastMCP(
    "deep-think-mcp",
    instructions=(
        "Async multi-pass reasoning server. "
        "Call deep_think_async to start a reasoning job — it returns a job_id immediately. "
        "Call get_thinking_result(job_id) to poll for results. "
        "Jobs persist across server restarts. "
        "Provider and model are configured via environment variables; "
        "use provider_config to override provider or model IDs per call (no secrets in params)."
    ),
    lifespan=_lifespan,
)


@mcp.tool()
async def deep_think_async(
    question: str,
    passes: int = 3,
    task_class: str = "general",
    data_policy: str = "any",
    model: str = "",
    provider_config: Optional[dict] = None,
) -> dict:
    """Queue a multi-pass reasoning job and return a job_id immediately.

    The job runs asynchronously. Poll with get_thinking_result(job_id).

    Args:
        question:    The question or problem to reason about.
        passes:      Number of reasoning passes (2–6). Default 3.
        task_class:  Routing hint — picks specialist models and pass directives.
            "general"       Default reasoning (no routing). Safe default.
            "auto"          Run a lightweight classifier; apply result if confidence >= 0.75.
            "code_review"   Bug detection, security review. Uses qwen2.5-coder / gpt-5.2-codex.
            "investigation" Evidence weighing, IOC triage, incident response.
            "safety"        Risk detection, harm mapping. Runs granite3-guardian pre-check.
            "extraction"    Structured JSON output, entity extraction.
            "synthesis"     Writing, summarization, report drafting.
            "reasoning"     Complex logical / mathematical reasoning.
        data_policy: Controls which providers are allowed.
            "any"    (default) Use any configured provider including cloud.
            "local"  Ollama ONLY — never send data to cloud providers.
            "cloud"  Cloud providers preferred; Ollama only for light tier.
        model:           Override all tiers with a single model ID (shorthand).
        provider_config: Optional per-call overrides (no secrets — use env vars for those):
            provider        "anthropic" | "copilot" | "ollama"
            base_url        Ollama endpoint, e.g. "http://localhost:11434"
            model           Single model ID for all tiers
            light           Light-tier model ID override
            medium          Medium-tier model ID override
            heavy           Heavy-tier model ID override
            light_provider  Per-tier provider (e.g. "ollama" for cheap local)
            medium_provider Per-tier provider
            heavy_provider  Per-tier provider (e.g. "copilot" for synthesis)

    Provider secrets via environment variables only:
        ANTHROPIC_API_KEY              Anthropic API key
        GITHUB_COPILOT_OAUTH_TOKEN     GitHub Copilot OAuth token
        OLLAMA_BASE_URL                Ollama base URL (default: http://localhost:11434)
    """
    pc: dict = dict(provider_config or {})
    if model:
        pc.setdefault("model", model)
    if data_policy and data_policy != "any":
        pc["data_policy"] = data_policy

    cfg = engine.build_provider_config(pc)
    resolved_class = task_class if task_class in engine.TASK_CLASS_PROFILES else "general"
    summary = engine.model_summary(cfg, resolved_class)

    job_id = store.create_job(
        question=question,
        passes=max(2, min(passes, 6)),
        provider=cfg.provider,
        model_summary=summary,
        provider_config_json=json.dumps({**pc, "task_class": task_class, "data_policy": data_policy}),
    )

    return {
        "job_id": job_id,
        "status": "queued",
        "task_class": resolved_class,
        "data_policy": data_policy,
        "provider": cfg.provider,
        "model_summary": summary,
        "message": f"Call get_thinking_result('{job_id}') to poll for results.",
    }


@mcp.tool()
async def get_thinking_result(job_id: str) -> dict:
    """Poll a deep_think job for results.

    Returns status (queued → running → complete | failed),
    duration_secs once complete, and the full reasoning chain + final_answer.

    For fan_out jobs, also surfaces confidence_score, converged_claims, and
    contested_areas at the top level for easy inspection.
    """
    job = store.get_job(job_id)
    if not job:
        return {"error": f"No job found with job_id={job_id!r}"}

    duration = None
    if job.get("completed_at") and job.get("created_at"):
        try:
            from datetime import datetime

            start = datetime.fromisoformat(job["created_at"])
            end = datetime.fromisoformat(job["completed_at"])
            duration = int((end - start).total_seconds())
        except Exception:
            pass

    response: dict = {
        "job_id":        job["job_id"],
        "status":        job["status"],
        "provider":      job.get("provider"),
        "model_summary": job.get("model_summary"),
        "created_at":    job.get("created_at"),
        "duration_secs": duration,
    }

    if job["status"] == "complete" and job.get("result"):
        try:
            result = json.loads(job["result"])
            response["result"] = result
            # Surface fan-out convergence fields at the top level for convenience
            if isinstance(result, dict) and result.get("type") == "fan_out":
                if result.get("confidence_score") is not None:
                    response["confidence_score"] = result["confidence_score"]
                if result.get("converged_claims"):
                    response["converged_claims"] = result["converged_claims"]
                if result.get("contested_areas"):
                    response["contested_areas"] = result["contested_areas"]
        except (json.JSONDecodeError, TypeError):
            response["result"] = job["result"]
    elif job["status"] == "failed":
        response["error"] = job.get("error")

    return response


@mcp.tool()
async def discover_models(force: bool = False, benchmark: bool = True) -> dict:
    """Discover available models, benchmark their latency, and assign tiers.

    Runs automatically at server startup (non-blocking). Call this tool to:
      - See what models are available and how they were assigned to tiers
      - Force a re-benchmark after adding/removing Ollama models
      - Check benchmarked timeouts (conservative: benchmark × 8, min 45s, max 300s)

    Args:
        force:     Re-run even if the cache is fresh (< 24h old). Default False.
        benchmark: Actually measure latency per model. Set False for a fast inventory
                   without benchmarking (uses size-based heuristics only). Default True.

    Returns a summary of discovered models grouped by provider and tier.
    """
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    result = await _discover.run_discovery(base_url, force=force, benchmark=benchmark)

    # Build a clean summary for display
    by_provider: dict[str, list[dict]] = {}
    for m in result.models:
        entry = {
            "model_id":       m.model_id,
            "tier":           m.suggested_tier,
            "capabilities":   m.capabilities,
            "benchmark_ms":   m.benchmark_ms if m.benchmark_ms else "not measured",
            "timeout_secs":   m.timeout_secs,
        }
        if m.benchmark_ms:
            entry["size_b"] = m.size_b
        by_provider.setdefault(m.provider, []).append(entry)

    tier_summary = {
        provider: {"light": ta.light, "medium": ta.medium, "heavy": ta.heavy}
        for provider, ta in result.tier_assignments.items()
    }

    return {
        "from_cache":       result.from_cache,
        "completed_at":     result.completed_at,
        "discovery_secs":   round(result.discovery_secs, 1),
        "errors":           result.errors,
        "tier_assignments": tier_summary,
        "models":           by_provider,
    }


@mcp.tool()
async def deep_think_fan_out(
    question: str,
    width: int = 3,
    height: int = 2,
    task_class: str = "general",
    data_policy: str = "any",
    max_parallel: int = 2,
    provider_config: Optional[dict] = None,
) -> dict:
    """Queue a perspective fan-out reasoning job and return a job_id immediately.

    Runs `width` parallel agents each with a structurally different mandate (e.g. defense /
    prosecution / forensics), each doing `height` sequential reasoning passes. A final
    synthesis pass integrates all perspectives, identifying convergence (high confidence)
    and divergence (contested areas).

    Poll with get_thinking_result(job_id) until status is "complete".

    Args:
        question:     The question or content to analyze.
        width:        Parallel perspectives (1–6). Task class determines which mandates are used.
                      width=3 → first 3 mandates; width=6 → all 6.
        height:       Sequential passes per perspective (1–5). Each perspective runs this many
                      reasoning passes with its mandate injected into every prompt.
        task_class:   Selects the mandate set and specialist models.
            "investigation" → defense / prosecution / forensics / compliance / red_team / timeline
            "general"       → primary / adversarial / alternative / technical / risk / devils_advocate
            "code_review"   → correctness / security / performance / maintainability / api_contract / edge_cases
            "safety"        → harm_assessment / policy_compliance / mitigations / false_positives / context / legal
            "reasoning"     → formal / adversarial / constraints / alternative / verification / simplification
            "synthesis"     → structure / accuracy / clarity / completeness / audience / attribution
            "extraction"    → schema / completeness / disambiguation / confidence / validation / context
        data_policy:  "any" | "local" | "cloud"
        max_parallel: Max concurrent perspectives (default 2 — safe for Copilot Business
                      heavy-tier limits). Increase to 4 for Enterprise accounts.
        provider_config: Optional per-call model/provider overrides (no secrets).

    Total LLM calls = (width × height) + 1 synthesis pass.
    Example: width=3, height=2 → 7 total calls (6 perspective passes + 1 synthesis).
    """
    pc: dict = dict(provider_config or {})
    if data_policy and data_policy != "any":
        pc["data_policy"] = data_policy

    cfg = engine.build_provider_config(pc)
    resolved_class = task_class if task_class in engine.TASK_CLASS_PROFILES else "general"
    summary = engine.model_summary(cfg, resolved_class)

    # Clip to valid ranges
    width = max(1, min(width, 6))
    height = max(1, min(height, 5))
    total_calls = width * height + 1  # stored as passes for display

    job_id = store.create_job(
        question=question,
        passes=total_calls,
        provider=cfg.provider,
        model_summary=summary,
        provider_config_json=json.dumps({
            **pc,
            "task_class": task_class,
            "data_policy": data_policy,
            "fan_out": True,
            "width": width,
            "height": height,
            "max_parallel": max_parallel,
        }),
    )

    # List which perspectives will run
    mandates = engine.PERSPECTIVE_MANDATES.get(resolved_class, engine.PERSPECTIVE_MANDATES["general"])
    perspective_names = [m["name"] for m in mandates[:width]]

    return {
        "job_id": job_id,
        "status": "queued",
        "task_class": resolved_class,
        "width": width,
        "height": height,
        "total_llm_calls": total_calls,
        "perspectives": perspective_names,
        "data_policy": data_policy,
        "provider": cfg.provider,
        "model_summary": summary,
        "message": f"Call get_thinking_result('{job_id}') to poll for results.",
    }


@mcp.tool()
async def list_thinking_jobs(status: str = "all", limit: int = 10) -> dict:
    """List recent thinking jobs.

    Args:
        status: Filter by status — "all", "queued", "running", "complete", "failed".
        limit:  Max number of jobs to return (max 100).
    """
    jobs = store.list_jobs(status=status, limit=min(limit, 100))
    return {"count": len(jobs), "jobs": jobs}
