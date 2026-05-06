"""Async worker loop — claims queued jobs from SQLite and executes them.

Design:
- Single asyncio loop polls the job queue every second
- Concurrency controlled via a counter (not semaphore, to avoid private attrs)
- Stale 'running' jobs are requeued on startup (crash recovery, time-based cutoff)
- Task references kept in a set to prevent GC before completion
- All store calls from async context run in a thread pool (asyncio.to_thread)
  to avoid blocking the event loop on SQLite I/O
"""

import asyncio
import json
import logging
import os

# Load credentials from ~/.copilot/credentials at startup if not already in env
def _load_credentials_at_startup():
    """Load credentials from file into environment variables at worker startup."""
    cred_file = os.path.expanduser("~/.copilot/credentials")
    if os.path.exists(cred_file):
        try:
            with open(cred_file) as f:
                for line in f:
                    line = line.strip()
                    if '=' not in line or line.startswith('#'):
                        continue
                    key, value = line.split('=', 1)
                    # Convert anthropic.api_key -> ANTHROPIC_API_KEY
                    if key == "anthropic.api_key" and not os.getenv("ANTHROPIC_API_KEY"):
                        os.environ["ANTHROPIC_API_KEY"] = value
                    elif key == "copilot.oauth_token" and not os.getenv("GITHUB_COPILOT_OAUTH_TOKEN"):
                        os.environ["GITHUB_COPILOT_OAUTH_TOKEN"] = value
                    elif key == "ollama.base_url" and not os.getenv("OLLAMA_BASE_URL"):
                        os.environ["OLLAMA_BASE_URL"] = value
        except Exception as e:
            logging.warning(f"Failed to load credentials from {cred_file}: {e}")

_load_credentials_at_startup()

from . import engine, store
from .engine.creative import CreativeReasoningEngine
from .nova_factcheck.pipeline import VerificationPipeline
from . import metrics

_NOVA_VERIFY_ENABLED = os.getenv("DEEP_THINK_NOVA_VERIFY", "true").lower() not in ("0", "false", "no")
_verification_pipeline = VerificationPipeline(enabled=_NOVA_VERIFY_ENABLED)

log = logging.getLogger(__name__)

_active_tasks: set[asyncio.Task] = set()


def _log_job_event(level: str, event: str, **fields) -> None:
    payload = {"event": event, **fields}
    message = json.dumps(payload, sort_keys=True, default=str)
    getattr(log, level)("job_event %s", message)


async def _run_job(job: dict) -> None:
    job_id = job["job_id"]
    task_class = job.get("task_class", "general")
    data_policy = job.get("data_policy", "any")
    try:
        provider_config = json.loads(job.get("provider_config_json") or "{}")
        
        # Extract job control params while preserving routing-critical overrides.
        task_class = provider_config.pop("task_class", job.get("task_class", "general"))
        data_policy = provider_config.get("data_policy", job.get("data_policy", "any"))
        device_id = job.get("device_id", "")
        force_local_models = job.get("force_local_models", False)

        # Grounded reasoning parameters
        enable_research = provider_config.pop("enable_research", True)
        research_query = provider_config.pop("research_query", "")
        dama_node_id = provider_config.pop("dama_node_id", "")
        dama_metric = provider_config.pop("dama_metric", "")
        web_domain_whitelist = provider_config.pop("web_domain_whitelist", [])
        
        # Auto-enable local-only for MQTT operations
        if device_id or force_local_models:
            force_local_models = True
            log.info(f"[MQTT] Detected MQTT job (device_id={device_id}), enabling local-only models")

        _log_job_event(
            "info",
            "job_started",
            job_id=job_id,
            task_class=task_class,
            data_policy=data_policy,
            passes=job.get("passes"),
            force_local_models=force_local_models,
            device_id=device_id or None,
        )

        if provider_config.pop("fan_out", False):
            width = int(provider_config.pop("width", 3))
            height = int(provider_config.pop("height", 2))
            result = await engine.run_fan_out(
                question=job["question"],
                width=width,
                height=height,
                provider_config=provider_config,
                task_class=task_class,
                data_policy=data_policy,
                force_local_models=force_local_models,
                device_id=device_id,
            )
            cfg = engine.build_provider_config(provider_config)
            log.info(
                "Fan-out job %s complete (width=%d height=%d task_class=%s provider=%s)",
                job_id, width, height, task_class, cfg.provider,
            )
        elif provider_config.pop("creative", False):
            creative_mode = provider_config.pop("creative_mode", "lateral-thinking")
            creative_passes = int(provider_config.pop("creative_passes", job.get("passes", 4)))
            verify_with_nova = provider_config.pop("verify_with_nova", False)
            creative_engine = CreativeReasoningEngine()
            creative_result = await creative_engine.run(
                question=job["question"],
                mode=creative_mode,
                passes=creative_passes,
                provider_config=provider_config,
                verify_with_nova=verify_with_nova,
                job_id=job_id,
            )
            result = creative_result.to_dict()
            cfg = engine.build_provider_config(provider_config)
            log.info(
                "Creative job %s complete (mode=%s passes=%d provider=%s)",
                job_id, creative_mode, creative_passes, cfg.provider,
            )
        else:
            result = await engine.deep_think_passes(
                question=job["question"],
                passes=int(job["passes"]),
                provider_config=provider_config,
                model=provider_config.get("model"),
                task_class=task_class,
                data_policy=data_policy,
                force_local_models=force_local_models,
                device_id=device_id,
                job_id=job_id,
                enable_research=enable_research,
                research_query=research_query or "",
                dama_node_id=dama_node_id,
                dama_metric=dama_metric,
                web_domain_whitelist=web_domain_whitelist,
            )
            cfg = engine.build_provider_config(provider_config)
            log.info("Job %s complete (task_class=%s provider=%s)", job_id, task_class, cfg.provider)

        # Nova fact-check: enrich result with verification_results and adjusted confidence
        try:
            result = await asyncio.wait_for(_verification_pipeline.run(result, job_id=job_id), timeout=30.0)
        except asyncio.TimeoutError:
            metrics.get_metrics().record_timeout("nova_verification")
            _log_job_event(
                "warning",
                "job_verification_timeout",
                job_id=job_id,
                timeout_seconds=30.0,
            )
            log.warning("Nova verification pipeline timed out for job %s (non-fatal)", job_id)
        except Exception as vexc:
            _log_job_event(
                "warning",
                "job_verification_failed",
                job_id=job_id,
                exception_type=type(vexc).__qualname__,
                error=str(vexc) or type(vexc).__qualname__,
            )
            log.warning("Nova verification pipeline failed for job %s (non-fatal): %s", job_id, vexc)

        await asyncio.to_thread(store.complete_job, job_id, json.dumps(result))
        _log_job_event(
            "info",
            "job_completed",
            job_id=job_id,
            task_class=task_class,
            result_status=result.get("status"),
            verification_status=result.get("verification_status"),
            final_answer_len=len(result.get("final_answer", "") or ""),
            pass_result_count=len(result.get("pass_results", []) or []),
        )
    except Exception as exc:
        error_msg = str(exc) or type(exc).__qualname__
        try:
            await asyncio.to_thread(store.fail_job, job_id, error_msg)
        except Exception as fail_exc:
            # Double-failure: both complete_job and fail_job failed
            fail_error_msg = str(fail_exc) or type(fail_exc).__qualname__
            log.error("Job %s: DOUBLE FAILURE - complete_job AND fail_job failed. complete: %s, fail: %s", 
                     job_id, error_msg, fail_error_msg)
            # Don't re-raise — let orphan watchdog detect and requeue
        _log_job_event(
            "error",
            "job_failed",
            job_id=job_id,
            task_class=task_class,
            data_policy=data_policy,
            exception_type=type(exc).__qualname__,
            error=error_msg,
        )
        log.error("Job %s failed: %s", job_id, error_msg)


async def _orphan_watchdog(check_interval_seconds: int = 30) -> None:
    """Background watchdog that detects and requeues orphaned jobs.
    
    Runs continuously, checking every check_interval_seconds for jobs stuck in
    'running' state beyond the orphan timeout threshold. When found, requeues
    them by resetting status='queued' and logging the event with metrics.
    """
    log.info("Orphan watchdog started (check interval=%ds)", check_interval_seconds)
    m = metrics.get_metrics()
    
    while True:
        try:
            await asyncio.sleep(check_interval_seconds)
            
            orphans = await asyncio.to_thread(store.detect_orphaned_jobs)
            if not orphans:
                continue
            
            log.warning("Detected %d orphaned job(s)", len(orphans))
            m.increment_orphaned_jobs_detected()
            
            for orphan in orphans:
                job_id = orphan["job_id"]
                claimed_by = orphan.get("claimed_by", "unknown")
                claimed_at = orphan.get("claimed_at", "unknown")
                
                try:
                    requeued = await asyncio.to_thread(
                        store.requeue_orphaned_job,
                        job_id,
                        "timeout"
                    )
                    if requeued:
                        log.warning(
                            "Requeued orphaned job %s (claimed_by=%s at %s)",
                            job_id, claimed_by, claimed_at
                        )
                        m.increment_orphaned_jobs_requeued()
                except Exception as exc:
                    log.error("Failed to requeue orphaned job %s: %s", job_id, exc)
        
        except Exception as exc:
            log.error("Orphan watchdog error (will continue): %s", exc)


async def worker_loop(max_concurrency: int = 0) -> None:
    """Continuously claim and execute queued thinking jobs."""
    if max_concurrency <= 0:
        cpu_count = os.cpu_count() or 4
        max_concurrency = max(
            int(os.getenv("DEEP_THINK_MAX_CONCURRENCY", str(max(4, cpu_count)))),
            4
        )

    stale = await asyncio.to_thread(store.requeue_stale)
    if stale:
        log.info("Requeued %d stale job(s) from prior run", stale)

    log.info("Worker loop started (max_concurrency=%d)", max_concurrency)
    
    # Start background orphan watchdog
    watchdog_task = asyncio.create_task(_orphan_watchdog())
    _active_tasks.add(watchdog_task)

    active = 0
    worker_id = f"worker-{os.getpid()}"
    poll_count = 0

    while True:
        poll_count += 1
        if poll_count % 30 == 0:  # Log every 30 polls (roughly every 30 seconds)
            _log_job_event("info", "worker_poll", active=active, poll_count=poll_count, worker_id=worker_id)
        
        if active >= max_concurrency:
            await asyncio.sleep(0.5)
            continue

        try:
            job = await asyncio.to_thread(store.claim_next_job, worker_id)
        except Exception as exc:
            log.error("claim_next_job failed (will retry): %s", exc)
            await asyncio.sleep(2.0)
            continue

        if job is None:
            await asyncio.sleep(1.0)
            continue

        active += 1
        _log_job_event(
            "info",
            "job_claimed",
            job_id=job["job_id"],
            worker_id=worker_id,
            active=active,
            remaining_capacity=max_concurrency - active,
        )
        log.info("Picked up job %s (%d active)", job["job_id"], active)

        async def _run_and_release(j: dict) -> None:
            nonlocal active
            try:
                await _run_job(j)
            finally:
                active -= 1

        try:
            task = asyncio.create_task(_run_and_release(job))
        except Exception as exc:
            # create_task failed after DB row is already marked 'running' — fail it
            log.error("create_task failed for job %s: %s", job["job_id"], exc)
            active -= 1
            await asyncio.to_thread(store.fail_job, job["job_id"], f"create_task: {exc}")
            continue

        _active_tasks.add(task)
        task.add_done_callback(_active_tasks.discard)
