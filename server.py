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
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastmcp import FastMCP

from .engine.provider import _tier_provider, refresh_ollama_models
from .engine import build_provider_config
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
    log.info("[LIFESPAN] Starting initialization...")
    
    try:
        store.init_db()
        log.info("[LIFESPAN] Database initialized")
    except Exception as e:
        log.error("[LIFESPAN] Database init failed: %s", e)
        raise
    
    # Refresh Ollama model cache for validation
    try:
        log.info("[LIFESPAN] Refreshing Ollama model cache...")
        ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        await refresh_ollama_models(ollama_url)
        log.info("[LIFESPAN] Ollama model cache refreshed")
    except Exception as e:
        log.warning("[LIFESPAN] Ollama model cache refresh failed (non-fatal): %s", e)
        # Continue without Ollama model validation — will fail at job runtime instead
    
    # [MQTT] Startup — initialize subscriber and processor
    try:
        log.info("[LIFESPAN] Starting MQTT...")
        await mqtt_integration.mqtt_startup()
        log.info("[LIFESPAN] MQTT startup complete")
        mqtt_integration.setup_signal_handlers()
        log.info("[LIFESPAN] MQTT signal handlers registered")
    except Exception as e:
        log.error("[LIFESPAN] MQTT startup failed: %s", e)
        raise
    
    # Initialize advanced MQTT engine adapter
    try:
        log.info("[LIFESPAN] Initializing MQTTEngineAdapter...")
        mqtt_adapter = MQTTEngineAdapter(deep_think_fn=__import__("engine", fromlist=["deep_think_passes"]).deep_think_passes)
        mqtt_initialized = await mqtt_adapter.start_mqtt()
        log.info("[LIFESPAN] MQTTEngineAdapter initialized: %s", mqtt_initialized)
        app.mqtt_adapter = mqtt_adapter
        mcp.mqtt_adapter = mqtt_adapter  # Expose to tools
        
        if mqtt_initialized:
            log.info("[MQTT] MQTTEngineAdapter initialized and running")
    except Exception as e:
        log.error("[LIFESPAN] MQTTEngineAdapter init failed: %s", e)
        mqtt_adapter = None
        mqtt_initialized = False
        # Don't raise — let system continue without MQTT
    
    # Initialize validation suite for self-improvement
    try:
        log.info("[LIFESPAN] Initializing ValidationSuite...")
        metrics_collector = MetricsCollector()
        validation_suite = ValidationSuite(
            metrics=metrics_collector,
            git_repo_root="/home/USER/development/deep_think_mcp",
            test_command="pytest --cov=adversarial_testing adversarial_testing/tests/",
        )
        app.validation_suite = validation_suite
        mcp.validation_suite = validation_suite  # Expose to tools
        log.info("[LIFESPAN] ValidationSuite initialized")
    except Exception as e:
        log.error("[LIFESPAN] ValidationSuite init failed: %s", e)
        validation_suite = None
    
    # Initialize planning engine
    try:
        log.info("[LIFESPAN] Initializing PlanningEngine...")
        global _planning_engine
        from .engine import deep_think_passes
        _planning_engine = PlanningEngine(deep_think_fn=deep_think_passes)
        app.planning_engine = _planning_engine
        mcp.planning_engine = _planning_engine
        log.info("[LIFESPAN] PlanningEngine initialized")
    except Exception as e:
        log.error("[LIFESPAN] PlanningEngine init failed: %s", e)
    
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
        mcp._cloud_provider = _cloud_provider
        mcp._local_provider = _local_provider
        
        # Start worker
        await _verify_worker.start()
        log.info("Verification system initialized")
    except Exception as e:
        log.error("Failed to initialize verification system: %s", e)
    
    discovery_task = None
    if _ollama_in_use():
        base_url = os.getenv("OLLAMA_BASE_URL", "")
        if not base_url:
            log.error("Ollama provider configured but OLLAMA_BASE_URL not set")
        else:
            log.info("[LIFESPAN] Creating model discovery task for Ollama at %s", base_url)
            discovery_task = asyncio.create_task(_discover.run_discovery(base_url, benchmark=True))
    else:
        log.info("No Ollama provider in use — skipping model discovery")
    
    log.info("[LIFESPAN] Creating worker loop task...")
    try:
        worker_task = asyncio.create_task(worker.worker_loop())
        log.info("[LIFESPAN] Worker loop task created: %s", worker_task)
    except Exception as e:
        log.error("[LIFESPAN] Failed to create worker task: %s", e)
        import traceback
        traceback.print_exc()
        raise
    
    log.info("[LIFESPAN] ===== INITIALIZATION COMPLETE, YIELDING CONTROL =====")
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

# Register all API routes from modular api modules
from . import api
api.register_routes(mcp)


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
