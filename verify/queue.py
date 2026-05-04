"""Job queue for asynchronous claim verification."""

import asyncio
import logging
import sqlite3
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import VerifyConfig
from .provider import LLMProvider, VerifyResult

log = logging.getLogger(__name__)


@dataclass
class VerifyJob:
    """A claim verification job."""

    id: str
    claim: str
    context: Optional[str]
    provider: str  # "cloud" or "local"
    status: str  # "queued", "processing", "done", "failed"
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class VerifyJobQueue:
    """SQLite-backed job queue for verification."""

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize job queue.

        Args:
            db_path: Path to SQLite database (default: ~/.deep_think/verify.db)
        """
        if db_path is None:
            db_path = (
                Path.home() / ".deep_think" / "verify.db"
            )
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS verify_jobs (
                    id TEXT PRIMARY KEY,
                    claim TEXT NOT NULL,
                    context TEXT,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_status ON verify_jobs(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_created_at ON verify_jobs(created_at)"
            )
            conn.commit()

    def create_job(
        self, claim: str, provider: str, context: Optional[str] = None
    ) -> str:
        """Create a new verification job.

        Args:
            claim: Claim to verify
            provider: "cloud" or "local"
            context: Optional context for grounding

        Returns:
            Job ID
        """
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO verify_jobs
                (id, claim, context, provider, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job_id, claim, context, provider, "queued", now),
            )
            conn.commit()

        log.info(
            "Created verification job %s (provider=%s, claim_len=%d)",
            job_id,
            provider,
            len(claim),
        )
        return job_id

    def claim_next_job(self, worker_id: str) -> Optional[VerifyJob]:
        """Claim the next queued job for processing.

        Args:
            worker_id: Worker identifier

        Returns:
            VerifyJob if available, None otherwise
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT * FROM verify_jobs
                    WHERE status = 'queued'
                    ORDER BY created_at ASC
                    LIMIT 1
                    """
                ).fetchone()

                if not row:
                    conn.execute("ROLLBACK")
                    return None

                now = datetime.now(timezone.utc).isoformat()
                job_id = row[0]

                conn.execute(
                    """
                    UPDATE verify_jobs
                    SET status = 'processing', started_at = ?
                    WHERE id = ?
                    """,
                    (now, job_id),
                )
                conn.commit()

                return VerifyJob(
                    id=row[0],
                    claim=row[1],
                    context=row[2],
                    provider=row[3],
                    status="processing",
                    result=None,
                    error=None,
                    created_at=row[7],
                    started_at=now,
                    completed_at=None,
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def complete_job(self, job_id: str, result: VerifyResult):
        """Mark job as complete with result.

        Args:
            job_id: Job ID
            result: VerifyResult
        """
        now = datetime.now(timezone.utc).isoformat()
        result_json = json.dumps(result.to_dict())

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE verify_jobs
                SET status = 'done', result = ?, completed_at = ?
                WHERE id = ?
                """,
                (result_json, now, job_id),
            )
            conn.commit()

        log.info(
            "Completed verification job %s (verdict=%s, confidence=%.1f)",
            job_id,
            result.verdict,
            result.confidence,
        )

    def fail_job(self, job_id: str, error: str):
        """Mark job as failed.

        Args:
            job_id: Job ID
            error: Error message
        """
        now = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE verify_jobs
                SET status = 'failed', error = ?, completed_at = ?
                WHERE id = ?
                """,
                (error, now, job_id),
            )
            conn.commit()

        log.warning("Failed verification job %s: %s", job_id, error)

    def get_status(self, job_id: str) -> Optional[dict]:
        """Get job status.

        Args:
            job_id: Job ID

        Returns:
            Job status dict or None if not found
        """
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM verify_jobs WHERE id = ?", (job_id,)
            ).fetchone()

        if not row:
            return None

        result = None
        if row[5]:  # result column
            import json
            result = json.loads(row[5])

        return {
            "job_id": row[0],
            "claim": row[1],
            "context": row[2],
            "provider": row[3],
            "status": row[4],
            "result": result,
            "error": row[6],
            "created_at": row[7],
            "started_at": row[8],
            "completed_at": row[9],
        }

    def get_metrics(self) -> dict:
        """Get queue metrics for health and diagnostic purposes.

        Returns:
            Dict with:
            - queue_depth: number of queued jobs
            - processing: number of jobs currently processing
            - completed: total completed jobs
            - failed: total failed jobs
            - avg_latency: average job duration in seconds or None
            - p95_latency: 95th percentile latency in seconds or None
            - completion_rate: percentage of jobs completed (0-100)
        """
        with sqlite3.connect(self.db_path) as conn:
            # Count jobs by status
            queued = conn.execute(
                "SELECT COUNT(*) FROM verify_jobs WHERE status = 'queued'"
            ).fetchone()[0]
            
            processing = conn.execute(
                "SELECT COUNT(*) FROM verify_jobs WHERE status = 'processing'"
            ).fetchone()[0]
            
            completed = conn.execute(
                "SELECT COUNT(*) FROM verify_jobs WHERE status = 'done'"
            ).fetchone()[0]
            
            failed = conn.execute(
                "SELECT COUNT(*) FROM verify_jobs WHERE status = 'failed'"
            ).fetchone()[0]
            
            # Calculate average latency
            latency_rows = conn.execute(
                """
                SELECT CAST((julianday(completed_at) - julianday(created_at)) * 86400.0 AS FLOAT)
                FROM verify_jobs
                WHERE status = 'done'
                ORDER BY completed_at DESC
                LIMIT 100
                """
            ).fetchall()
            
            avg_latency = None
            p95_latency = None
            
            if latency_rows:
                latencies = [row[0] for row in latency_rows if row[0] is not None]
                if latencies:
                    avg_latency = sum(latencies) / len(latencies)
                    latencies.sort()
                    # Calculate 95th percentile
                    idx = int(len(latencies) * 0.95)
                    if idx > 0:
                        p95_latency = latencies[idx]
        
        total = queued + processing + completed + failed
        completion_rate = 0
        if total > 0:
            completion_rate = (completed / total) * 100
        
        return {
            "queue_depth": queued,
            "processing": processing,
            "completed": completed,
            "failed": failed,
            "avg_latency": round(avg_latency, 2) if avg_latency else None,
            "p95_latency": round(p95_latency, 2) if p95_latency else None,
            "completion_rate": round(completion_rate, 1),
        }


class VerifyWorker:
    """Background worker for processing verification jobs."""

    def __init__(
        self,
        queue: VerifyJobQueue,
        cloud_provider: Optional[LLMProvider],
        local_provider: Optional[LLMProvider],
        config: VerifyConfig,
    ):
        """Initialize worker.

        Args:
            queue: VerifyJobQueue instance
            cloud_provider: CloudProvider or None
            local_provider: LocalProvider or None
            config: VerifyConfig
        """
        self.queue = queue
        self.cloud_provider = cloud_provider
        self.local_provider = local_provider
        self.config = config
        self.running = False
        self.task = None

    async def start(self):
        """Start worker background task."""
        if self.running:
            return
        self.running = True
        self.task = asyncio.create_task(self._run())
        log.info("Verification worker started")

    async def stop(self):
        """Stop worker gracefully."""
        if not self.running:
            return
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        log.info("Verification worker stopped")

    async def _run(self):
        """Worker event loop."""
        worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        log.info("Worker %s started", worker_id)

        while self.running:
            try:
                job = self.queue.claim_next_job(worker_id)
                if not job:
                    await asyncio.sleep(0.5)
                    continue

                provider = self._get_provider(job.provider)
                if not provider:
                    self.queue.fail_job(
                        job.id, f"Provider not available: {job.provider}"
                    )
                    continue

                try:
                    result = await provider.verify_claim(
                        job.claim, job.context
                    )
                    self.queue.complete_job(job.id, result)
                except asyncio.TimeoutError:
                    self.queue.fail_job(job.id, "Verification timed out")
                except Exception as e:
                    self.queue.fail_job(job.id, str(e))

            except Exception as e:
                log.error("Worker error: %s", e)
                await asyncio.sleep(1.0)

    def _get_provider(self, provider_name: str) -> Optional[LLMProvider]:
        """Get provider by name."""
        if provider_name == "cloud":
            return self.cloud_provider
        elif provider_name == "local":
            return self.local_provider
        return None


# Import json at module level for use in queue
import json
