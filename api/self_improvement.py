"""Self-improvement plan generation, approval, implementation, and deployment."""

import json
import logging
import os
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..adversarial_testing.implementation_pipeline import ImplementationPipeline
from ..adversarial_testing.deployment_pipeline import DeploymentPipeline
from ..adversarial_testing.metrics import MetricsCollector
from .. import adversarial_store

log = logging.getLogger(__name__)


def register(mcp):
    """Register self-improvement routes."""
    
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
            
            pipeline = ImplementationPipeline()
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
        findings: list,
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
        _planning_engine = getattr(mcp, 'planning_engine', None)
        if not _planning_engine:
            return {
                "status": "error",
                "error": "Planning engine not initialized",
                "plans": [],
            }
        
        try:
            start_time = time.time()
            
            if not findings or not isinstance(findings, list):
                return {
                    "status": "error",
                    "error": "findings must be a non-empty list",
                    "plans": [],
                }
            
            findings = findings[:limit * 2]
            
            log.info(f"Generating plans for {len(findings)} findings (limit={limit})")
            
            plans = await _planning_engine.generate_plans_for_findings(
                findings=findings,
                limit=limit,
            )
            
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
        _planning_engine = getattr(mcp, 'planning_engine', None)
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
        _planning_engine = getattr(mcp, 'planning_engine', None)
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
            
            adversarial_db = os.getenv(
                "ADVERSARIAL_DB",
                str(__import__("pathlib").Path.home() / ".deep_think" / "adversarial.db"),
            )
            store_instance = adversarial_store.AdversarialStore(adversarial_db)
            
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
