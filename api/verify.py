"""Claim verification endpoints (synchronous and asynchronous)."""

import asyncio
import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)


def register(mcp):
    """Register verification routes."""
    
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

            # Get the appropriate provider from mcp attributes
            if provider == "cloud":
                _cloud_provider = getattr(mcp, '_cloud_provider', None)
                if not _cloud_provider:
                    return JSONResponse(
                        {
                            "error": "Cloud provider not available (missing ANTHROPIC_API_KEY?)"
                        },
                        status_code=503,
                    )
                prov = _cloud_provider
                verify_config = mcp.verify_config
                timeout = verify_config.verify_cloud_timeout if verify_config else 30
            else:
                _local_provider = getattr(mcp, '_local_provider', None)
                if not _local_provider:
                    return JSONResponse(
                        {
                            "error": "Local provider not available (Ollama not running?)"
                        },
                        status_code=503,
                    )
                prov = _local_provider
                verify_config = mcp.verify_config
                timeout = verify_config.verify_local_timeout if verify_config else 60

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
