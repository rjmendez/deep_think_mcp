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
from typing import Optional

from fastapi import FastAPI, HTTPException
from uvicorn import run

from engine import orchestrator, TASK_CLASS_PROFILES, build_provider_config
from engine.store import create_job, get_job, list_jobs, update_job
from engine.validator import validate_passes, validate_width, validate_height, ValidationError

log = logging.getLogger(__name__)

app = FastAPI(title="Deep-Think HTTP API", version="1.0.0")

# Enable CORS for local use
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "deep-think-http-api",
        "version": "1.0.0"
    }


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
):
    """Queue a multi-pass reasoning job and return a job_id immediately."""
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
    if web_domain_whitelist:
        provider_config_for_job["web_domain_whitelist"] = web_domain_whitelist
    
    # Build provider config to apply data_policy-based defaults
    cfg = build_provider_config(provider_config_for_job)
    
    # Create job
    job_id = create_job(
        question=question,
        passes=passes,
        provider=cfg.provider,
        model_summary="",
        provider_config_json=json.dumps(provider_config_for_job),
    )

    # Queue async work
    asyncio.create_task(
        orchestrator.deep_think_passes(
            question=question,
            passes=passes,
            task_class=task_class,
            data_policy=data_policy,
            model=model,
            provider_config=provider_config_for_job,
            verify=verify,
            job_id=job_id,
        )
    )

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
):
    """Queue a perspective fan-out reasoning job and return a job_id immediately."""
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
    
    # Build provider config to apply data_policy-based defaults
    cfg = build_provider_config(provider_config_for_job)
    
    # Create job
    job_id = create_job(
        question=question,
        passes=total_calls,
        provider=cfg.provider,
        model_summary="",
        provider_config_json=json.dumps(provider_config_for_job),
    )

    # Queue async work
    asyncio.create_task(
        orchestrator.run_fan_out(
            question=question,
            width=width,
            height=height,
            task_class=task_class,
            data_policy=data_policy,
            max_parallel=max_parallel,
            max_width=max_width,
            confidence_threshold=confidence_threshold,
            extract_claims=extract_claims,
            provider_config=provider_config_for_job,
            job_id=job_id,
        )
    )

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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    host = os.getenv("DEEP_THINK_HOST", "127.0.0.1")
    port = int(os.getenv("DEEP_THINK_PORT", "8080"))
    run(app, host=host, port=port, log_level="info")
