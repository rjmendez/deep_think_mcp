"""Planning Engine for Layer 5 Self-Improvement System

Analyzes high-priority findings and generates ranked improvement plans using deep_think.
Integrates with the existing deep_think infrastructure to create structured roadmaps.
"""

import json
import uuid
import asyncio
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from enum import Enum

import aiohttp
from . import store
from .metrics import MetricsCollector

logger = logging.getLogger(__name__)

# Risk levels for fix implementations
class RiskLevel(Enum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


@dataclass
class PlanMetadata:
    """Metadata for a self-improvement plan"""
    id: str
    finding_ids: List[str]
    severity_levels: List[str]
    created_at: datetime
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    deployment_sha: Optional[str] = None
    status: str = "pending"  # pending, approved, implementing, validating, deployed, rolled_back


@dataclass
class FixApproach:
    """Structured fix approach from deep_think planning"""
    root_cause: str
    primary_strategy: str
    fallback_strategy: Optional[str]
    effort_estimate: int  # 1-5 days
    risk_level: RiskLevel
    dependencies: List[str]  # Other finding IDs that must be fixed first
    subtasks: List[str]
    validation_tests: List[str]
    estimated_cost_tokens: int


class PlanningEngine:
    """Generates improvement plans for adversarial findings using deep_think"""

    # Thresholds for plan prioritization
    SEVERITY_WEIGHTS = {
        "CRITICAL": 3.0,
        "HIGH": 2.0,
        "MEDIUM": 1.0,
        "LOW": 0.3,
    }

    RISK_PENALTY = {
        RiskLevel.LOW: 1.0,
        RiskLevel.MEDIUM: 1.5,
        RiskLevel.HIGH: 2.5,
    }

    EFFORT_PENALTY = {
        1: 1.0,
        2: 1.2,
        3: 1.5,
        4: 2.0,
        5: 3.0,
    }

    MIN_REPRODUCIBILITY = 0.7  # Only plan fixes for highly reproducible findings

    def __init__(
        self,
        metrics: MetricsCollector,
        deep_think_endpoint: str = "http://localhost:8000/think",
        max_concurrent_plans: int = 3,
    ):
        self.metrics = metrics
        self.deep_think_endpoint = deep_think_endpoint
        self.max_concurrent_plans = max_concurrent_plans
        self.semaphore = asyncio.Semaphore(max_concurrent_plans)

    async def generate_plans_for_findings(
        self, limit: int = 5, exclude_dependencies: bool = True
    ) -> List[PlanMetadata]:
        """
        Generate improvement plans for top-priority findings.

        Args:
            limit: Maximum number of plans to generate
            exclude_dependencies: Skip findings that depend on others not yet fixed

        Returns:
            List of created plans with metadata
        """
        # Fetch top findings by priority
        findings = store.list_findings(
            status="unresolved",
            limit=limit * 2,  # Fetch extra in case we filter some out
        )

        # Filter out low-confidence findings
        viable_findings = [
            f for f in findings if f.get("reproducibility", 0) >= self.MIN_REPRODUCIBILITY
        ]

        if not viable_findings:
            logger.info("No highly reproducible findings available for planning")
            return []

        # Compute priority scores and select top N
        prioritized = [
            (f, self._compute_priority(f)) for f in viable_findings[:limit]
        ]
        prioritized.sort(key=lambda x: x[1], reverse=True)

        # Create plans concurrently
        plans = []
        tasks = []

        for finding, priority_score in prioritized[:limit]:
            task = asyncio.create_task(
                self._plan_single_finding(finding, priority_score)
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for finding, result in zip([f for f, _ in prioritized[:limit]], results):
            if isinstance(result, Exception):
                logger.error(
                    f"Failed to plan finding {finding['id']}: {result}",
                    exc_info=result,
                )
                continue

            if result:
                plans.append(result)

        return plans

    async def _plan_single_finding(
        self, finding: Dict[str, Any], priority_score: float
    ) -> Optional[PlanMetadata]:
        """Generate a plan for a single finding using deep_think"""
        async with self.semaphore:
            try:
                # Get code context for the finding
                code_context = await self._fetch_code_context(finding)

                # Build prompt for planning
                prompt = self._build_planning_prompt(finding, code_context)

                # Call deep_think with planning task_class
                approach = await self._call_deep_think_planning(prompt)

                if not approach:
                    logger.warning(f"Failed to generate approach for {finding['id']}")
                    return None

                # Store plan in database
                plan_id = str(uuid.uuid4())
                timestamp = datetime.utcnow().isoformat()

                plan_data = {
                    "id": plan_id,
                    "finding_ids": json.dumps([finding["id"]]),
                    "plan_json": json.dumps(asdict(approach)),
                    "priority": priority_score,
                    "effort_estimate": approach.effort_estimate,
                    "risk_level": approach.risk_level.name,
                    "status": "pending",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }

                # Insert into database using store module
                conn = store._connect()
                try:
                    conn.execute(
                        """
                        INSERT INTO self_improvement_plans 
                        (id, finding_ids, plan_json, priority, effort_estimate, risk_level, 
                         status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            plan_data["id"],
                            plan_data["finding_ids"],
                            plan_data["plan_json"],
                            plan_data["priority"],
                            plan_data["effort_estimate"],
                            plan_data["risk_level"],
                            plan_data["status"],
                            plan_data["created_at"],
                            plan_data["updated_at"],
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                # Log in audit trail
                store.audit_log(
                    event_type="plan_created",
                    finding_id=finding["id"],
                    details_json=json.dumps({"plan_id": plan_id, "priority": priority_score}),
                )

                logger.info(
                    f"Created plan {plan_id} for finding {finding['id']} "
                    f"(priority={priority_score:.2f}, effort={approach.effort_estimate}d)"
                )

                return PlanMetadata(
                    id=plan_id,
                    finding_ids=[finding["id"]],
                    severity_levels=[finding.get("severity", "MEDIUM")],
                    created_at=datetime.utcnow(),
                    status="pending",
                )

            except Exception as e:
                logger.error(f"Exception planning finding {finding['id']}: {e}")
                return None

    def _compute_priority(self, finding: Dict[str, Any]) -> float:
        """
        Compute priority score for a finding.

        Priority = (severity_weight × impact) / (effort_penalty × risk_penalty)

        Higher score = higher priority to fix
        """
        severity = finding.get("severity", "MEDIUM")
        impact = finding.get("impact", 1.0)  # 0-10 scale
        reproducibility = finding.get("reproducibility", 0.5)

        # Base numerator: severity × impact × reproducibility
        numerator = self.SEVERITY_WEIGHTS.get(severity, 1.0) * impact * reproducibility

        # Estimated effort (inferred from historical time-to-fix)
        metrics_snapshot = self.metrics.snapshot()
        historical_ttf = metrics_snapshot.get("avg_time_to_fix_days", 3)
        effort_estimate = min(5, max(1, int(historical_ttf)))

        # Denominator: effort_penalty × risk_penalty
        # Assume MEDIUM risk by default (will be refined by deep_think)
        denominator = (
            self.EFFORT_PENALTY.get(effort_estimate, 1.5)
            * self.RISK_PENALTY[RiskLevel.MEDIUM]
        )

        priority = numerator / denominator

        return priority

    async def _fetch_code_context(self, finding: Dict[str, Any]) -> Dict[str, str]:
        """Fetch code context (module, recent commits, affected lines)"""
        # This would integrate with git to fetch:
        # - Last 5 commits touching affected module
        # - Lines of code around the error
        # - Test cases for this finding category

        # For now, return placeholder
        return {
            "module": finding.get("category", "unknown"),
            "affected_lines": "...",  # Would be fetched from git
            "recent_commits": "...",  # Last 5 commits
            "test_examples": finding.get("example_input", ""),
        }

    def _build_planning_prompt(
        self, finding: Dict[str, Any], code_context: Dict[str, str]
    ) -> str:
        """Build the prompt for deep_think planning task"""
        return f"""You are a planning expert analyzing an adversarial finding to create a structured fix roadmap.

**Finding Details:**
- Category: {finding.get('category', 'UNKNOWN')}
- Severity: {finding.get('severity', 'MEDIUM')}
- Reproducibility: {finding.get('reproducibility', 0.5):.1%}
- Impact: {finding.get('impact', 5)}/10

**Error Information:**
{finding.get('example_input', 'N/A')}

**Affected Module:**
{code_context.get('module', 'unknown')}

**Code Context:**
Recent commits: {code_context.get('recent_commits', 'N/A')}

**Your Task:**
Generate a structured improvement plan with:
1. Root cause analysis (what's the fundamental issue?)
2. Primary fix strategy (high-level approach)
3. Fallback strategy (if primary fails)
4. Effort estimate (1-5 days, with daily breakdown)
5. Risk assessment (regression risk: LOW/MEDIUM/HIGH)
6. Dependencies (other findings that must be fixed first)
7. Subtasks (concrete steps to implement)
8. Validation tests (how to verify the fix works)
9. Estimated API token cost

Output ONLY valid JSON (no markdown, no explanation):
{{
  "root_cause": "...",
  "primary_strategy": "...",
  "fallback_strategy": "...",
  "effort_estimate": 2,
  "risk_level": "MEDIUM",
  "dependencies": [],
  "subtasks": ["step 1", "step 2", ...],
  "validation_tests": ["test 1", "test 2", ...],
  "estimated_cost_tokens": 5000
}}"""

    async def _call_deep_think_planning(self, prompt: str) -> Optional[FixApproach]:
        """Call deep_think with planning task_class"""
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "question": prompt,
                    "task_class": "planning",
                    "passes": 2,  # Planning uses fewer passes than general reasoning
                }

                async with session.post(
                    self.deep_think_endpoint,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"deep_think returned {resp.status}")
                        return None

                    result = await resp.json()
                    response_text = result.get("final_answer", "")

                    # Parse JSON response
                    approach_json = json.loads(response_text)

                    risk_level = RiskLevel[approach_json.get("risk_level", "MEDIUM")]

                    return FixApproach(
                        root_cause=approach_json["root_cause"],
                        primary_strategy=approach_json["primary_strategy"],
                        fallback_strategy=approach_json.get("fallback_strategy"),
                        effort_estimate=approach_json["effort_estimate"],
                        risk_level=risk_level,
                        dependencies=approach_json.get("dependencies", []),
                        subtasks=approach_json.get("subtasks", []),
                        validation_tests=approach_json.get("validation_tests", []),
                        estimated_cost_tokens=approach_json.get(
                            "estimated_cost_tokens", 5000
                        ),
                    )

        except asyncio.TimeoutError:
            logger.error("deep_think planning timed out")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse deep_think response as JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"deep_think planning failed: {e}")
            return None

    async def get_pending_plans(self) -> List[Dict[str, Any]]:
        """Fetch all pending plans awaiting approval or implementation"""
        conn = store._connect()
        try:
            rows = conn.execute(
                """
                SELECT id, finding_ids, plan_json, priority, effort_estimate, risk_level, status
                FROM self_improvement_plans
                WHERE status IN ('pending', 'approved')
                ORDER BY priority DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    async def approve_plan(
        self, plan_id: str, approved_by: str, notes: str = ""
    ) -> bool:
        """Approve a plan for implementation"""
        try:
            timestamp = datetime.utcnow().isoformat()

            conn = store._connect()
            try:
                conn.execute(
                    """
                    UPDATE self_improvement_plans
                    SET status = 'approved', approved_by = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (approved_by, timestamp, plan_id),
                )
                conn.commit()
            finally:
                conn.close()

            # Log approval in audit trail
            store.audit_log(
                event_type="plan_approved",
                details_json=json.dumps({
                    "plan_id": plan_id,
                    "approved_by": approved_by,
                    "notes": notes
                }),
            )

            logger.info(f"Plan {plan_id} approved by {approved_by}")
            return True

        except Exception as e:
            logger.error(f"Failed to approve plan {plan_id}: {e}")
            return False

    async def reject_plan(self, plan_id: str, rejected_by: str, reason: str) -> bool:
        """Reject a plan and close associated findings"""
        try:
            timestamp = datetime.utcnow().isoformat()

            conn = store._connect()
            try:
                conn.execute(
                    """
                    UPDATE self_improvement_plans
                    SET status = 'rejected', updated_at = ?
                    WHERE id = ?
                    """,
                    (timestamp, plan_id),
                )
                conn.commit()
            finally:
                conn.close()

            # Log rejection
            store.audit_log(
                event_type="plan_rejected",
                details_json=json.dumps({
                    "plan_id": plan_id,
                    "rejected_by": rejected_by,
                    "reason": reason
                }),
            )

            logger.info(f"Plan {plan_id} rejected by {rejected_by}: {reason}")
            return True

        except Exception as e:
            logger.error(f"Failed to reject plan {plan_id}: {e}")
            return False
