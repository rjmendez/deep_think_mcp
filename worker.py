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

from . import engine, store

log = logging.getLogger(__name__)

_active_tasks: set[asyncio.Task] = set()


async def _run_job(job: dict) -> None:
    job_id = job["job_id"]
    try:
        provider_config = json.loads(job.get("provider_config_json") or "{}")
        task_class = provider_config.pop("task_class", "general")
        data_policy = provider_config.pop("data_policy", "any")
        cfg = engine.build_provider_config(provider_config)

        if provider_config.pop("fan_out", False):
            width = int(provider_config.pop("width", 3))
            height = int(provider_config.pop("height", 2))
            max_parallel = int(provider_config.pop("max_parallel", 2))
            max_width = int(provider_config.pop("max_width", 6))
            confidence_threshold = int(provider_config.pop("confidence_threshold", 50))
            extract_claims = bool(provider_config.pop("extract_claims", False))
            result = await engine.run_fan_out(
                question=job["question"],
                width=width,
                height=height,
                provider_cfg=cfg,
                task_class=task_class,
                data_policy=data_policy,
                max_parallel=max_parallel,
                job_id=job_id,
                max_width=max_width,
                confidence_threshold=confidence_threshold,
                extract_claims=extract_claims,
            )
            log.info(
                "Fan-out job %s complete (width=%d height=%d task_class=%s provider=%s)",
                job_id, width, height, task_class, cfg.provider,
            )
        else:
            verify = bool(provider_config.pop("verify", False))
            result = await engine.deep_think_passes(
                question=job["question"],
                passes=int(job["passes"]),
                provider_cfg=cfg,
                task_class=task_class,
                data_policy=data_policy,
                verify=verify,
            )
            log.info("Job %s complete (task_class=%s provider=%s)", job_id, task_class, cfg.provider)

        await asyncio.to_thread(store.complete_job, job_id, result)
    except Exception as exc:
        error_msg = str(exc) or type(exc).__qualname__
        await asyncio.to_thread(store.fail_job, job_id, error_msg)
        log.error("Job %s failed: %s", job_id, error_msg)


async def worker_loop(max_concurrency: int = 0) -> None:
    """Continuously claim and execute queued thinking jobs."""
    if max_concurrency <= 0:
        max_concurrency = int(os.getenv("DEEP_THINK_MAX_CONCURRENCY", "2"))

    stale = await asyncio.to_thread(store.requeue_stale)
    if stale:
        log.info("Requeued %d stale job(s) from prior run", stale)

    log.info("Worker loop started (max_concurrency=%d)", max_concurrency)

    active = 0

    while True:
        if active >= max_concurrency:
            await asyncio.sleep(0.5)
            continue

        try:
            job = await asyncio.to_thread(store.claim_next_job)
        except Exception as exc:
            log.error("claim_next_job failed (will retry): %s", exc)
            await asyncio.sleep(2.0)
            continue

        if job is None:
            await asyncio.sleep(1.0)
            continue

        active += 1
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
