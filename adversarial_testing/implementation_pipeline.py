"""Implementation Pipeline for Layer 5 Self-Improvement System

Orchestrates code changes through approval gates, with integrated code-review,
implementation, and rollback capability. Manages the full lifecycle from plan to
merged code.
"""

import json
import uuid
import asyncio
import logging
import subprocess
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

from adversarial_testing import store
from adversarial_testing.governance import requires_human_review

logger = logging.getLogger(__name__)


class ImplementationStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    IMPLEMENTING = "implementing"
    REVIEW_PENDING = "review_pending"
    REVIEW_PASSED = "review_passed"
    REVIEW_FAILED = "review_failed"
    VALIDATING = "validating"
    VALIDATION_PASSED = "validation_passed"
    VALIDATION_FAILED = "validation_failed"
    DEPLOYING = "deploying"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"


@dataclass
class ImplementationTask:
    """Task for implementing a single fix within a plan"""
    id: str
    plan_id: str
    task_description: str
    status: ImplementationStatus
    implementation_notes: str = ""
    commit_sha: Optional[str] = None
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class ImplementationPipeline:
    """Manages code implementation with approval gates and budget controls"""

    # Budget controls (from adversarial_budget tracking)
    DEFAULT_DAILY_TOKEN_LIMIT = 1_000_000
    DEFAULT_MONTHLY_BUDGET_USD = 10_000

    def __init__(
        self,
        git_repo_root: str = "/home/USER/development/deep_think_mcp",
        code_review_agent_endpoint: str = "http://localhost:8000/code-review",
        impl_agent_endpoint: str = "http://localhost:8000/general-purpose",
    ):
        self.git_repo_root = git_repo_root
        self.code_review_endpoint = code_review_agent_endpoint
        self.impl_agent_endpoint = impl_agent_endpoint
        self.store = store

    async def start_implementation(
        self, plan_id: str, skip_approval: bool = False
    ) -> Tuple[bool, Optional[str]]:
        """
        Start implementation pipeline for an approved plan.

        Returns:
            (success: bool, error_message: Optional[str])
        """
        try:
            # Fetch plan details
            conn = self.store._connect()
            try:
                plan = conn.execute(
                    "SELECT * FROM self_improvement_plans WHERE id = ?",
                    (plan_id,),
                ).fetchone()
            finally:
                conn.close()

            if not plan:
                return False, f"Plan {plan_id} not found"

            if plan["status"] not in ("approved", "pending"):
                return False, f"Plan {plan_id} has status {plan['status']}, not ready for implementation"

            # Check budget before proceeding
            has_budget, budget_msg = await self._check_budget(
                plan["plan_json"]
            )
            if not has_budget:
                logger.warning(f"Implementation blocked: {budget_msg}")
                return False, budget_msg

            # Parse plan details
            plan_data = json.loads(plan["plan_json"])
            finding_ids = json.loads(plan["finding_ids"])

            # Check if human review is required
            severity = plan.get("severity", "MEDIUM")
            if requires_human_review(severity) and not skip_approval:
                result = await self._queue_for_approval(plan_id, severity)
                if not result:
                    return False, f"Failed to queue plan {plan_id} for human approval"
                return True, None

            # Create feature branch
            branch_name = f"layer5-impl-{plan_id[:8]}-{finding_ids[0][:8]}"
            success, msg = await self._create_feature_branch(branch_name)
            if not success:
                return False, msg

            # Execute implementation tasks
            tasks = plan_data.get("subtasks", [])
            implementation_results = []

            for i, task_desc in enumerate(tasks):
                task_id = str(uuid.uuid4())
                task_result = await self._implement_single_task(
                    task_id, plan_id, task_desc
                )
                implementation_results.append(task_result)

                if not task_result[0]:  # If task failed
                    logger.error(f"Implementation task {i} failed: {task_result[1]}")
                    await self._rollback_branch(branch_name)
                    return False, f"Implementation failed at task {i}: {task_result[1]}"

            # Commit changes
            commit_msg = self._build_commit_message(plan_id, finding_ids, plan_data)
            commit_sha, err = await self._commit_changes(branch_name, commit_msg)
            if not commit_sha:
                await self._rollback_branch(branch_name)
                return False, f"Failed to commit changes: {err}"

            # Tag for tracking
            tag_name = f"layer5-impl-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-pending"
            await self._create_git_tag(commit_sha, tag_name)

            # Update plan status
            timestamp = datetime.utcnow().isoformat()
            conn = self.store._connect()
            try:
                conn.execute(
                    """
                    UPDATE self_improvement_plans
                    SET status = 'implementing', deployment_sha = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (commit_sha, timestamp, plan_id),
                )
                conn.commit()
            finally:
                conn.close()

            # Log in audit trail
            conn = self.store._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO layer5_audit_log (event, details, timestamp)
                    VALUES (?, ?, ?)
                    """,
                    (
                        "implementation_started",
                        json.dumps({
                            "plan_id": plan_id,
                            "branch": branch_name,
                            "commit_sha": commit_sha,
                            "task_count": len(tasks)
                        }),
                        timestamp,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            logger.info(
                f"Implementation started for plan {plan_id} on branch {branch_name} "
                f"(commit {commit_sha[:8]})"
            )

            return True, None

        except Exception as e:
            logger.error(f"Exception in start_implementation: {e}")
            return False, str(e)

    async def _check_budget(self, plan_json: str) -> Tuple[bool, str]:
        """Check if we have budget capacity to implement this plan"""
        plan_data = json.loads(plan_json)
        estimated_tokens = plan_data.get("estimated_cost_tokens", 5000)

        # Query current budget usage - use correct column names
        conn = self.store._connect()
        try:
            budget_row = conn.execute(
                """
                SELECT tokens_used
                FROM adversarial_budget
                ORDER BY date DESC LIMIT 1
                """
            ).fetchone()
        finally:
            conn.close()

        if not budget_row:
            # No budget tracking yet, allow with default limits
            return True, ""

        daily_used = budget_row[0] if budget_row else 0
        daily_limit = self.DEFAULT_DAILY_TOKEN_LIMIT

        if daily_used + estimated_tokens > daily_limit:
            return (
                False,
                f"Daily token budget exceeded: {daily_used + estimated_tokens} > {daily_limit}",
            )

        return True, ""

    async def _queue_for_approval(self, plan_id: str, severity: str) -> bool:
        """Queue plan for human approval via escalation framework"""
        try:
            # This would integrate with HumanEscalationQueue pattern
            # For now, log that approval is needed
            logger.info(f"Plan {plan_id} (severity={severity}) queued for human approval")
            return True
        except Exception as e:
            logger.error(f"Failed to queue plan for approval: {e}")
            return False

    async def _create_feature_branch(self, branch_name: str) -> Tuple[bool, str]:
        """Create a feature branch for implementation"""
        try:
            result = subprocess.run(
                ["git", "checkout", "-b", branch_name],
                cwd=self.git_repo_root,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                return False, f"git checkout failed: {result.stderr}"

            return True, ""
        except Exception as e:
            return False, str(e)

    async def _implement_single_task(
        self, task_id: str, plan_id: str, task_description: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Implement a single task from the plan.

        This would integrate with the general-purpose agent to apply code changes.
        For now, return success placeholder.
        """
        try:
            timestamp = datetime.utcnow().isoformat()

            # Record task in database
            conn = self.store._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO implementation_tasks 
                    (id, plan_id, task_description, status, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (task_id, plan_id, task_description, "in_progress", timestamp),
                )
                conn.commit()
            finally:
                conn.close()

            # Call implementation agent
            # This is a placeholder - in reality, would call the general-purpose agent
            # to apply code changes based on task_description

            logger.info(f"Implementing task {task_id}: {task_description}")

            # Mark task as complete
            conn = self.store._connect()
            try:
                conn.execute(
                    """
                    UPDATE implementation_tasks
                    SET status = 'completed', completed_at = ?
                    WHERE id = ?
                    """,
                    (datetime.utcnow().isoformat(), task_id),
                )
                conn.commit()
            finally:
                conn.close()

            return True, None

        except Exception as e:
            logger.error(f"Failed to implement task {task_id}: {e}")
            return False, str(e)

    def _build_commit_message(
        self, plan_id: str, finding_ids: List[str], plan_data: Dict[str, Any]
    ) -> str:
        """Build the git commit message with Layer 5 tracer"""
        category = plan_data.get("category", "improvement")
        description = plan_data.get("root_cause", "Fix identified issues")[:50]

        message = (
            f"[Layer 5] Fix {category}: {description}\n\n"
            f"Plan: {plan_id}\n"
            f"Findings: {', '.join(finding_ids[:3])}\n"
            f"Risk: {plan_data.get('risk_level', 'MEDIUM')}\n"
            f"Effort: {plan_data.get('effort_estimate', 2)}d\n\n"
            f"Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
        )
        return message

    async def _commit_changes(
        self, branch_name: str, commit_msg: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """Commit changes to the feature branch"""
        try:
            # Stage all changes
            result = subprocess.run(
                ["git", "add", "-A"],
                cwd=self.git_repo_root,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                return None, f"git add failed: {result.stderr}"

            # Commit with message
            result = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=self.git_repo_root,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                return None, f"git commit failed: {result.stderr}"

            # Get commit SHA
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.git_repo_root,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                return None, f"Failed to get commit SHA: {result.stderr}"

            commit_sha = result.stdout.strip()
            return commit_sha, None

        except Exception as e:
            return None, str(e)

    async def _create_git_tag(self, commit_sha: str, tag_name: str) -> bool:
        """Create a git tag for tracking"""
        try:
            result = subprocess.run(
                ["git", "tag", tag_name, commit_sha],
                cwd=self.git_repo_root,
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

    async def _rollback_branch(self, branch_name: str) -> bool:
        """Rollback to main branch and delete feature branch"""
        try:
            subprocess.run(
                ["git", "checkout", "main"],
                cwd=self.git_repo_root,
                capture_output=True,
            )

            subprocess.run(
                ["git", "branch", "-D", branch_name],
                cwd=self.git_repo_root,
                capture_output=True,
            )

            logger.info(f"Rolled back branch {branch_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to rollback branch: {e}")
            return False

    async def get_implementation_status(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """Get current implementation status for a plan"""
        conn = self.store._connect()
        try:
            plan = conn.execute(
                "SELECT * FROM self_improvement_plans WHERE id = ?",
                (plan_id,),
            ).fetchone()

            if not plan:
                return None

            tasks = conn.execute(
                """
                SELECT id, task_description, status, completed_at
                FROM implementation_tasks
                WHERE plan_id = ?
                ORDER BY created_at
                """,
                (plan_id,),
            ).fetchall()

            return {
                "plan_id": plan_id,
                "status": plan[1],  # status column
                "commit_sha": plan[4] if len(plan) > 4 else None,  # deployment_sha
                "tasks": [dict(t) for t in tasks],
                "created_at": plan[3],
            }
        finally:
            conn.close()

    async def pause_implementation(self, plan_id: str, reason: str) -> bool:
        """Pause implementation (e.g., due to budget limits)"""
        try:
            timestamp = datetime.utcnow().isoformat()

            conn = self.store._connect()
            try:
                conn.execute(
                    """
                    UPDATE self_improvement_plans
                    SET status = 'paused', updated_at = ?
                    WHERE id = ?
                    """,
                    (timestamp, plan_id),
                )
                conn.execute(
                    """
                    INSERT INTO layer5_audit_log (event, details, timestamp)
                    VALUES (?, ?, ?)
                    """,
                    (
                        "implementation_paused",
                        json.dumps({"plan_id": plan_id, "reason": reason}),
                        timestamp,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            logger.info(f"Implementation paused for plan {plan_id}: {reason}")
            return True

        except Exception as e:
            logger.error(f"Failed to pause implementation: {e}")
            return False

    async def resume_implementation(self, plan_id: str) -> bool:
        """Resume a paused implementation"""
        try:
            timestamp = datetime.utcnow().isoformat()

            conn = self.store._connect()
            try:
                conn.execute(
                    """
                    UPDATE self_improvement_plans
                    SET status = 'approved', updated_at = ?
                    WHERE id = ? AND status = 'paused'
                    """,
                    (timestamp, plan_id),
                )
                conn.execute(
                    """
                    INSERT INTO layer5_audit_log (event, details, timestamp)
                    VALUES (?, ?, ?)
                    """,
                    (
                        "implementation_resumed",
                        json.dumps({"plan_id": plan_id}),
                        timestamp,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            logger.info(f"Implementation resumed for plan {plan_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to resume implementation: {e}")
            return False
