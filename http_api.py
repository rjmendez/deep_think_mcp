"""
HTTP API server for deep-think reasoning.

Exposes deep_think_async and deep_think_fan_out as HTTP endpoints.
No MCP protocol layer — just plain REST JSON.

Endpoints:
  POST /api/v1/deep_think_async — Queue a multi-pass reasoning job
  POST /api/v1/deep_think_fan_out — Queue a fan-out reasoning job
  GET  /api/v1/result/{job_id} — Poll job result
  GET  /api/v1/jobs — List recent jobs
  GET  /health — Health check
"""

import asyncio
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from uvicorn import run

try:  # Package import path
    from .engine import TASK_CLASS_PROFILES, build_provider_config
    from .store import (
        create_job,
        get_job,
        list_jobs,
        request_job_cancellation,
        lookup_idempotent_job,
        bind_idempotency_key,
        build_idempotency_request_hash,
        fail_job,
        init_db,
        _connect,
    )
    from . import worker as _worker
    from . import runtime_guard
    from . import metrics as runtime_metrics
    from .logging_context import setup_structured_logging
    from .engine.validator import (
        validate_passes,
        validate_width,
        validate_height,
        validate_question,
        validate_adaptive_config,
        validate_web_domain_whitelist,
        ValidationError,
    )
    from .api_security import ApiProtectionMiddleware
except ImportError:  # Script import path
    from engine import TASK_CLASS_PROFILES, build_provider_config
    from store import (
        create_job,
        get_job,
        list_jobs,
        request_job_cancellation,
        lookup_idempotent_job,
        bind_idempotency_key,
        build_idempotency_request_hash,
        fail_job,
        init_db,
        _connect,
    )
    import worker as _worker
    import runtime_guard
    import metrics as runtime_metrics
    from logging_context import setup_structured_logging
    from engine.validator import (
        validate_passes,
        validate_width,
        validate_height,
        validate_question,
        validate_adaptive_config,
        validate_web_domain_whitelist,
        ValidationError,
    )
    from api_security import ApiProtectionMiddleware

log = logging.getLogger(__name__)
setup_structured_logging()

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    init_db()
    worker_task = asyncio.create_task(_worker.worker_loop())
    try:
        yield
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Deep-Think HTTP API", version="1.0.0", lifespan=_lifespan)

# Enable CORS for local use
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Shared API hardening (auth + per-IP rate limiting) for all HTTP endpoints.
app.add_middleware(ApiProtectionMiddleware)

def _idempotent_response(job: dict, endpoint: str) -> dict:
    response = {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "idempotent_replay": True,
        "idempotency_endpoint": endpoint,
    }
    if job.get("completed_at"):
        response["completed_at"] = job.get("completed_at")
    if job.get("error"):
        response["error"] = job.get("error")
    if job.get("result") is not None:
        response["result_available"] = True
    return response


@app.get("/health")
async def health():
    """Health check endpoint with DB and runtime freshness checks."""
    db_status = "unavailable"
    runtime_fingerprint = runtime_guard.get_runtime_fingerprint().as_dict()
    try:
        conn = _connect()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        db_status = "healthy"
    except sqlite3.DatabaseError:
        log.exception("HTTP API health check DB failure")
    except Exception:
        log.exception("HTTP API health check unexpected failure")

    status = "healthy"
    if db_status != "healthy" or runtime_fingerprint.get("runtime_stale"):
        status = "degraded"

    payload = {
        "status": status,
        "service": "deep-think-http-api",
        "version": "1.0.0",
        "db_status": db_status,
        "runtime_stale": bool(runtime_fingerprint.get("runtime_stale")),
    }
    return JSONResponse(payload, status_code=200 if status == "healthy" else 503)


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return PlainTextResponse(
        runtime_metrics.get_metrics().to_prometheus_format(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.post("/api/v1/deep_think_async")
async def deep_think_async(
    question: str,
    passes: Optional[int] = None,
    task_class: Optional[str] = None,
    data_policy: Optional[str] = None,
    model: Optional[str] = None,
    provider_config: Optional[dict] = None,
    verify: bool = False,
    width: Optional[int] = None,
    height: Optional[int] = None,
    extract_claims: bool = False,
    enable_research: bool = True,
    research_query: Optional[str] = None,
    dama_node_id: Optional[str] = None,
    dama_metric: Optional[str] = None,
    web_domain_whitelist: Optional[list] = None,
    idempotency_key: Optional[str] = None,
):
    """Queue a multi-pass reasoning job and return a job_id immediately."""
    try:
        question = validate_question(question)
        web_domain_whitelist = validate_web_domain_whitelist(web_domain_whitelist)
        if provider_config is not None and not isinstance(provider_config, dict):
            raise ValidationError("provider_config must be an object")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        # Apply defaults
        passes = passes if passes is not None else 3
        task_class = task_class if task_class is not None else "general"
        data_policy = data_policy if data_policy is not None else "any"
        model = model if model is not None else ""
        width = width if width is not None else 1
        height = height if height is not None else 1
        research_query = research_query if research_query is not None else ""
        dama_node_id = dama_node_id if dama_node_id is not None else ""
        dama_metric = dama_metric if dama_metric is not None else ""

        # Validate parameters
        passes = validate_passes(passes)
        width = validate_width(width)
        height = validate_height(height)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        # Ensure provider_config is a dict
        provider_config = provider_config or {}

        # Store control params in provider_config so worker can retrieve them
        provider_config_for_job = provider_config.copy()
        provider_config_for_job["data_policy"] = data_policy
        provider_config_for_job["task_class"] = task_class
        provider_config_for_job["enable_research"] = enable_research
        provider_config_for_job["research_query"] = research_query
        provider_config_for_job["dama_node_id"] = dama_node_id
        provider_config_for_job["dama_metric"] = dama_metric
        provider_config_for_job["web_domain_whitelist"] = web_domain_whitelist

        # Build provider config to apply data_policy-based defaults
        cfg = build_provider_config(provider_config_for_job)
        request_hash = None
        if idempotency_key:
            request_hash = build_idempotency_request_hash(
                {
                    "endpoint": "deep_think_async",
                    "question": question,
                    "passes": passes,
                    "task_class": task_class,
                    "data_policy": data_policy,
                    "model": model,
                    "provider_config": provider_config_for_job,
                    "verify": verify,
                    "width": width,
                    "height": height,
                    "extract_claims": extract_claims,
                    "enable_research": enable_research,
                    "research_query": research_query,
                    "dama_node_id": dama_node_id,
                    "dama_metric": dama_metric,
                    "web_domain_whitelist": web_domain_whitelist,
                }
            )
            existing = lookup_idempotent_job(idempotency_key, request_hash, "deep_think_async")
            if existing:
                return _idempotent_response(existing, "deep_think_async")

        # Create job
        job_id = create_job(
            question=question,
            passes=passes,
            provider=cfg.provider,
            model_summary="",
            provider_config_json=json.dumps(provider_config_for_job),
        )
        if idempotency_key and request_hash:
            bound_job_id = bind_idempotency_key(
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                endpoint="deep_think_async",
                job_id=job_id,
            )
            if bound_job_id != job_id:
                fail_job(job_id, "cancelled: duplicate idempotency key")
                existing = get_job(bound_job_id)
                if existing:
                    return _idempotent_response(existing, "deep_think_async")
        _worker.notify_job_available()
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        log.exception("HTTP deep_think_async failed")
        raise HTTPException(status_code=500, detail="Internal server error")

    return {"job_id": job_id, "status": "queued"}


@app.post("/api/v1/deep_think_fan_out")
async def deep_think_fan_out(
    question: str,
    width: Optional[int] = None,
    height: Optional[int] = None,
    task_class: Optional[str] = None,
    data_policy: Optional[str] = None,
    max_parallel: Optional[int] = None,
    max_width: Optional[int] = None,
    confidence_threshold: Optional[int] = None,
    extract_claims: bool = False,
    provider_config: Optional[dict] = None,
    adaptive_config: Optional[dict] = None,
    web_domain_whitelist: Optional[list] = None,
    idempotency_key: Optional[str] = None,
):
    """Queue a perspective fan-out reasoning job and return a job_id immediately."""
    try:
        question = validate_question(question)
        adaptive_config = validate_adaptive_config(adaptive_config)
        web_domain_whitelist = validate_web_domain_whitelist(web_domain_whitelist)
        if provider_config is not None and not isinstance(provider_config, dict):
            raise ValidationError("provider_config must be an object")
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        # Apply defaults
        width = width if width is not None else 3
        height = height if height is not None else 2
        task_class = task_class if task_class is not None else "general"
        data_policy = data_policy if data_policy is not None else "any"

        # Validate parameters
        width = validate_width(width)
        height = validate_height(height)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    max_parallel = max_parallel if max_parallel is not None else 2
    max_width = max_width if max_width is not None else 6
    confidence_threshold = confidence_threshold if confidence_threshold is not None else 50

    total_calls = width * height + 1

    try:
        # Ensure provider_config is a dict
        provider_config = provider_config or {}

        # Store control params in provider_config so worker can retrieve them
        provider_config_for_job = provider_config.copy()
        provider_config_for_job["data_policy"] = data_policy
        provider_config_for_job["task_class"] = task_class
        provider_config_for_job["extract_claims"] = extract_claims
        provider_config_for_job["fan_out"] = True
        provider_config_for_job["width"] = width
        provider_config_for_job["height"] = height
        provider_config_for_job["adaptive_config"] = adaptive_config
        provider_config_for_job["web_domain_whitelist"] = web_domain_whitelist

        # Build provider config to apply data_policy-based defaults
        cfg = build_provider_config(provider_config_for_job)
        request_hash = None
        if idempotency_key:
            request_hash = build_idempotency_request_hash(
                {
                    "endpoint": "deep_think_fan_out",
                    "question": question,
                    "width": width,
                    "height": height,
                    "task_class": task_class,
                    "data_policy": data_policy,
                    "max_parallel": max_parallel,
                    "max_width": max_width,
                    "confidence_threshold": confidence_threshold,
                    "extract_claims": extract_claims,
                    "provider_config": provider_config_for_job,
                    "adaptive_config": adaptive_config,
                    "web_domain_whitelist": web_domain_whitelist,
                }
            )
            existing = lookup_idempotent_job(idempotency_key, request_hash, "deep_think_fan_out")
            if existing:
                response = _idempotent_response(existing, "deep_think_fan_out")
                response["total_calls"] = total_calls
                return response

        # Create job
        job_id = create_job(
            question=question,
            passes=total_calls,
            provider=cfg.provider,
            model_summary="",
            provider_config_json=json.dumps(provider_config_for_job),
        )
        if idempotency_key and request_hash:
            bound_job_id = bind_idempotency_key(
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                endpoint="deep_think_fan_out",
                job_id=job_id,
            )
            if bound_job_id != job_id:
                fail_job(job_id, "cancelled: duplicate idempotency key")
                existing = get_job(bound_job_id)
                if existing:
                    response = _idempotent_response(existing, "deep_think_fan_out")
                    response["total_calls"] = total_calls
                    return response
        _worker.notify_job_available()
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        log.exception("HTTP deep_think_fan_out failed")
        raise HTTPException(status_code=500, detail="Internal server error")

    return {"job_id": job_id, "status": "queued", "total_calls": total_calls}


@app.get("/api/v1/result/{job_id}")
async def get_result(job_id: str):
    """Poll job status and retrieve results."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return {
        "job_id": job_id,
        "status": job.get("status"),
        "result": job.get("result"),
        "confidence": job.get("confidence"),
        "pass_outputs": job.get("pass_outputs"),
        "error": job.get("error"),
    }


@app.get("/api/v1/jobs")
async def list_recent_jobs(status: Optional[str] = None, limit: int = 10):
    """List recent jobs."""
    jobs = list_jobs(status=status, limit=limit)
    return {"jobs": jobs, "count": len(jobs)}


@app.post("/api/v1/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, reason: str = "api_cancel"):
    """Cancel a queued/running job and return current cancellation state."""
    result = request_job_cancellation(job_id, reason=reason)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if result.get("status") == "running" and result.get("cancel_requested"):
        task_cancelled = _worker.cancel_running_job(job_id)
        result["worker_cancelled"] = bool(task_cancelled)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    host = os.getenv("DEEP_THINK_HOST", "0.0.0.0")
    port = int(os.getenv("DEEP_THINK_PORT", "8080"))
    run(app, host=host, port=port, log_level="info")
