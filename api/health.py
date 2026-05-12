"""Health check endpoints for system monitoring and diagnostics."""

import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import health, store, mcp_help, runtime_guard

log = logging.getLogger(__name__)


def register(mcp):
    """Register health check routes."""
    
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
            # Import here to avoid circular imports
            from .. import mqtt as mqtt_integration
            
            # Get verification queue metrics
            verify_metrics = {}
            if hasattr(mcp, 'verify_queue') and mcp.verify_queue:
                verify_metrics = mcp.verify_queue.get_metrics()
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

    @mcp.custom_route("/health/invariants", methods=["GET"])
    async def health_invariants(request: Request) -> JSONResponse:
        """Validate recent fan-out result contract invariants."""
        try:
            invariant = runtime_guard.check_recent_fanout_invariants(store._connect, limit=25)
            fingerprint = runtime_guard.get_runtime_fingerprint().as_dict()
            degraded = bool(invariant.get("violations_count", 0)) or bool(fingerprint.get("runtime_stale"))
            return JSONResponse(
                {
                    "status": "degraded" if degraded else "healthy",
                    "runtime_stale": bool(fingerprint.get("runtime_stale")),
                    "runtime_fingerprint": fingerprint,
                    "fanout_invariants": invariant,
                },
                status_code=503 if degraded else 200,
            )
        except Exception as e:
            log.exception("Health invariants endpoint error")
            return JSONResponse(
                {"status": "degraded", "error": f"Failed to check invariants: {str(e)}"},
                status_code=500,
            )
