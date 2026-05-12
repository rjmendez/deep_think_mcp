from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from deep_think_mcp import store
from deep_think_mcp.api import reasoning as reasoning_api


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


@pytest.fixture
def local_db(monkeypatch):
    base_dir = Path(__file__).resolve().parent.parent / ".test-artifacts"
    base_dir.mkdir(exist_ok=True)
    db_path = base_dir / f"status-propagation-{uuid.uuid4().hex}.db"
    monkeypatch.setenv("DEEP_THINK_DB", str(db_path))
    store.init_db()
    try:
        yield db_path
    finally:
        if db_path.exists():
            db_path.unlink()


def test_complete_job_uses_failed_status_from_result_payload(local_db):
    job_id = store.create_job("question", passes=1, provider="ollama", model_summary="test")
    claimed = store.claim_next_job()
    assert claimed is not None
    assert claimed["job_id"] == job_id

    store.complete_job(job_id, json.dumps({"status": "failed", "error": "synthesis failed"}))

    job = store.get_job(job_id)
    assert job is not None
    assert job["status"] == "failed"
    assert job["error"] == "synthesis failed"


@pytest.mark.asyncio
async def test_get_thinking_result_reports_effective_status_and_empty_lists(monkeypatch):
    fake_mcp = FakeMCP()
    reasoning_api.register(fake_mcp)
    get_thinking_result = fake_mcp.tools["get_thinking_result"]

    now = datetime.now(timezone.utc).isoformat()
    result_payload = {
        "type": "fan_out",
        "status": "failed",
        "final_answer": "",
        "converged_claims": [],
        "contested_areas": [],
        "claim_sets": [],
        "escalated_claim_ids": [],
        "error": "synthesis failed",
    }

    monkeypatch.setattr(
        reasoning_api.store,
        "get_job",
        lambda _job_id: {
            "job_id": "job-123",
            "status": "complete",
            "provider": "anthropic",
            "model_summary": "summary",
            "created_at": now,
            "completed_at": now,
            "result": json.dumps(result_payload),
            "error": None,
        },
    )
    monkeypatch.setattr(reasoning_api.store, "get_full_reasoning_chain", lambda _job_id: [])

    response = await get_thinking_result("job-123")

    assert response["status"] == "failed"
    assert response["job_status"] == "complete"
    assert response["result_status"] == "failed"
    assert response["converged_claims"] == []
    assert response["contested_areas"] == []
    assert response["claim_sets"] == []
    assert response["escalated_claim_ids"] == []
