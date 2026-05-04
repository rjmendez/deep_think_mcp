"""FastMCP server for deep-think multi-pass reasoning.

This module implements the MCP (Model Context Protocol) server that exposes
deep_think reasoning as a callable tool. It manages job lifecycle, async
worker coordination, and persistence.

ENDPOINTS:
  POST /initialize           Start a new MCP session
  POST /call/deep_think_async  Queue a reasoning job (returns job_id)
  GET  /call/get_thinking_result  Poll job status and retrieve results
  GET  /call/list_thinking_jobs    List all jobs in database
  POST /self-improvement/implement Orchestrate code implementation from plan
  GET  /self-improvement/status    Get implementation status for a plan
  POST /self-improvement/deploy    Deploy validated code with canary rollout
  GET  /health               Health check with queue metrics

TOOLS EXPOSED:
  deep_think_async          Multi-pass reasoning (2-6 passes with different framings)
  get_thinking_result       Poll job status and retrieve full reasoning chain
  list_thinking_jobs        List jobs by status
  get_creative_metrics      Return creativity metrics for trend analysis

JOB FLOW:
  1. Client calls deep_think_async with question + passes
  2. Server queues job in SQLite (store.py)
  3. Worker process picks up job (worker.py)
  4. Worker calls engine.deep_think_passes for actual reasoning
  5. Client polls get_thinking_result to check status
  6. When complete, client receives full reasoning chain + answer

IMPLEMENTATION FLOW (Layer 5 Self-Improvement):
  1. Client calls POST /self-improvement/implement with plan_id
  2. ImplementationPipeline checks budget constraints
  3. Approval gates check severity (CRITICAL=manual, HIGH=owner, MEDIUM/LOW=auto)
  4. Feature branch created, code changes orchestrated through agents
  5. Commits tracked with Layer 5 tracer
  6. Status tracked in implementation_tasks table
  7. Client polls /self-improvement/status for progress
  8. POST /self-improvement/deploy triggers canary rollout if validation passes

DEPLOYMENT FLOW:
  1. Client calls POST /self-improvement/deploy with validation_id, plan_id, commit_sha
  2. Server validates: validation passed? rollback snapshot exists?
  3. DeploymentPipeline executes canary stages: 5% → 25% → 100%
  4. Monitors error rate, timeout rate, latency p99 at each stage
  5. If thresholds exceeded, automatic rollback triggered
  6. On success, tags release; on rollback, restores previous version

PERSISTENCE:
  - Jobs stored in SQLite (store.py) with status (queued/running/complete/failed)
  - Implementation tasks stored in implementation_tasks table
  - Reasoning chains kept in memory during execution
  - Failed jobs retain error logs for debugging

BACKGROUND TASKS:
  - Ollama model discovery (if OLLAMA_BASE_URL set) — runs in parallel on startup
  - Worker loop — continuously processes job queue (worker.py)"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

# Modular imports from new structure
from .engine import (
    build_provider_config,
    TASK_CLASS_PROFILES,
    model_summary,
    PERSPECTIVE_MANDATES,
    deep_think_passes,
    CREATIVE_MODES,
    get_metrics_snapshot,
)
from .engine.provider import _tier_provider
from . import store, worker, discover as _discover
from . import mqtt as mqtt_integration
from .engine.mqtt_tasks import MQTTEngineAdapter
from . import health
from .adversarial_testing.implementation_pipeline import ImplementationPipeline
from .planning_engine import PlanningEngine
from .adversarial_testing.validation_suite import ValidationSuite
from .adversarial_testing.metrics import MetricsCollector
from .adversarial_testing.deployment_pipeline import DeploymentPipeline
from .adversarial_testing import store as adversarial_store
from .verify.config import load_config as load_verify_config
from .verify.provider import CloudProvider, LocalProvider
from .verify.queue import VerifyJobQueue, VerifyWorker
from . import mcp_help

log = logging.getLogger(__name__)

# Global planning engine instance
_planning_engine: Optional[PlanningEngine] = None

# Global verification system instance
_verify_queue: Optional[VerifyJobQueue] = None
_verify_worker: Optional[VerifyWorker] = None
_cloud_provider: Optional[object] = None
_local_provider: Optional[object] = None



@asynccontextmanager
async def _lifespan(app):
    store.init_db()
    
    # [MQTT] Startup — initialize subscriber and processor
    await mqtt_integration.mqtt_startup()
    mqtt_integration.setup_signal_handlers()
    
    # Initialize advanced MQTT engine adapter
    mqtt_adapter = MQTTEngineAdapter(deep_think_fn=deep_think_passes)
    mqtt_initialized = await mqtt_adapter.start_mqtt()
    app.mqtt_adapter = mqtt_adapter
    mcp.mqtt_adapter = mqtt_adapter  # Expose to tools
    
    if mqtt_initialized:
        log.info("[MQTT] MQTTEngineAdapter initialized and running")
    
    # Initialize validation suite for self-improvement
    metrics_collector = MetricsCollector()
    validation_suite = ValidationSuite(
        metrics=metrics_collector,
        git_repo_root="/home/USER/development/deep_think_mcp",
        test_command="pytest --cov=adversarial_testing adversarial_testing/tests/",
    )
    app.validation_suite = validation_suite
    mcp.validation_suite = validation_suite  # Expose to tools
    log.info("ValidationSuite initialized for self-improvement")
    
    # Initialize planning engine
    global _planning_engine
    _planning_engine = PlanningEngine(deep_think_fn=deep_think_passes)
    app.planning_engine = _planning_engine
    mcp.planning_engine = _planning_engine
    log.info("PlanningEngine initialized for self-improvement")
    
    # Initialize verification system
    global _verify_queue, _verify_worker, _cloud_provider, _local_provider
    try:
        verify_config = load_verify_config()
        _verify_queue = VerifyJobQueue()
        
        # Initialize providers
        _cloud_provider = None
        _local_provider = None
        
        if verify_config.anthropic_api_key:
            try:
                _cloud_provider = CloudProvider(
                    api_key=verify_config.anthropic_api_key,
                    model=verify_config.anthropic_model,
                    timeout=verify_config.verify_cloud_timeout,
                )
                log.info("CloudProvider initialized")
            except Exception as e:
                log.warning("Failed to initialize CloudProvider: %s", e)
        else:
            log.info("ANTHROPIC_API_KEY not set, CloudProvider disabled")
        
        try:
            _local_provider = LocalProvider(
                url=verify_config.ollama_url,
                timeout=verify_config.verify_local_timeout,
            )
            log.info("LocalProvider initialized (Ollama at %s)", verify_config.ollama_url)
        except Exception as e:
            log.warning("Failed to initialize LocalProvider: %s", e)
        
        # Initialize worker
        _verify_worker = VerifyWorker(
            queue=_verify_queue,
            cloud_provider=_cloud_provider,
            local_provider=_local_provider,
            config=verify_config,
        )
        
        # Expose to mcp (only those that work)
        mcp.verify_queue = _verify_queue
        mcp.verify_worker = _verify_worker
        mcp.verify_config = verify_config
        
        # Start worker
        await _verify_worker.start()
        log.info("Verification system initialized")
    except Exception as e:
        log.error("Failed to initialize verification system: %s", e)
    
    discovery_task = None
    if _ollama_in_use():
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        discovery_task = asyncio.create_task(_discover.run_discovery(base_url, benchmark=True))
    else:
        log.info("No Ollama provider in use — skipping model discovery")
    worker_task = asyncio.create_task(worker.worker_loop())
    try:
        yield
    finally:
        # Shutdown verification worker
        if _verify_worker:
            await _verify_worker.stop()
        
        # [MQTT] Shutdown — gracefully stop subscriber and processor
        if mqtt_initialized:
            log.info("[MQTT] Shutting down MQTTEngineAdapter...")
            await mqtt_adapter.stop_mqtt()
        
        await mqtt_integration.mqtt_shutdown()
        
        for t in (discovery_task, worker_task):
            if t is None:
                continue
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


def _ollama_in_use() -> bool:
    """Return True if any provider tier resolves to Ollama."""
    cfg = build_provider_config()
    return any(
        _tier_provider(cfg, tier) == "ollama"
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
    verify: bool = False,
    # Fan-out (multi-perspective) reasoning
    width: int = 1,
    height: int = 1,
    extract_claims: bool = False,
    # Grounded reasoning parameters
    enable_research: bool = True,
    research_query: str = "",
    dama_node_id: str = "",
    dama_metric: str = "",
    web_domain_whitelist: Optional[list] = None,
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
            "adversarial"   Unconstrained challenge reasoning. Ollama-only, NO research tools.
            "research"      Grounded research. Full research tools, no abliteration models.
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
        verify:      If True, runs an extra heavy-tier re-traversal pass after the main passes
                     to check for gaps, contradictions, and unsupported claims (RYS verification).
        enable_research: If True and task_class permits, inject grounded research context.
        research_query:  Override query for research tools (defaults to question).
        dama_node_id:    DAMA device node ID for telemetry lookup (research/research_synthesis only).
        dama_metric:     DAMA metric name for telemetry lookup.
        web_domain_whitelist: Restrict web_search to specific domains (research only).

    Provider secrets via environment variables only:
        ANTHROPIC_API_KEY              Anthropic API key
        GITHUB_COPILOT_OAUTH_TOKEN     GitHub Copilot OAuth token
        OLLAMA_BASE_URL                Ollama base URL (default: http://localhost:11434)

    Response includes proof_chain field when research tools were used.
    """
    pc: dict = dict(provider_config or {})
    if model:
        pc.setdefault("model", model)
    if data_policy and data_policy != "any":
        pc["data_policy"] = data_policy

    # If width > 1, enable fan-out mode
    fan_out_enabled = width > 1
    if fan_out_enabled:
        # For fan-out, use height for passes per perspective, width for # perspectives
        total_passes = width * height + 1  # perspectives × passes + synthesis
    else:
        # Standard mode: passes param is the number of sequential passes
        total_passes = max(2, min(passes, 6))

    cfg = build_provider_config(pc)
    resolved_class = task_class if task_class in TASK_CLASS_PROFILES else "general"
    summary = model_summary(cfg, resolved_class)

    job_id = store.create_job(
        question=question,
        passes=total_passes,
        provider=cfg.provider,
        model_summary=summary,
        provider_config_json=json.dumps({
            **pc,
            "task_class": task_class,
            "data_policy": data_policy,
            "verify": verify,
            "enable_research": enable_research,
            "research_query": research_query,
            "dama_node_id": dama_node_id,
            "dama_metric": dama_metric,
            "web_domain_whitelist": web_domain_whitelist or [],
            # Fan-out config (if width > 1)
            "fan_out": fan_out_enabled,
            "width": width if fan_out_enabled else 1,
            "height": height if fan_out_enabled else 1,
            "extract_claims": extract_claims,
        }),
    )

    response = {
        "job_id": job_id,
        "status": "queued",
        "task_class": resolved_class,
        "data_policy": data_policy,
        "provider": cfg.provider,
        "model_summary": summary,
        "research_enabled": enable_research and resolved_class not in ("adversarial",),
    }
    
    if fan_out_enabled:
        response["fan_out"] = True
        response["width"] = width
        response["height"] = height
        response["message"] = f"Fan-out job with {width} perspectives × {height} passes. Call get_thinking_result('{job_id}') to poll."
    else:
        response["message"] = f"Call get_thinking_result('{job_id}') to poll for results."
    
    return response


@mcp.tool()
async def get_thinking_result(job_id: str, include_reasoning_chain: bool = False) -> dict:
    """Poll a deep_think job for results.

    Returns status (queued → running → complete | failed),
    duration_secs once complete, and the full reasoning chain + final_answer.

    For fan_out jobs, also surfaces confidence_score, converged_claims,
    contested_areas, and claim_sets at the top level for easy inspection.

    Args:
        job_id:                  The job_id returned by deep_think_async or deep_think_fan_out.
        include_reasoning_chain: If True, attach the full intermediate pass outputs from
                                 pass_cache as a "reasoning_chain" field — one entry per
                                 perspective (or "main" for standard jobs), each with an
                                 ordered list of passes (framing, tier, model, provider,
                                 output). Useful for forensic review and debugging.
                                 Default False to keep normal poll responses compact.
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
                if result.get("claim_sets"):
                    response["claim_sets"] = result["claim_sets"]
                response["adaptive_triggered"] = result.get("adaptive_triggered", False)
                if result.get("adaptive_triggered"):
                    response["adaptive_reason"] = result.get("adaptive_reason", "")
                    response["final_width"] = result.get("final_width")
            # Surface verification_pass at the top level for deep_think jobs
            if isinstance(result, dict) and result.get("verification_pass") is not None:
                response["verification_pass"] = result["verification_pass"]
            # Surface Nova fact-check fields
            if isinstance(result, dict):
                if result.get("verification_results") is not None:
                    response["verification_results"] = result["verification_results"]
                if result.get("adjusted_final_confidence") is not None:
                    response["adjusted_final_confidence"] = result["adjusted_final_confidence"]
                if result.get("verification_summary") is not None:
                    response["verification_summary"] = result["verification_summary"]
                if result.get("escalated_claim_ids"):
                    response["escalated_claim_ids"] = result["escalated_claim_ids"]
        except (json.JSONDecodeError, TypeError):
            response["result"] = job["result"]
    elif job["status"] == "failed":
        response["error"] = job.get("error")

    if include_reasoning_chain:
        response["reasoning_chain"] = store.get_full_reasoning_chain(job_id)

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
    max_parallel: Optional[int] = None,
    max_width: Optional[int] = None,
    confidence_threshold: Optional[int] = None,
    extract_claims: bool = False,
    provider_config: Optional[dict] = None,
) -> dict:
    """Queue a perspective fan-out reasoning job and return a job_id immediately.

    Runs `width` parallel agents each with a structurally different mandate (e.g. defense /
    prosecution / forensics), each doing `height` sequential reasoning passes. A final
    synthesis pass integrates all perspectives, identifying convergence (high confidence)
    and divergence (contested areas).

    If synthesis confidence_score < confidence_threshold (default 50) OR contested_areas > 2,
    automatically dispatches remaining unused perspective mandates and re-synthesizes with all
    outputs (adaptive width expansion — DAMA sampling_factor analog). Limited to 1 expansion
    to cap API spend.

    Poll with get_thinking_result(job_id) until status is "complete".

    Args:
        question:             The question or content to analyze.
        width:                Parallel perspectives (1–6). Task class determines which mandates are used.
                              width=3 → first 3 mandates; width=6 → all 6.
        height:               Sequential passes per perspective (1–5). Each perspective runs this many
                              reasoning passes with its mandate injected into every prompt.
        task_class:           Selects the mandate set and specialist models.
            "investigation" → defense / prosecution / forensics / compliance / red_team / timeline
            "general"       → primary / adversarial / alternative / technical / risk / devils_advocate
            "code_review"   → correctness / security / performance / maintainability / api_contract / edge_cases
            "safety"        → harm_assessment / policy_compliance / mitigations / false_positives / context / legal
            "reasoning"     → formal / adversarial / constraints / alternative / verification / simplification
            "synthesis"     → structure / accuracy / clarity / completeness / audience / attribution
            "extraction"    → schema / completeness / disambiguation / confidence / validation / context
        data_policy:          "any" | "local" | "cloud"
        max_parallel:         Max concurrent perspectives (default 2 — safe for Copilot Business
                              heavy-tier limits). Increase to 4 for Enterprise accounts.
        max_width:            Upper bound on total perspectives after adaptive expansion (default 6).
        confidence_threshold: Trigger adaptive expansion when confidence_score < this value (default 50).
        extract_claims:       If True, distil each perspective's prose into a structured claim set
                              (light-tier model) before synthesis. Reduces synthesis context ~10-20×.
                              Default False.
        provider_config: Optional per-call model/provider overrides (no secrets).

    Total LLM calls = (width × height) + 1 synthesis pass (+ adaptive expansion if triggered).
    Example: width=3, height=2 → 7 total calls (6 perspective passes + 1 synthesis).
    """
    # Apply defaults for optional params only
    max_parallel = max_parallel if max_parallel is not None else 2
    max_width = max_width if max_width is not None else 6
    confidence_threshold = confidence_threshold if confidence_threshold is not None else 50
    
    pc: dict = dict(provider_config or {})
    if data_policy and data_policy != "any":
        pc["data_policy"] = data_policy

    cfg = build_provider_config(pc)
    resolved_class = task_class if task_class in TASK_CLASS_PROFILES else "general"
    summary = model_summary(cfg, resolved_class)

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
            "max_width": max_width,
            "confidence_threshold": confidence_threshold,
            "extract_claims": extract_claims,
        }),
    )

    # List which perspectives will run
    mandates = PERSPECTIVE_MANDATES.get(resolved_class, PERSPECTIVE_MANDATES["general"])
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


@mcp.tool()
async def mqtt_health() -> dict:
    """Get MQTT engine health status and metrics.
    
    Returns:
        Health status, circuit breaker state, message counts, error logs, and connection status.
    """
    if not hasattr(mcp, "mqtt_adapter"):
        return {
            "status": "not_initialized",
            "message": "MQTT adapter not initialized (MQTT_ENABLE=false?)"
        }
    
    adapter = mcp.mqtt_adapter
    return adapter.get_health()


@mcp.tool()
async def mqtt_metrics() -> dict:
    """Get detailed MQTT metrics for monitoring and observability.
    
    Returns:
        Messages received/published, deep_think runs, failures, circuit breaker trips, etc.
    """
    if not hasattr(mcp, "mqtt_adapter"):
        return {
            "status": "not_initialized",
            "metrics": {}
        }
    
    adapter = mcp.mqtt_adapter
    health = adapter.get_health()
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "circuit_breaker_state": health["circuit_breaker"],
        "metrics": health["metrics"],
        "connections": health["connections"],
    }


@mcp.tool()
async def deep_think_creative(
    question: str,
    mode: str = "lateral-thinking",
    passes: int = 4,
    data_policy: str = "any",
    model: str = "",
    provider_config: Optional[dict] = None,
    verify_with_nova: bool = False,
) -> dict:
    """Queue a high-temperature creative reasoning job and return a job_id immediately.

    Runs multi-pass creative reasoning with dynamic temperature scheduling and
    mode-specific prompt templates that evolve progressively across passes.

    Poll with get_thinking_result(job_id) to check status and retrieve results.

    Args:
        question:         The problem or question to explore creatively.
        mode:             Creative reasoning mode:
            "lateral-thinking"  Sideways problem solving, constraint-violation exploration.
            "blue-sky"          Unconstrained ideation, "what if" scenarios.
            "socratic"          Questioning assumptions, dialectical exploration.
            "evolutionary"      Iterative idea building; temperature decreases across passes.
        passes:           Number of reasoning passes (2–6, default 4).
        data_policy:      "any" | "local" | "cloud"
        model:            Override all tiers with a single model ID (shorthand).
        provider_config:  Optional per-call overrides (no secrets — use env vars for those).
        verify_with_nova: If True, verify the best-scoring pass against Nova's /pre_action.

    Temperature schedule (automatic):
        Passes 1-2:  0.8–1.0  (high exploration)
        Passes 3-4:  0.6–0.7  (medium refinement)
        Final pass:  0.3–0.5  (validation / convergence)
        Dynamic adjustment ±0.05 based on novelty score feedback.

    Quality metrics returned per pass:
        novelty_score    (0-1): divergence from conventional reasoning
        feasibility_score (0-1): implementability / realism
        impact_score      (0-1): potential significance
        combined_score    = novelty × feasibility × impact
    """
    if mode not in CREATIVE_MODES:
        return {
            "error": f"Unknown creative mode '{mode}'. Valid modes: {list(CREATIVE_MODES)}",
        }

    pc: dict = dict(provider_config or {})
    if model:
        pc.setdefault("model", model)
    if data_policy and data_policy != "any":
        pc["data_policy"] = data_policy

    cfg = build_provider_config(pc)
    summary = model_summary(cfg, "general")
    passes = max(2, min(passes, 6))

    job_id = store.create_job(
        question=question,
        passes=passes,
        provider=cfg.provider,
        model_summary=summary,
        provider_config_json=json.dumps({
            **pc,
            "creative":        True,
            "creative_mode":   mode,
            "creative_passes": passes,
            "data_policy":     data_policy,
            "verify_with_nova": verify_with_nova,
        }),
    )

    return {
        "job_id":        job_id,
        "status":        "queued",
        "mode":          mode,
        "passes":        passes,
        "data_policy":   data_policy,
        "provider":      cfg.provider,
        "model_summary": summary,
        "temperature_schedule": {
            "passes_1_2": "0.8–1.0 (high exploration)",
            "passes_3_4": "0.6–0.7 (medium refinement)",
            "final_pass": "0.3–0.5 (validation)",
        },
        "message": f"Call get_thinking_result('{job_id}') to poll for results.",
    }


@mcp.tool()
async def get_creative_metrics() -> dict:
    """Return accumulated creativity metrics for trend analysis.

    Metrics are tracked in-process across all creative reasoning jobs run
    in this server session. Useful for understanding which modes and ideas
    tend to score highest on novelty, feasibility, and impact.

    Returns:
        total_jobs, total_passes, verified_passes, rolling averages per dimension,
        per-mode job counts, and per-mode average combined scores.
    """
    return get_metrics_snapshot()


@mcp.custom_route("/self-improvement/implement", methods=["POST"])
async def implement_plan(request: Request) -> JSONResponse:
    """Orchestrate code implementation from a planning engine output.
    
    Executes the full implementation pipeline:
    - Check budget before starting
    - Queue for human approval if severity requires it (CRITICAL, HIGH)
    - Create feature branch
    - Orchestrate code-review agent → planning agent → implementation agent
    - Commit changes with Layer 5 tracer
    - Track status in implementation_tasks table
    - Create rollback snapshots
    
    Request body:
    {
        "plan_id": str,         # ID from planning_engine output
        "skip_approval": bool   # (optional) bypass human review gates
    }
    
    Response:
    {
        "success": bool,
        "plan_id": str,
        "branch_name": str,
        "commit_sha": str,
        "status": str,
        "message": str,
        "error": str (if failed)
    }
    """
    try:
        body = await request.json()
        plan_id = body.get("plan_id")
        skip_approval = body.get("skip_approval", False)
        
        if not plan_id:
            return JSONResponse(
                {
                    "success": False,
                    "error": "Missing required field: plan_id",
                    "status": "error",
                },
                status_code=400,
            )
        
        # Initialize pipeline
        pipeline = ImplementationPipeline()
        
        # Start implementation
        success, error_msg = await pipeline.start_implementation(
            plan_id=plan_id,
            skip_approval=skip_approval,
        )
        
        if not success:
            return JSONResponse(
                {
                    "success": False,
                    "plan_id": plan_id,
                    "status": "failed",
                    "error": error_msg,
                },
                status_code=400,
            )
        
        # Get updated status
        status = await pipeline.get_implementation_status(plan_id)
        
        return JSONResponse(
            {
                "success": True,
                "plan_id": plan_id,
                "status": status.get("status") if status else "implementing",
                "commit_sha": status.get("commit_sha") if status else None,
                "message": "Implementation started successfully. Poll status endpoint for updates.",
            },
            status_code=200,
        )
    
    except Exception as e:
        log.exception("Implementation failed")
        return JSONResponse(
            {
                "success": False,
                "status": "error",
                "error": f"Implementation exception: {str(e)}",
            },
            status_code=500,
        )


@mcp.custom_route("/self-improvement/status", methods=["GET"])
async def get_implementation_status_endpoint(request: Request) -> JSONResponse:
    """Get current implementation status for a plan.
    
    Query parameters:
    - plan_id: ID of the plan to check status for
    
    Response:
    {
        "plan_id": str,
        "status": str,
        "commit_sha": str,
        "tasks": list,
        "created_at": str
    }
    """
    try:
        plan_id = request.query_params.get("plan_id")
        
        if not plan_id:
            return JSONResponse(
                {
                    "error": "Missing required parameter: plan_id",
                    "status": "error",
                },
                status_code=400,
            )
        
        pipeline = ImplementationPipeline()
        status = await pipeline.get_implementation_status(plan_id)
        
        if not status:
            return JSONResponse(
                {
                    "error": f"Plan {plan_id} not found",
                    "status": "error",
                },
                status_code=404,
            )
        
        return JSONResponse(status, status_code=200)
    
    except Exception as e:
        log.exception("Status check failed")
        return JSONResponse(
            {
                "error": f"Status check exception: {str(e)}",
                "status": "error",
            },
            status_code=500,
        )


@mcp.tool()
async def generate_self_improvement_plan(
    findings: list[dict],
    limit: int = 5,
) -> dict:
    """Generate ranked improvement plans for findings using deep_think planning.
    
    Analyzes findings, computes priority scores based on severity/impact/effort,
    and generates structured improvement plans using deep_think with task_class="planning".
    
    Args:
        findings: List of finding dicts with keys:
            - id: unique finding identifier
            - severity: CRITICAL|HIGH|MEDIUM|LOW
            - impact: 0-10 numeric impact score
            - reproducibility: 0-1 likelihood of reproducing
            - category: finding category/type
            - description: brief description
            - details: full context/stack trace
            - effort_estimate: estimated days (1-5)
            - risk_level: LOW|MEDIUM|HIGH
        limit: Max number of plans to generate (default 5)
    
    Returns:
        {
            "status": "success"|"error",
            "plans": [
                {
                    "plan_id": uuid,
                    "finding_id": str,
                    "priority": float,
                    "effort_estimate": int,
                    "risk_level": str,
                    "status": "pending",
                    "created_at": iso8601,
                }
            ],
            "error": optional error message,
            "metrics": {
                "total_plans": int,
                "avg_priority": float,
                "total_effort_days": int,
                "generation_time_secs": float,
            }
        }
    """
    global _planning_engine
    if not _planning_engine:
        return {
            "status": "error",
            "error": "Planning engine not initialized",
            "plans": [],
        }
    
    try:
        start_time = time.time()
        
        # Validate findings input
        if not findings or not isinstance(findings, list):
            return {
                "status": "error",
                "error": "findings must be a non-empty list",
                "plans": [],
            }
        
        # Limit input size
        findings = findings[:limit * 2]
        
        log.info(f"Generating plans for {len(findings)} findings (limit={limit})")
        
        # Generate plans concurrently
        plans = await _planning_engine.generate_plans_for_findings(
            findings=findings,
            limit=limit,
        )
        
        # Compute metrics
        total_effort = sum(p.get("effort_estimate", 0) for p in plans)
        avg_priority = (
            sum(p.get("priority", 0) for p in plans) / len(plans)
            if plans else 0
        )
        
        elapsed = time.time() - start_time
        
        return {
            "status": "success",
            "plans": plans,
            "metrics": {
                "total_plans": len(plans),
                "avg_priority": round(avg_priority, 2),
                "total_effort_days": total_effort,
                "generation_time_secs": round(elapsed, 2),
            },
        }
    
    except Exception as e:
        log.error(f"Failed to generate plans: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e),
            "plans": [],
        }


@mcp.tool()
async def get_pending_improvement_plans() -> dict:
    """List all pending self-improvement plans awaiting approval.
    
    Returns:
        {
            "status": "success",
            "plans": [
                {
                    "plan_id": str,
                    "finding_ids": [str],
                    "priority": float,
                    "effort_estimate": int,
                    "risk_level": str,
                    "status": str,
                    "created_at": iso8601,
                }
            ]
        }
    """
    global _planning_engine
    if not _planning_engine:
        return {
            "status": "error",
            "error": "Planning engine not initialized",
            "plans": [],
        }
    
    try:
        plans = await _planning_engine.get_pending_plans()
        return {
            "status": "success",
            "plans": plans,
        }
    except Exception as e:
        log.error(f"Failed to fetch pending plans: {e}")
        return {
            "status": "error",
            "error": str(e),
            "plans": [],
        }


@mcp.tool()
async def approve_improvement_plan(
    plan_id: str,
    approved_by: str,
    approval_notes: str = "",
) -> dict:
    """Approve a pending improvement plan for implementation.
    
    Args:
        plan_id: UUID of plan to approve
        approved_by: Name/email of approver
        approval_notes: Optional approval notes/justification
    
    Returns:
        {"status": "success"|"error", "message": str}
    """
    global _planning_engine
    if not _planning_engine:
        return {
            "status": "error",
            "message": "Planning engine not initialized",
        }
    
    try:
        success = await _planning_engine.approve_plan(
            plan_id=plan_id,
            approved_by=approved_by,
            approval_notes=approval_notes,
        )
        
        if success:
            return {
                "status": "success",
                "message": f"Plan {plan_id} approved",
            }
        else:
            return {
                "status": "error",
                "message": f"Failed to approve plan {plan_id}",
            }
    
    except Exception as e:
        log.error(f"Failed to approve plan {plan_id}: {e}")
        return {
            "status": "error",
            "message": str(e),
        }


@mcp.custom_route("/self-improvement/deploy", methods=["POST"])
async def deploy_validated_code(request: Request) -> JSONResponse:
    """Deploy validated code through canary rollout with automatic rollback.
    
    Executes Layer 5 Self-Improvement System deployment pipeline:
    - Stage 1: 5% traffic (1 pod replica) for 30 seconds
    - Stage 2: 25% traffic (multi-replica) for 2 minutes  
    - Stage 3: 100% traffic (full rollout) for 5 minutes
    
    Monitors metrics at each stage:
    - Error rate spike > 2% triggers rollback
    - Timeout rate > 1% triggers rollback
    - Latency p99 > 5s triggers rollback
    
    If any threshold violated, automatically rollback to previous stable version.
    If all stages pass, tag release and update deployment_events table.
    
    Request body:
    {
        "validation_id": str,  # validation_results.id from validation_suite
        "plan_id": str,        # self_improvement_plans.id
        "commit_sha": str      # git commit SHA to deploy
    }
    
    Response:
    {
        "success": bool,
        "deployment_id": str,
        "status": str,  # "completed" or "rolled_back"
        "details": dict
    }
    """
    try:
        body = await request.json()
        validation_id = body.get("validation_id")
        plan_id = body.get("plan_id")
        commit_sha = body.get("commit_sha")
        
        if not all([validation_id, plan_id, commit_sha]):
            return JSONResponse(
                {
                    "success": False,
                    "error": "Missing required fields: validation_id, plan_id, commit_sha"
                },
                status_code=400
            )
        
        # Initialize adversarial store with custom db path if provided
        adversarial_db = os.getenv(
            "ADVERSARIAL_DB",
            str(__import__("pathlib").Path.home() / ".deep_think" / "adversarial.db"),
        )
        store_instance = adversarial_store.AdversarialStore(adversarial_db)
        
        # Pre-flight checks
        # 1. Verify validation passed
        validation_result = store_instance.execute(
            "SELECT status FROM validation_results WHERE id = ?",
            (validation_id,)
        ).fetchone()
        
        if not validation_result or validation_result["status"] != "passed":
            return JSONResponse(
                {
                    "success": False,
                    "error": f"Validation {validation_id} did not pass or does not exist"
                },
                status_code=400
            )
        
        # 2. Verify plan exists and has deployment info
        plan_result = store_instance.execute(
            "SELECT deployment_sha FROM self_improvement_plans WHERE id = ?",
            (plan_id,)
        ).fetchone()
        
        if not plan_result:
            return JSONResponse(
                {
                    "success": False,
                    "error": f"Plan {plan_id} not found"
                },
                status_code=404
            )
        
        # Initialize deployment pipeline
        metrics = MetricsCollector()
        prometheus_endpoint = os.getenv(
            "PROMETHEUS_ENDPOINT", "http://localhost:9090"
        )
        
        pipeline = DeploymentPipeline(
            store=store_instance,
            metrics=metrics,
            prometheus_endpoint=prometheus_endpoint,
            k3s_namespace=os.getenv("K3S_NAMESPACE", "agents"),
            deployment_name=os.getenv("DEPLOYMENT_NAME", "deep-think"),
        )
        
        # Execute deployment
        success, error_msg, details = await pipeline.deploy_validated_fix(
            plan_id=plan_id,
            commit_sha=commit_sha,
        )
        
        return JSONResponse(
            {
                "success": success,
                "error": error_msg,
                "deployment_id": details.get("deployment_id"),
                "status": details.get("status"),
                "details": details,
            },
            status_code=200 if success else 400
        )
    
    except json.JSONDecodeError:
        return JSONResponse(
            {"success": False, "error": "Invalid JSON in request body"},
            status_code=400
        )
    except Exception as e:
        log.error(f"Deployment endpoint error: {e}", exc_info=True)
        return JSONResponse(
            {"success": False, "error": f"Internal server error: {str(e)}"},
            status_code=500
        )



@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint with queue metrics.
    
    Returns HTTP 200 if healthy, 503 if degraded (too many pending jobs).
    
    Response includes:
    - status: "healthy" or "degraded"
    - pending_count: number of queued jobs
    - avg_latency: average job duration in seconds
    - last_success_timestamp: when the last job completed
    - worker_count: number of active workers
    - db_status: database connectivity status
    - completed_count: total completed jobs
    
    Response time: <100ms (uses cached metrics)
    """
    max_pending = int(os.getenv("DEEP_THINK_HEALTH_MAX_PENDING", "100"))
    metrics = health.get_health_metrics(store._connect, max_pending)
    
    http_status = metrics.pop("http_status", 200)
    return JSONResponse(metrics, status_code=http_status)


@mcp.custom_route("/self-improvement/validate", methods=["POST"])
async def validate_implementation(request: Request) -> JSONResponse:
    """Validate implementation with before/after metric comparison and regression detection.
    
    Accepts:
        implementation_id: ID from implementation_pipeline output (commit SHA)
        plan_id: ID of the self-improvement plan
    
    Returns:
        - passed: bool indicating if validation passed
        - improvement_score: 0-1 scale
        - before_metrics: snapshot before implementation
        - after_metrics: snapshot after implementation  
        - regressions: list of detected regressions
        - test_output: pytest output
        - validation_id: ID of validation record
    
    HTTP 200: Validation completed (check 'passed' field)
    HTTP 400: Missing required fields
    HTTP 500: Validation error
    """
    try:
        data = await request.json()
    except Exception as e:
        return JSONResponse(
            {"error": f"Invalid JSON: {str(e)}", "status": "error"},
            status_code=400,
        )
    
    implementation_id = data.get("implementation_id")
    plan_id = data.get("plan_id")
    
    if not implementation_id or not plan_id:
        return JSONResponse(
            {
                "error": "Missing required fields: implementation_id, plan_id",
                "status": "error",
            },
            status_code=400,
        )
    
    try:
        validation_suite = mcp.validation_suite
        passed, error_msg, validation_details = await validation_suite.validate_implementation(
            plan_id=plan_id,
            commit_sha=implementation_id,
        )
        
        return JSONResponse(
            {
                "status": "completed",
                "passed": passed,
                "error": error_msg,
                **validation_details,
            },
            status_code=200,
        )
    except Exception as e:
        log.exception("Validation failed")
        return JSONResponse(
            {
                "error": f"Validation exception: {str(e)}",
                "status": "error",
            },
            status_code=500,
        )


@mcp.custom_route("/verify", methods=["POST"])
async def verify_sync(request: Request):
    """Synchronous claim verification endpoint.
    
    Request body:
    {
        "claim": "string",
        "context": "optional string",
        "provider": "cloud|local"  # defaults to "cloud"
    }
    
    Response:
    {
        "verdict": true/false,
        "confidence": 0.0-1.0,
        "reasoning": "string",
        "latency_ms": 1234
    }
    """
    try:
        body = await request.json()
        claim = body.get("claim", "").strip()
        context = body.get("context", "").strip() or None
        provider = body.get("provider", "cloud")

        if not claim:
            return JSONResponse(
                {"error": "Missing required field: claim"},
                status_code=400,
            )

        if provider not in ("cloud", "local"):
            return JSONResponse(
                {"error": "Invalid provider (must be 'cloud' or 'local')"},
                status_code=400,
            )

        # Get the appropriate provider
        if provider == "cloud":
            if not _cloud_provider:
                return JSONResponse(
                    {
                        "error": "Cloud provider not available (missing ANTHROPIC_API_KEY?)"
                    },
                    status_code=503,
                )
            prov = _cloud_provider
            timeout = mcp.verify_config.verify_cloud_timeout
        else:
            if not _local_provider:
                return JSONResponse(
                    {
                        "error": "Local provider not available (Ollama not running?)"
                    },
                    status_code=503,
                )
            prov = _local_provider
            timeout = mcp.verify_config.verify_local_timeout

        try:
            result = await asyncio.wait_for(
                prov.verify_claim(claim, context),
                timeout=timeout,
            )
            return JSONResponse(result.to_dict(), status_code=200)
        except asyncio.TimeoutError:
            return JSONResponse(
                {"error": f"Verification timed out after {timeout}s"},
                status_code=504,
            )

    except json.JSONDecodeError:
        return JSONResponse(
            {"error": "Invalid JSON request"},
            status_code=400,
        )
    except Exception as e:
        log.exception("Verification failed")
        return JSONResponse(
            {"error": f"Verification failed: {str(e)}"},
            status_code=500,
        )


@mcp.custom_route("/verify-async", methods=["POST"])
async def verify_async(request: Request):
    """Queue an asynchronous claim verification job.
    
    Request body:
    {
        "claim": "string",
        "context": "optional string",
        "provider": "cloud|local"  # defaults to "cloud"
    }
    
    Response:
    {
        "job_id": "uuid",
        "status_url": "/verify-status/{job_id}"
    }
    """
    try:
        body = await request.json()
        claim = body.get("claim", "").strip()
        context = body.get("context", "").strip() or None
        provider = body.get("provider", "cloud")

        if not claim:
            return JSONResponse(
                {"error": "Missing required field: claim"},
                status_code=400,
            )

        if provider not in ("cloud", "local"):
            return JSONResponse(
                {"error": "Invalid provider (must be 'cloud' or 'local')"},
                status_code=400,
            )

        if not mcp.verify_queue:
            return JSONResponse(
                {"error": "Verification queue not available"},
                status_code=503,
            )

        job_id = mcp.verify_queue.create_job(claim, provider, context)

        return JSONResponse(
            {
                "job_id": job_id,
                "status_url": f"/verify-status/{job_id}",
            },
            status_code=202,
        )

    except json.JSONDecodeError:
        return JSONResponse(
            {"error": "Invalid JSON request"},
            status_code=400,
        )
    except Exception as e:
        log.exception("Queue failed")
        return JSONResponse(
            {"error": f"Failed to queue verification: {str(e)}"},
            status_code=500,
        )


@mcp.custom_route("/verify-status/{job_id}", methods=["GET"])
async def verify_status(request: Request):
    """Get status of an asynchronous verification job.
    
    Response:
    {
        "job_id": "uuid",
        "status": "queued|processing|done|failed",
        "result": { /* VerifyResult */ } or null,
        "error": "error message" or null,
        "created_at": "ISO timestamp",
        "started_at": "ISO timestamp" or null,
        "completed_at": "ISO timestamp" or null
    }
    """
    try:
        job_id = request.path_params.get("job_id", "")

        if not mcp.verify_queue:
            return JSONResponse(
                {"error": "Verification queue not available"},
                status_code=503,
            )

        status = mcp.verify_queue.get_status(job_id)

        if not status:
            return JSONResponse(
                {"error": f"Job not found: {job_id}"},
                status_code=404,
            )

        return JSONResponse(status, status_code=200)

    except Exception as e:
        log.exception("Status lookup failed")
        return JSONResponse(
            {"error": f"Status lookup failed: {str(e)}"},
            status_code=500,
        )


@mcp.custom_route("/health/hints", methods=["GET"])
async def health_with_hints(request: Request) -> JSONResponse:
    """Health check endpoint with actionable hints for common issues.
    
    Response includes:
    - status: "healthy" or "degraded"
    - queue_depth: number of verification jobs queued
    - processing: number of jobs currently processing
    - completed: total completed jobs
    - hints: list of actionable recommendations based on metrics
    
    Example response:
    {
        "status": "healthy",
        "queue_depth": 5,
        "processing": 2,
        "completed": 150,
        "hints": [
            "System operating normally"
        ]
    }
    """
    try:
        # Get verification queue metrics
        verify_metrics = {}
        if _verify_queue:
            verify_metrics = _verify_queue.get_metrics()
        else:
            verify_metrics = {
                "queue_depth": 0,
                "processing": 0,
                "completed": 0,
                "failed": 0,
                "avg_latency": None,
                "completion_rate": 0,
            }
        
        queue_depth = verify_metrics.get("queue_depth", 0)
        processing = verify_metrics.get("processing", 0)
        completed = verify_metrics.get("completed", 0)
        failed = verify_metrics.get("failed", 0)
        avg_latency = verify_metrics.get("avg_latency")
        completion_rate = verify_metrics.get("completion_rate", 0)
        
        # Generate hints using mcp_help module
        hints = mcp_help.generate_hints(verify_metrics)
        
        status = "degraded" if len(hints) > 1 else "healthy"
        http_status = 503 if status == "degraded" else 200
        
        return JSONResponse(
            {
                "status": status,
                "queue_depth": queue_depth,
                "processing": processing,
                "completed": completed,
                "failed": failed,
                "avg_latency": avg_latency,
                "completion_rate": completion_rate,
                "hints": hints,
            },
            status_code=http_status,
        )
    
    except Exception as e:
        log.exception("Health hints endpoint error")
        return JSONResponse(
            {"error": f"Failed to get health status: {str(e)}", "hints": []},
            status_code=500,
        )



@mcp.custom_route("/capabilities", methods=["GET"])
async def get_capabilities(request: Request) -> JSONResponse:
    """List available reasoning capabilities and configurations.
    
    Response includes:
    - passes: [2, 3, 4, 5, 6] - available pass counts
    - task_classes: available reasoning modes
    - providers: configured providers with available models
    - latency_estimates: estimated latency per pass count and provider
    
    Example response:
    {
        "passes": [2, 3, 4, 5, 6],
        "task_classes": [
            "general", "code_review", "investigation", "safety", "extraction",
            "synthesis", "reasoning", "data_governance", "research_synthesis"
        ],
        "providers": {
            "anthropic": {
                "available": true,
                "models": ["claude-opus-4-1-20250805", "claude-sonnet-4-20250514"]
            },
            "ollama": {
                "available": true,
                "url": "http://localhost:11434",
                "models": ["phi4-mini:latest", "qwen3.5:27b", "qwen2.5-coder:7b"]
            }
        },
        "latency_estimates": {
            "2_passes_cloud": "15-30s",
            "3_passes_cloud": "30-60s",
            "2_passes_local": "10-20s"
        }
    }
    """
    try:
        cfg = build_provider_config()
        
        # Get list of task classes from TASK_CLASS_PROFILES
        task_classes = list(TASK_CLASS_PROFILES.keys())
        
        # Check provider availability
        providers = {}
        
        # Check Anthropic/Copilot availability
        if os.getenv("ANTHROPIC_API_KEY") or os.getenv("GITHUB_COPILOT_OAUTH_TOKEN"):
            providers["anthropic"] = {
                "available": True,
                "models": [
                    "claude-opus-4-1-20250805",
                    "claude-sonnet-4-20250514",
                    "claude-opus-4-1",
                ]
            }
            providers["copilot"] = {
                "available": True,
                "models": ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"]
            }
        else:
            providers["anthropic"] = {"available": False, "models": []}
            providers["copilot"] = {"available": False, "models": []}
        
        # Check Ollama availability
        ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        ollama_available = False
        ollama_models = []
        
        try:
            import requests
            health = requests.get(f"{ollama_url}/api/tags", timeout=2)
            if health.status_code == 200:
                ollama_available = True
                data = health.json()
                ollama_models = [m.get("name", "") for m in data.get("models", [])][:5]
        except Exception:
            pass
        
        providers["ollama"] = {
            "available": ollama_available,
            "url": ollama_url if ollama_available else None,
            "models": ollama_models or ["phi4-mini:latest", "qwen3.5:27b", "qwen2.5-coder:7b"]
        }
        
        # Latency estimates
        latency_estimates = {
            "2_passes_cloud": "15-30s",
            "3_passes_cloud": "30-60s",
            "4_passes_cloud": "60-90s",
            "5_passes_cloud": "90-120s",
            "6_passes_cloud": "120-180s",
            "2_passes_local": "10-20s",
            "3_passes_local": "20-40s",
            "4_passes_local": "40-60s",
            "5_passes_local": "60-80s",
            "6_passes_local": "80-120s",
            "fan_out_3x2": "60-120s (3 perspectives × 2 passes)",
        }
        
        return JSONResponse(
            {
                "passes": [2, 3, 4, 5, 6],
                "width_range": [1, 2, 3, 4, 5, 6],
                "task_classes": task_classes,
                "providers": providers,
                "latency_estimates": latency_estimates,
            },
            status_code=200,
        )
    
    except Exception as e:
        log.exception("Capabilities endpoint error")
        return JSONResponse(
            {"error": f"Failed to get capabilities: {str(e)}"},
            status_code=500,
        )


@mcp.custom_route("/suggest", methods=["POST"])
async def suggest_reasoning_config(request: Request) -> JSONResponse:
    """Smart request routing based on query complexity.
    
    Request body:
    {
        "query": "user question here",
        "context": "optional context" (optional),
        "prefer_local": false (optional, default false)
    }
    
    Response:
    {
        "recommended_passes": 3,
        "task_class": "general",
        "provider": "cloud",
        "width": 1,
        "height": 1,
        "reasoning": "Query is moderately complex; 3 passes recommended for balanced reasoning time",
        "estimated_latency": "30-60s"
    }
    
    HTTP 200: Suggestion generated
    HTTP 400: Invalid input
    HTTP 500: Internal error
    """
    try:
        body = await request.json()
        query = body.get("query", "").strip()
        context = body.get("context", "").strip()
        prefer_local = body.get("prefer_local", False)
        
        if not query:
            return JSONResponse(
                {"error": "Missing required field: query"},
                status_code=400,
            )
        
        # Analyze query complexity
        query_len = len(query)
        complexity = "simple"
        passes = 2
        task_class = "general"
        width = 1
        height = 1
        
        # Detect task class from keywords
        query_lower = query.lower()
        # Check more specific task classes first
        if any(keyword in query_lower for keyword in ["investigate", "evidence", "incident", "threat", "attack", "ioc"]):
            task_class = "investigation"
        elif any(keyword in query_lower for keyword in ["extract", "parse", "schema", "json", "structure", "entity"]):
            task_class = "extraction"
        elif any(keyword in query_lower for keyword in ["write", "summarize", "report", "narrative", "document"]):
            task_class = "synthesis"
        elif any(keyword in query_lower for keyword in ["reason", "logic", "math", "complex", "proof", "algorithm"]):
            task_class = "reasoning"
        elif any(keyword in query_lower for keyword in ["safe", "risk", "policy", "harm", "guardrail", "compliance"]):
            task_class = "safety"
        elif any(keyword in query_lower for keyword in ["code", "bug", "function", "error", "security", "vulnerability"]):
            task_class = "code_review"
        
        # Determine pass count based on complexity
        if query_len < 100:
            passes = 2
            complexity = "simple"
        elif query_len < 300:
            passes = 3
            complexity = "moderate"
        elif query_len < 800:
            passes = 4
            complexity = "complex"
        else:
            passes = 5
            complexity = "very_complex"
        
        # Recommend fan-out for complex investigations
        if task_class in ("investigation", "reasoning") and complexity in ("complex", "very_complex"):
            width = 3
            height = 2
            passes = 1  # height handles pass count in fan-out
        
        # Determine provider
        has_api_key = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("GITHUB_COPILOT_OAUTH_TOKEN"))
        provider = "cloud" if (has_api_key and not prefer_local) else "local"
        
        if not has_api_key:
            provider = "local"
        
        # Estimate latency
        if width > 1:
            estimated_latency = f"{60 * width * height}-{120 * width * height}s (fan-out)"
        else:
            min_lat = 15 * passes
            max_lat = 30 * passes
            estimated_latency = f"{min_lat}-{max_lat}s"
        
        reasoning = f"Query is {complexity}; {passes} passes recommended for {'balanced reasoning time' if passes <= 3 else 'thorough analysis'}."
        if width > 1:
            reasoning += f" Using {width} perspectives with {height} passes each for multi-angle analysis."
        
        return JSONResponse(
            {
                "recommended_passes": passes if width == 1 else height,
                "width": width,
                "height": height,
                "task_class": task_class,
                "provider": provider,
                "complexity": complexity,
                "reasoning": reasoning,
                "estimated_latency": estimated_latency,
            },
            status_code=200,
        )
    
    except json.JSONDecodeError:
        return JSONResponse(
            {"error": "Invalid JSON request"},
            status_code=400,
        )
    except Exception as e:
        log.exception("Suggest endpoint error")
        return JSONResponse(
            {"error": f"Failed to generate suggestion: {str(e)}"},
            status_code=500,
        )


@mcp.custom_route("/mcp/help/{command}", methods=["GET"])
async def get_help(request: Request) -> JSONResponse:
    """Interactive help for common deep-think commands.
    
    Supported commands:
    - verify: Information about claim verification
    - reason: Information about reasoning passes
    - review: Information about code review
    - escalate: Information about escalation mechanisms
    
    Response:
    {
        "command": "verify",
        "description": "...",
        "usage": "...",
        "example": {...},
        "common_mistakes": [...]
    }
    
    HTTP 200: Help found
    HTTP 404: Unknown command
    HTTP 500: Internal error
    """
    try:
        command = request.path_params.get("command", "").lower()
        
        help_docs = {
            "verify": {
                "description": "Verify a claim using chain-of-thought reasoning with cloud or local LLMs.",
                "usage": "POST /verify-queue with {\"claim\": \"...\", \"provider\": \"cloud|local\", \"context\": \"...\"}",
                "example": {
                    "request": {
                        "claim": "Python is a compiled language",
                        "context": "Programming languages",
                        "provider": "cloud"
                    },
                    "response": {
                        "job_id": "uuid-string",
                        "status_url": "/verify-status/uuid-string"
                    }
                },
                "common_mistakes": [
                    "Missing 'claim' field (required)",
                    "Using invalid provider (must be 'cloud' or 'local')",
                    "Not providing context for complex claims",
                    "Polling status too frequently (recommended: 1-2s interval)"
                ],
                "tips": [
                    "Provide context for grounded verification",
                    "Use cloud provider for higher accuracy, local for privacy",
                    "Cache results for identical claims",
                ]
            },
            "reason": {
                "description": "Run multi-pass reasoning with different framings and models.",
                "usage": "POST /call/deep_think_async with {\"question\": \"...\", \"passes\": 2-6, \"task_class\": \"...\"}",
                "example": {
                    "request": {
                        "question": "How should I optimize this database query?",
                        "passes": 3,
                        "task_class": "code_review"
                    },
                    "response": {
                        "job_id": "uuid-string",
                        "status": "queued"
                    }
                },
                "common_mistakes": [
                    "Using passes < 2 or > 6 (clamped to range)",
                    "Using invalid task_class (check /capabilities for valid options)",
                    "Not polling for results (jobs run asynchronously)",
                    "Assuming results available immediately (typical latency: 15-180s)"
                ],
                "tips": [
                    "Use 2-3 passes for quick analysis, 4-6 for deep investigation",
                    "Match task_class to question type (code_review, investigation, etc.)",
                    "Enable verify=True for critical decisions requiring extra validation",
                    "Use provider_config to specify models or Ollama endpoint"
                ]
            },
            "review": {
                "description": "Perform code review using code_review task class with security focus.",
                "usage": "POST /call/deep_think_async with {\"question\": \"<code_snippet>\", \"task_class\": \"code_review\"}",
                "example": {
                    "request": {
                        "question": "def authenticate(password): return len(password) > 0",
                        "task_class": "code_review",
                        "passes": 3
                    },
                    "response": {
                        "job_id": "uuid-string"
                    }
                },
                "common_mistakes": [
                    "Not using task_class='code_review' (this enables code specialization)",
                    "Including too much context (keep focused on review target)",
                    "Using too few passes (3+ recommended for thorough review)",
                    "Not enabling verify=True for security-critical code"
                ],
                "tips": [
                    "Use code_review task_class for specialized code analysis",
                    "Enable verify=True for security review",
                    "Provide minimal but sufficient context",
                    "Use 4-6 passes for security-critical code",
                    "Check /capabilities to see code-specialized models in use"
                ]
            },
            "escalate": {
                "description": "Escalate unresolved claims to manual review or higher-tier models.",
                "usage": "Enable verify=True in deep_think_async call, or POST to /verification/escalate",
                "example": {
                    "request": {
                        "claim": "Unresolved claim from reasoning",
                        "reason": "Confidence too low"
                    },
                    "response": {
                        "escalation_id": "uuid-string",
                        "status": "escalated"
                    }
                },
                "common_mistakes": [
                    "Not enabling verify=True when certainty is critical",
                    "Escalating without trying local reasoning first",
                    "Assuming escalation = guaranteed correctness"
                ],
                "tips": [
                    "Enable verify=True in reasoning calls for critical decisions",
                    "Use escalation for confidence scores < 0.7",
                    "Combine with heavy-tier models for difficult claims",
                    "Check escalation_status for escalated items"
                ]
            }
        }
        
        if command not in help_docs:
            return JSONResponse(
                {
                    "error": f"Unknown command: {command}",
                    "available_commands": list(help_docs.keys())
                },
                status_code=404,
            )
        
        return JSONResponse(help_docs[command], status_code=200)
    
    except Exception as e:
        log.exception("Help endpoint error")
        return JSONResponse(
            {"error": f"Failed to get help: {str(e)}"},
            status_code=500,
        )


def main():
    transport = os.getenv("DEEP_THINK_TRANSPORT", "stdio")
    if transport == "streamable-http":
        host = os.getenv("DEEP_THINK_HOST", "0.0.0.0")
        port = int(os.getenv("DEEP_THINK_PORT", "8080"))
        log.info("Starting HTTP/SSE server on %s:%d", host, port)
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
