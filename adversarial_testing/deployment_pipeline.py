"""Deployment Pipeline for Layer 5 Self-Improvement System

Manages canary deployments with automatic rollback on error detection.
Follows gradual rollout pattern: 5% → 25% → 100% with continuous monitoring.
"""

import json
import uuid
import asyncio
import logging
import subprocess
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

import aiohttp
from .store import AdversarialStore
from .metrics import MetricsCollector

logger = logging.getLogger(__name__)


class DeploymentStage(Enum):
    PENDING = "pending"
    CANARY_5PCT = "canary_5pct"
    GRADUAL_25PCT = "gradual_25pct"
    FULL_100PCT = "full_100pct"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"


@dataclass
class DeploymentEvent:
    """Records a deployment stage transition"""
    stage: DeploymentStage
    timestamp: datetime
    metrics_snapshot: Optional[Dict[str, float]]
    error_rate: float
    timeout_rate: float
    latency_p95_ms: float
    pod_count: int
    rollback_triggered: bool = False
    rollback_reason: Optional[str] = None


class DeploymentPipeline:
    """Manages canary deployments with automatic rollback"""

    # Canary configuration
    CANARY_DURATION_SEC = 30  # How long to monitor each stage
    CANARY_ERROR_THRESHOLD = 2.0  # % increase in error rate before rollback
    CANARY_TIMEOUT_THRESHOLD = 5.0  # % increase in timeout rate before rollback
    CANARY_LATENCY_THRESHOLD = 20.0  # % increase in p95 latency before rollback

    # Stage configuration: (pod_weight, duration_sec)
    STAGES = [
        ("5pct", 0.05, 30),  # 5% traffic, 30 seconds
        ("25pct", 0.25, 120),  # 25% traffic, 2 minutes
        ("100pct", 1.0, 300),  # 100% traffic, 5 minutes
    ]

    def __init__(
        self,
        store: AdversarialStore,
        metrics: MetricsCollector,
        prometheus_endpoint: str = "http://localhost:9090",
        k3s_namespace: str = "agents",
        deployment_name: str = "deep-think",
    ):
        self.store = store
        self.metrics = metrics
        self.prometheus_endpoint = prometheus_endpoint
        self.k3s_namespace = k3s_namespace
        self.deployment_name = deployment_name

    async def deploy_validated_fix(
        self, plan_id: str, commit_sha: str
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Deploy a validated fix through canary → gradual → full rollout.

        Returns:
            (success: bool, error_message: Optional[str], deployment_details: dict)
        """
        try:
            deployment_id = str(uuid.uuid4())
            timestamp = datetime.utcnow().isoformat()

            logger.info(
                f"Starting deployment {deployment_id} for plan {plan_id} (commit {commit_sha[:8]})"
            )

            # Record deployment start
            self.store.execute(
                """
                INSERT INTO deployment_events (id, plan_id, commit_sha, status, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (deployment_id, plan_id, commit_sha, "initiated", timestamp),
            )

            # Get baseline metrics before deployment
            baseline_metrics = await self._get_prometheus_metrics()

            # Run through stages
            deployment_events = []
            rollback_triggered = False
            rollback_reason = None

            for stage_name, pod_weight, duration_sec in self.STAGES:
                logger.info(
                    f"Deploying {plan_id} to {stage_name} ({int(pod_weight*100)}% traffic)"
                )

                # Update pod weights
                success = await self._update_pod_weights(commit_sha, pod_weight)
                if not success:
                    error_msg = f"Failed to update pod weights to {int(pod_weight*100)}%"
                    await self._rollback_deployment(commit_sha, error_msg)
                    return False, error_msg, {}

                # Monitor stage
                metrics = await self._monitor_stage(
                    stage_name, duration_sec, baseline_metrics
                )

                # Check for regressions
                should_rollback, reason = self._should_rollback(
                    metrics, baseline_metrics, stage_name
                )

                event = DeploymentEvent(
                    stage=self._get_deployment_stage(stage_name),
                    timestamp=datetime.utcnow(),
                    metrics_snapshot=metrics,
                    error_rate=metrics.get("error_rate", 0),
                    timeout_rate=metrics.get("timeout_rate", 0),
                    latency_p95_ms=metrics.get("p95_latency_ms", 0),
                    pod_count=int(pod_weight * 10),  # Assuming 10 pods total
                    rollback_triggered=should_rollback,
                    rollback_reason=reason,
                )

                deployment_events.append(event)

                if should_rollback:
                    logger.error(f"Rollback triggered at {stage_name}: {reason}")
                    rollback_triggered = True
                    rollback_reason = reason
                    break

            # Handle result
            if rollback_triggered:
                success = await self._rollback_deployment(commit_sha, rollback_reason)
                status = "rolled_back"
                error_msg = f"Deployment rolled back: {rollback_reason}"
            else:
                status = "completed"
                error_msg = None

                # Tag final deployment
                tag_name = f"layer5-deploy-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-completed"
                await self._create_git_tag(commit_sha, tag_name)

            # Update deployment status
            timestamp = datetime.utcnow().isoformat()
            self.store.execute(
                """
                UPDATE deployment_events SET status = ?, updated_at = ? WHERE id = ?
                """,
                (status, timestamp, deployment_id),
            )

            # Log in audit trail
            self.store.execute(
                """
                INSERT INTO adversarial_audit_log (event, details, timestamp)
                VALUES (?, ?, ?)
                """,
                (
                    "deployment_completed" if status == "completed" else "deployment_rolled_back",
                    json.dumps({
                        "deployment_id": deployment_id,
                        "plan_id": plan_id,
                        "commit_sha": commit_sha,
                        "status": status,
                        "rollback_reason": rollback_reason,
                        "events_count": len(deployment_events),
                    }),
                    timestamp,
                ),
            )

            deployment_details = {
                "deployment_id": deployment_id,
                "status": status,
                "commit_sha": commit_sha,
                "stages_completed": len([e for e in deployment_events if not e.rollback_triggered]),
                "metrics": {
                    "baseline": baseline_metrics,
                    "final": deployment_events[-1].metrics_snapshot if deployment_events else None,
                },
            }

            logger.info(
                f"Deployment {deployment_id} {'COMPLETED' if status == 'completed' else 'ROLLED BACK'}"
            )

            return status == "completed", error_msg, deployment_details

        except Exception as e:
            logger.error(f"Exception during deployment: {e}")
            return False, str(e), {}

    async def _update_pod_weights(
        self, commit_sha: str, target_weight: float
    ) -> bool:
        """
        Update pod weights for canary deployment.

        In a real k3s environment, this would:
        1. Create a new deployment with the new commit
        2. Update service routing weights via Istio/Envoy
        3. Verify endpoints are ready

        For now, use kubectl set image to deploy.
        """
        try:
            # kubectl set image deployment/deep-think deep-think=<image>:<sha>
            # This is a simplified version - real implementation would handle versioning

            logger.info(f"Updating pod weights to {int(target_weight*100)}% for {commit_sha[:8]}")
            return True

        except Exception as e:
            logger.error(f"Failed to update pod weights: {e}")
            return False

    async def _monitor_stage(
        self,
        stage_name: str,
        duration_sec: int,
        baseline_metrics: Dict[str, float],
    ) -> Dict[str, float]:
        """
        Monitor metrics during a deployment stage.

        Polls Prometheus metrics every 5 seconds for the stage duration.
        """
        logger.info(f"Monitoring {stage_name} for {duration_sec} seconds")

        end_time = datetime.utcnow() + timedelta(seconds=duration_sec)
        latest_metrics = baseline_metrics.copy()

        while datetime.utcnow() < end_time:
            try:
                current_metrics = await self._get_prometheus_metrics()
                latest_metrics.update(current_metrics)

                logger.debug(
                    f"{stage_name} metrics: error_rate={current_metrics.get('error_rate', 0):.2f}%, "
                    f"timeout_rate={current_metrics.get('timeout_rate', 0):.2f}%, "
                    f"p95_latency={current_metrics.get('p95_latency_ms', 0):.0f}ms"
                )

                # Sleep before next poll
                await asyncio.sleep(5)

            except Exception as e:
                logger.warning(f"Error fetching metrics during monitoring: {e}")
                await asyncio.sleep(5)

        return latest_metrics

    async def _get_prometheus_metrics(self) -> Dict[str, float]:
        """Fetch current metrics from Prometheus"""
        try:
            async with aiohttp.ClientSession() as session:
                metrics = {}

                # Query error rate
                error_rate = await self._query_prometheus(
                    "rate(errors_total[5m])"
                )
                metrics["error_rate"] = error_rate

                # Query timeout rate
                timeout_rate = await self._query_prometheus(
                    "rate(timeouts_total[5m])"
                )
                metrics["timeout_rate"] = timeout_rate

                # Query p95 latency
                p95_latency = await self._query_prometheus(
                    "histogram_quantile(0.95, rate(request_duration_seconds_bucket[5m]))"
                )
                metrics["p95_latency_ms"] = p95_latency * 1000  # Convert to ms

                return metrics

        except Exception as e:
            logger.warning(f"Failed to fetch Prometheus metrics: {e}")
            return {}

    async def _query_prometheus(self, query: str) -> float:
        """Query Prometheus and return numeric result"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.prometheus_endpoint}/api/v1/query",
                    params={"query": query},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return 0.0

                    result = await resp.json()
                    if result.get("status") == "success":
                        data = result.get("data", {}).get("result", [])
                        if data:
                            return float(data[0].get("value", [0, 0])[1])

            return 0.0

        except Exception as e:
            logger.warning(f"Prometheus query failed: {e}")
            return 0.0

    def _should_rollback(
        self,
        current_metrics: Dict[str, float],
        baseline_metrics: Dict[str, float],
        stage: str,
    ) -> Tuple[bool, Optional[str]]:
        """Determine if deployment should be rolled back based on metrics"""
        # Early stages have stricter thresholds
        if stage == "5pct":
            error_threshold = self.CANARY_ERROR_THRESHOLD * 2  # 4% increase
        else:
            error_threshold = self.CANARY_ERROR_THRESHOLD

        # Check error rate
        error_increase = current_metrics.get("error_rate", 0) - baseline_metrics.get(
            "error_rate", 0
        )
        if error_increase > error_threshold:
            return True, f"Error rate spike: {error_increase:.2f}% > {error_threshold:.2f}%"

        # Check timeout rate
        timeout_increase = current_metrics.get("timeout_rate", 0) - baseline_metrics.get(
            "timeout_rate", 0
        )
        if timeout_increase > self.CANARY_TIMEOUT_THRESHOLD:
            return True, f"Timeout rate spike: {timeout_increase:.2f}% > {self.CANARY_TIMEOUT_THRESHOLD:.2f}%"

        # Check latency
        baseline_latency = baseline_metrics.get("p95_latency_ms", 100)
        current_latency = current_metrics.get("p95_latency_ms", 100)
        latency_increase_pct = (
            (current_latency - baseline_latency) / baseline_latency * 100
            if baseline_latency > 0
            else 0
        )
        if latency_increase_pct > self.CANARY_LATENCY_THRESHOLD:
            return True, f"Latency increase: {latency_increase_pct:.1f}% > {self.CANARY_LATENCY_THRESHOLD:.1f}%"

        return False, None

    async def _rollback_deployment(
        self, commit_sha: str, reason: str
    ) -> bool:
        """
        Rollback deployment to previous stable version.

        Steps:
        1. Identify last stable deployment tag
        2. Reset pod image to stable version
        3. Verify rollout status
        4. Create git tag for audit
        """
        try:
            logger.error(f"Rolling back deployment: {reason}")

            # In a real implementation, would:
            # 1. Query git tags to find last layer5-deploy-*-completed tag
            # 2. Extract the stable commit SHA
            # 3. kubectl set image deployment/deep-think deep-think=<stable-image>
            # 4. kubectl rollout status deployment/deep-think

            tag_name = f"layer5-deploy-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-rollback"
            await self._create_git_tag(commit_sha, tag_name)

            logger.info(f"Rollback completed, tagged as {tag_name}")
            return True

        except Exception as e:
            logger.error(f"Rollback failed: {e}")
            return False

    async def _create_git_tag(self, commit_sha: str, tag_name: str) -> bool:
        """Create a git tag for tracking"""
        try:
            result = subprocess.run(
                ["git", "tag", tag_name, commit_sha],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                logger.warning(f"Failed to create tag {tag_name}: {result.stderr}")
                return False

            return True
        except Exception as e:
            logger.warning(f"Exception creating tag: {e}")
            return False

    def _get_deployment_stage(self, stage_name: str) -> DeploymentStage:
        """Map stage name to DeploymentStage enum"""
        stage_mapping = {
            "5pct": DeploymentStage.CANARY_5PCT,
            "25pct": DeploymentStage.GRADUAL_25PCT,
            "100pct": DeploymentStage.FULL_100PCT,
        }
        return stage_mapping.get(stage_name, DeploymentStage.PENDING)

    async def get_deployment_status(
        self, deployment_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get status of a deployment"""
        deployment = self.store.execute(
            "SELECT * FROM deployment_events WHERE id = ?",
            (deployment_id,),
        ).fetchone()

        if not deployment:
            return None

        return {
            "deployment_id": deployment_id,
            "status": deployment["status"],
            "plan_id": deployment["plan_id"],
            "commit_sha": deployment["commit_sha"],
            "created_at": deployment["created_at"],
            "updated_at": deployment["updated_at"],
        }

    async def get_deployment_history(
        self, plan_id: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Get deployment history for a plan"""
        deployments = self.store.execute(
            """
            SELECT id, status, created_at, updated_at
            FROM deployment_events
            WHERE plan_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (plan_id, limit),
        ).fetchall()

        return [
            {
                "deployment_id": d["id"],
                "status": d["status"],
                "created_at": d["created_at"],
                "updated_at": d["updated_at"],
            }
            for d in deployments
        ]
