"""Planning Engine for Self-Improvement System

Analyzes findings and generates ranked improvement plans using deep_think.
Integrates with the MCP server to create structured roadmaps.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from . import store

logger = logging.getLogger(__name__)


class RiskLevel(Enum):
    """Risk level for fix implementations."""
    LOW = 1
    MEDIUM = 2
    HIGH = 3


@dataclass
class FixApproach:
    """Structured fix approach from deep_think planning."""
    root_cause: str
    primary_strategy: str
    fallback_strategy: Optional[str]
    effort_estimate: int  # 1-5 days
    risk_level: str  # LOW, MEDIUM, HIGH
    dependencies: List[str]  # Other finding IDs
    subtasks: List[str]
    validation_tests: List[str]
    estimated_cost_tokens: int


class PlanningEngine:
    """Generates improvement plans for findings using deep_think."""

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

    def __init__(
        self,
        deep_think_fn,
        max_concurrent_plans: int = 3,
        plan_timeout_secs: float = 120.0,
    ):
        """Initialize planning engine.
        
        Args:
            deep_think_fn: Async function to call deep_think reasoning
            max_concurrent_plans: Max concurrent plan generations
            plan_timeout_secs: Timeout per plan generation
        """
        self.deep_think_fn = deep_think_fn
        self.max_concurrent_plans = max_concurrent_plans
        self.plan_timeout_secs = plan_timeout_secs
        self.semaphore = asyncio.Semaphore(max_concurrent_plans)

    def _compute_priority(self, finding: Dict[str, Any]) -> float:
        """Compute priority score for a finding.
        
        Priority = (severity_weight × impact) / (effort_penalty × risk_penalty)
        Higher score = higher priority to fix
        """
        severity = finding.get("severity", "MEDIUM")
        impact = finding.get("impact", 1.0)  # 0-10 scale
        reproducibility = finding.get("reproducibility", 0.5)

        # Base numerator: severity × impact × reproducibility
        numerator = self.SEVERITY_WEIGHTS.get(severity, 1.0) * impact * reproducibility

        # Estimated effort (inferred or from finding)
        effort_estimate = finding.get("effort_estimate", 3)
        effort_estimate = min(5, max(1, int(effort_estimate)))

        # Denominator: effort_penalty × risk_penalty
        risk_level = finding.get("risk_level", "MEDIUM")
        risk_penalty = self.RISK_PENALTY.get(
            RiskLevel[risk_level] if isinstance(risk_level, str) else risk_level,
            1.5
        )
        denominator = (
            self.EFFORT_PENALTY.get(effort_estimate, 1.5) * risk_penalty
        )

        priority = numerator / denominator
        return priority

    def _build_planning_prompt(self, finding: Dict[str, Any]) -> str:
        """Build the prompt for deep_think planning task."""
        return f"""You are a planning expert analyzing an issue to create a structured fix roadmap.

**Issue Details:**
- Category: {finding.get('category', 'UNKNOWN')}
- Severity: {finding.get('severity', 'MEDIUM')}
- Reproducibility: {finding.get('reproducibility', 0.5):.1%}
- Impact: {finding.get('impact', 5)}/10
- Description: {finding.get('description', 'N/A')}

**Current State:**
{finding.get('details', 'N/A')}

**Your Task:**
Generate a structured improvement plan with:
1. Root cause analysis (what's the fundamental issue?)
2. Primary fix strategy (high-level approach)
3. Fallback strategy (if primary fails)
4. Effort estimate (1-5 days)
5. Risk assessment (LOW/MEDIUM/HIGH)
6. Dependencies (other issues that must be fixed first)
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
  "subtasks": ["step 1", "step 2"],
  "validation_tests": ["test 1"],
  "estimated_cost_tokens": 5000
}}"""

    async def _call_deep_think_planning(
        self,
        prompt: str,
    ) -> Optional[FixApproach]:
        """Call deep_think with planning task_class."""
        try:
            result = await asyncio.wait_for(
                self.deep_think_fn(
                    question=prompt,
                    task_class="planning",
                    passes=2,
                    data_policy="local",
                ),
                timeout=self.plan_timeout_secs,
            )
            
            if not result or "error" in result:
                logger.error(f"deep_think planning failed: {result}")
                return None

            # Extract final_answer or result depending on structure
            response_text = result.get("final_answer") or result.get("result", "")
            if isinstance(response_text, dict):
                response_text = json.dumps(response_text)

            # Parse JSON response
            try:
                approach_json = json.loads(response_text)
            except json.JSONDecodeError:
                # Try to extract JSON from the response if wrapped
                import re
                json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if json_match:
                    approach_json = json.loads(json_match.group())
                else:
                    logger.error(f"Failed to parse response as JSON: {response_text[:200]}")
                    return None

            return FixApproach(
                root_cause=approach_json.get("root_cause", ""),
                primary_strategy=approach_json.get("primary_strategy", ""),
                fallback_strategy=approach_json.get("fallback_strategy"),
                effort_estimate=int(approach_json.get("effort_estimate", 3)),
                risk_level=approach_json.get("risk_level", "MEDIUM"),
                dependencies=approach_json.get("dependencies", []),
                subtasks=approach_json.get("subtasks", []),
                validation_tests=approach_json.get("validation_tests", []),
                estimated_cost_tokens=int(approach_json.get("estimated_cost_tokens", 5000)),
            )

        except asyncio.TimeoutError:
            logger.error("deep_think planning timed out")
            return None
        except Exception as e:
            logger.error(f"deep_think planning failed: {e}", exc_info=True)
            return None

    async def generate_plan(
        self,
        finding: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Generate a plan for a single finding using deep_think."""
        async with self.semaphore:
            try:
                plan_id = str(uuid.uuid4())
                
                # Compute priority
                priority_score = self._compute_priority(finding)
                
                # Build planning prompt
                prompt = self._build_planning_prompt(finding)

                # Call deep_think planning (will be queued asynchronously)
                logger.info(f"Queuing deep_think planning for finding {finding['id']}")
                approach = await self._call_deep_think_planning(prompt)

                if not approach:
                    logger.warning(f"Failed to generate approach for {finding['id']}")
                    return None

                # Store plan in database
                timestamp = datetime.now(timezone.utc).isoformat()

                plan_json = json.dumps(asdict(approach))
                
                store.create_plan(
                    plan_id=plan_id,
                    finding_ids=[finding["id"]],
                    plan_json=plan_json,
                    priority=priority_score,
                    effort_estimate=approach.effort_estimate,
                    risk_level=approach.risk_level,
                    deep_think_job_id="",
                )

                logger.info(
                    f"Created plan {plan_id} for finding {finding['id']} "
                    f"(priority={priority_score:.2f}, effort={approach.effort_estimate}d)"
                )

                return {
                    "plan_id": plan_id,
                    "finding_id": finding["id"],
                    "priority": priority_score,
                    "effort_estimate": approach.effort_estimate,
                    "risk_level": approach.risk_level,
                    "status": "pending",
                    "created_at": timestamp,
                }

            except Exception as e:
                logger.error(
                    f"Exception planning finding {finding.get('id', 'UNKNOWN')}: {e}",
                    exc_info=True,
                )
                return None

    async def generate_plans_for_findings(
        self,
        findings: List[Dict[str, Any]],
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Generate improvement plans for findings.
        
        Args:
            findings: List of finding dictionaries
            limit: Max plans to generate
            
        Returns:
            List of created plan metadata
        """
        if not findings:
            logger.info("No findings provided for planning")
            return []

        # Compute priorities and sort
        prioritized = [
            (f, self._compute_priority(f)) for f in findings
        ]
        prioritized.sort(key=lambda x: x[1], reverse=True)

        # Create plans concurrently
        tasks = []
        for finding, _ in prioritized[:limit]:
            task = asyncio.create_task(self.generate_plan(finding))
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        plans = []
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

    async def get_pending_plans(self) -> List[Dict[str, Any]]:
        """Fetch all pending plans awaiting approval or implementation."""
        plans = store.list_plans(status="pending", limit=100)
        return [
            {
                "plan_id": p["id"],
                "finding_ids": json.loads(p["finding_ids"]),
                "priority": p["priority"],
                "effort_estimate": p["effort_estimate"],
                "risk_level": p["risk_level"],
                "status": p["status"],
                "created_at": p["created_at"],
            }
            for p in plans
        ]

    async def approve_plan(
        self,
        plan_id: str,
        approved_by: str,
        approval_notes: str = "",
    ) -> bool:
        """Approve a plan for implementation."""
        try:
            store.update_plan_status(plan_id, "approved", approved_by)
            
            plan = store.get_plan(plan_id)
            if plan and approval_notes:
                store.audit_log(
                    "plan_approval_notes",
                    plan_id,
                    json.dumps({"notes": approval_notes}),
                )
            
            logger.info(f"Plan {plan_id} approved by {approved_by}")
            return True

        except Exception as e:
            logger.error(f"Failed to approve plan {plan_id}: {e}", exc_info=True)
            return False

    async def reject_plan(
        self,
        plan_id: str,
        rejected_by: str,
        reason: str = "",
    ) -> bool:
        """Reject a plan."""
        try:
            store.update_plan_status(plan_id, "rejected")
            store.audit_log(
                "plan_rejected",
                plan_id,
                json.dumps({"rejected_by": rejected_by, "reason": reason}),
            )

            logger.info(f"Plan {plan_id} rejected by {rejected_by}: {reason}")
            return True

        except Exception as e:
            logger.error(f"Failed to reject plan {plan_id}: {e}", exc_info=True)
            return False
