"""Tests for MQTTFindingsPublisher module.

Tests cover:
- Module imports without errors
- Batching logic (time + count triggers)
- SQLite persistence with mock failures
- Findings converter extraction
- End-to-end publishing flow
"""

import asyncio
import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mqtt_findings_publisher import (
    Finding,
    FindingsPersistenceStore,
    MQTTFindingsPublisher,
    findings_from_deep_think_result,
    load_config_from_env,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test: Module Imports
# ─────────────────────────────────────────────────────────────────────────────


def test_module_imports():
    """Verify all classes and functions import without errors."""
    assert Finding is not None
    assert FindingsPersistenceStore is not None
    assert MQTTFindingsPublisher is not None
    assert findings_from_deep_think_result is not None
    assert load_config_from_env is not None


def test_finding_dataclass():
    """Test Finding dataclass creation and serialization."""
    finding = Finding(
        device_id="ant_001",
        claim_ids=["claim_1", "claim_2"],
        anomalies=["anomaly_a", "anomaly_b"],
        confidence=0.85,
        severity="high",
        timestamp="2024-01-01T00:00:00Z",
        metadata={"extra": "data"},
    )

    assert finding.device_id == "ant_001"
    assert finding.confidence == 0.85
    assert finding.severity == "high"

    # Test serialization
    finding_dict = finding.to_dict()
    assert isinstance(finding_dict, dict)
    assert finding_dict["device_id"] == "ant_001"
    assert finding_dict["confidence"] == 0.85

    # Test deserialization
    finding2 = Finding.from_dict(finding_dict)
    assert finding2.device_id == finding.device_id
    assert finding2.confidence == finding.confidence


# ─────────────────────────────────────────────────────────────────────────────
# Test: Persistence Store
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db():
    """Create temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_findings.db"
        yield str(db_path)


def test_persistence_store_init(temp_db):
    """Test FindingsPersistenceStore initialization."""
    store = FindingsPersistenceStore(temp_db)
    assert store.db_path == Path(temp_db)
    assert Path(temp_db).exists()


def test_persistence_store_save_and_load_finding(temp_db):
    """Test saving and loading findings from persistence store."""
    store = FindingsPersistenceStore(temp_db)

    finding = Finding(
        device_id="ant_001",
        claim_ids=["c1"],
        anomalies=["anomaly_1"],
        confidence=0.7,
        severity="medium",
        timestamp="2024-01-01T00:00:00Z",
    )

    # Save finding
    row_id = store.save_finding(finding)
    assert row_id > 0

    # Load pending findings
    pending = store.load_pending_findings()
    assert len(pending) == 1
    loaded_id, loaded_finding = pending[0]
    assert loaded_id == row_id
    assert loaded_finding.device_id == "ant_001"
    assert loaded_finding.confidence == 0.7


def test_persistence_store_mark_published(temp_db):
    """Test marking findings as published."""
    store = FindingsPersistenceStore(temp_db)

    finding = Finding(
        device_id="ant_001",
        claim_ids=["c1"],
        anomalies=["a1"],
        confidence=0.7,
        severity="medium",
        timestamp="2024-01-01T00:00:00Z",
    )

    row_id = store.save_finding(finding)
    assert len(store.load_pending_findings()) == 1

    # Mark as published
    store.mark_finding_published(row_id)
    assert len(store.load_pending_findings()) == 0


def test_persistence_store_save_confirmation(temp_db):
    """Test saving confirmations."""
    store = FindingsPersistenceStore(temp_db)

    store.save_confirmation("ant_001", "claim_1", "confirmed")
    store.save_confirmation("ant_001", "claim_2", "rejected")

    # Verify saved by querying database directly
    with sqlite3.connect(str(store.db_path)) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM confirmations")
        count = cursor.fetchone()[0]
        assert count == 2


def test_persistence_store_update_retry_count(temp_db):
    """Test updating retry count."""
    store = FindingsPersistenceStore(temp_db)

    finding = Finding(
        device_id="ant_001",
        claim_ids=["c1"],
        anomalies=["a1"],
        confidence=0.7,
        severity="medium",
        timestamp="2024-01-01T00:00:00Z",
    )

    row_id = store.save_finding(finding)
    store.update_retry_count(row_id, 3)

    # Verify in database
    with sqlite3.connect(str(store.db_path)) as conn:
        cursor = conn.execute(
            "SELECT retry_count FROM findings_queue WHERE id = ?", (row_id,)
        )
        retry_count = cursor.fetchone()[0]
        assert retry_count == 3


# ─────────────────────────────────────────────────────────────────────────────
# Test: Findings Converter
# ─────────────────────────────────────────────────────────────────────────────


def test_findings_from_deep_think_result_empty():
    """Test converter with empty result."""
    findings = findings_from_deep_think_result({})
    assert findings == []


def test_findings_from_deep_think_result_with_validation():
    """Test converter extracts validation-based findings."""
    result = {
        "validation": {
            "overall_confidence": 0.85,
            "contradictions": [
                {"description": "Claim A contradicts prior evidence"}
            ],
            "hallucination_details": [
                {"description": "Hallucinated claim B"}
            ],
            "claims": [
                {"id": "claim_1"},
                {"id": "claim_2"},
            ],
        },
        "pass_cache": [],
    }

    findings = findings_from_deep_think_result(
        result, device_id="ant_001", anomaly_threshold=0.5
    )

    assert len(findings) == 1
    finding = findings[0]
    assert finding.device_id == "ant_001"
    assert finding.confidence == 0.85
    assert len(finding.anomalies) >= 2
    assert "Contradiction" in str(finding.anomalies)
    assert "Hallucination" in str(finding.anomalies)
    assert len(finding.claim_ids) == 2


def test_findings_from_deep_think_result_pass_cache():
    """Test converter extracts pass-cache findings."""
    result = {
        "pass_cache": [
            {
                "pass_num": 1,
                "framing": "hypothesis_matrix",
                "validation": {"measured_confidence": 0.8},
            },
            {
                "pass_num": 2,
                "framing": "adversarial",
                "validation": {"measured_confidence": 0.6},
            },
        ],
        "validation": {},
    }

    findings = findings_from_deep_think_result(
        result, device_id="ant_002", anomaly_threshold=0.5
    )

    assert len(findings) == 1
    finding = findings[0]
    assert finding.confidence == 0.8
    assert len(finding.anomalies) >= 1


def test_findings_from_deep_think_result_severity():
    """Test converter determines severity correctly."""
    # Critical: >2 anomalies, confidence >0.8
    result_critical = {
        "validation": {
            "overall_confidence": 0.85,
            "contradictions": [{"description": "c1"}, {"description": "c2"}],
            "hallucination_details": [{"description": "h1"}],
            "claims": [],
        },
        "pass_cache": [],
    }
    findings = findings_from_deep_think_result(result_critical, anomaly_threshold=0.5)
    assert findings[0].severity == "critical"

    # High: >1 anomaly, confidence >0.7
    result_high = {
        "validation": {
            "overall_confidence": 0.75,
            "contradictions": [{"description": "c1"}],
            "hallucination_details": [{"description": "h1"}],
            "claims": [],
        },
        "pass_cache": [],
    }
    findings = findings_from_deep_think_result(result_high, anomaly_threshold=0.5)
    assert findings[0].severity == "high"

    # Medium: >0 anomalies, confidence >0.6
    result_medium = {
        "validation": {
            "overall_confidence": 0.65,
            "contradictions": [{"description": "c1"}],
            "hallucination_details": [],
            "claims": [],
        },
        "pass_cache": [],
    }
    findings = findings_from_deep_think_result(result_medium, anomaly_threshold=0.5)
    assert findings[0].severity == "medium"


def test_findings_from_deep_think_result_threshold():
    """Test converter respects anomaly threshold."""
    result = {
        "validation": {
            "overall_confidence": 0.4,
            "contradictions": [],
            "hallucination_details": [],
            "claims": [],
        },
        "pass_cache": [],
    }

    # Below threshold: no findings
    findings = findings_from_deep_think_result(result, anomaly_threshold=0.5)
    assert len(findings) == 0

    # Above threshold: findings created
    result["validation"]["overall_confidence"] = 0.6
    findings = findings_from_deep_think_result(result, anomaly_threshold=0.5)
    assert len(findings) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Test: MQTT Publisher Batching
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publisher_init():
    """Test MQTTFindingsPublisher initialization."""
    publisher = MQTTFindingsPublisher(
        enabled=False,  # Disable to avoid actual MQTT connection
        batch_size=3,
        batch_timeout_ms=1000,
    )

    assert publisher.batch_size == 3
    assert publisher.batch_timeout_ms == 1000
    assert not publisher._connected
    assert not publisher._running


@pytest.mark.asyncio
async def test_publisher_batching_by_size(temp_db):
    """Test batching publishes when size threshold reached."""
    publisher = MQTTFindingsPublisher(
        enabled=True,
        batch_size=3,
        batch_timeout_ms=10000,
        db_path=temp_db,
    )

    # Mock MQTT client
    publisher._client = AsyncMock()
    publisher._client.publish = AsyncMock()
    publisher._connected = True
    publisher._running = True

    # Queue 2 findings (batch not full)
    for i in range(2):
        finding = Finding(
            device_id="ant_001",
            claim_ids=[f"c{i}"],
            anomalies=[f"a{i}"],
            confidence=0.7,
            severity="medium",
            timestamp="2024-01-01T00:00:00Z",
        )
        await publisher.publish_finding(finding)

    # Should not publish yet
    publisher._client.publish.assert_not_called()

    # Queue 3rd finding (batch full)
    finding = Finding(
        device_id="ant_001",
        claim_ids=["c3"],
        anomalies=["a3"],
        confidence=0.7,
        severity="medium",
        timestamp="2024-01-01T00:00:00Z",
    )
    await publisher.publish_finding(finding)

    # Should publish now
    publisher._client.publish.assert_called_once()
    call_args = publisher._client.publish.call_args
    topic = call_args[0][0]
    payload = call_args[1]["payload"]

    assert topic == "dama/colony/findings/ant_001"
    findings_data = json.loads(payload)
    assert len(findings_data) == 3


@pytest.mark.asyncio
async def test_publisher_batching_by_timeout(temp_db):
    """Test batching publishes after timeout."""
    publisher = MQTTFindingsPublisher(
        enabled=True,
        batch_size=10,
        batch_timeout_ms=100,
        db_path=temp_db,
    )

    # Mock MQTT client
    publisher._client = AsyncMock()
    publisher._client.publish = AsyncMock()
    publisher._connected = True
    publisher._running = True

    # Queue finding
    finding = Finding(
        device_id="ant_001",
        claim_ids=["c1"],
        anomalies=["a1"],
        confidence=0.7,
        severity="medium",
        timestamp="2024-01-01T00:00:00Z",
    )
    await publisher.publish_finding(finding)

    # Should not publish immediately
    publisher._client.publish.assert_not_called()

    # Wait for timeout
    await asyncio.sleep(0.2)

    # Should publish now
    publisher._client.publish.assert_called_once()


@pytest.mark.asyncio
async def test_publisher_separate_batches_per_device(temp_db):
    """Test separate batches maintained per device."""
    publisher = MQTTFindingsPublisher(
        enabled=True,
        batch_size=2,
        batch_timeout_ms=10000,
        db_path=temp_db,
    )

    # Mock MQTT client
    publisher._client = AsyncMock()
    publisher._client.publish = AsyncMock()
    publisher._connected = True
    publisher._running = True

    # Queue findings for different devices
    for device in ["ant_001", "ant_002"]:
        for i in range(2):
            finding = Finding(
                device_id=device,
                claim_ids=[f"c{i}"],
                anomalies=[f"a{i}"],
                confidence=0.7,
                severity="medium",
                timestamp="2024-01-01T00:00:00Z",
            )
            await publisher.publish_finding(finding)

    # Should have published 2 batches (one per device)
    assert publisher._client.publish.call_count == 2


@pytest.mark.asyncio
async def test_publisher_exponential_backoff(temp_db):
    """Test exponential backoff retry logic."""
    publisher = MQTTFindingsPublisher(
        enabled=True,
        batch_size=1,
        batch_timeout_ms=1000,
        max_retries=3,
        db_path=temp_db,
    )

    # Mock MQTT client to fail
    publisher._client = AsyncMock()
    publisher._client.publish = AsyncMock(side_effect=Exception("Connection failed"))
    publisher._connected = True
    publisher._running = True

    finding = Finding(
        device_id="ant_001",
        claim_ids=["c1"],
        anomalies=["a1"],
        confidence=0.7,
        severity="medium",
        timestamp="2024-01-01T00:00:00Z",
    )

    # Measure time to confirm backoff
    start = asyncio.get_event_loop().time()
    result = await publisher._publish_batch("ant_001", [finding], retry_count=0)
    elapsed = asyncio.get_event_loop().time() - start

    # Should retry with backoff (1s + 2s + 4s = ~7s minimum)
    assert not result  # Failed and persisted
    assert elapsed >= 6  # Allow some variance


@pytest.mark.asyncio
async def test_publisher_persistence_on_failure(temp_db):
    """Test findings persisted when MQTT fails."""
    publisher = MQTTFindingsPublisher(
        enabled=True,
        batch_size=1,
        batch_timeout_ms=1000,
        max_retries=1,
        db_path=temp_db,
    )

    # Mock MQTT client to fail
    publisher._client = AsyncMock()
    publisher._client.publish = AsyncMock(side_effect=Exception("Connection failed"))
    publisher._connected = True
    publisher._running = True

    finding = Finding(
        device_id="ant_001",
        claim_ids=["c1"],
        anomalies=["a1"],
        confidence=0.7,
        severity="medium",
        timestamp="2024-01-01T00:00:00Z",
    )

    # Try to publish (should fail and persist)
    await publisher._publish_batch("ant_001", [finding], retry_count=0)

    # Verify persisted in database
    pending = publisher.store.load_pending_findings()
    assert len(pending) == 1
    assert pending[0][1].device_id == "ant_001"


@pytest.mark.asyncio
async def test_publisher_replay_persisted_findings(temp_db):
    """Test replaying persisted findings on reconnect."""
    publisher = MQTTFindingsPublisher(
        enabled=True,
        batch_size=10,
        batch_timeout_ms=5000,
        db_path=temp_db,
    )

    # Manually persist a finding
    finding = Finding(
        device_id="ant_001",
        claim_ids=["c1"],
        anomalies=["a1"],
        confidence=0.7,
        severity="medium",
        timestamp="2024-01-01T00:00:00Z",
    )
    publisher.store.save_finding(finding)

    # Mock MQTT client
    publisher._client = AsyncMock()
    publisher._client.publish = AsyncMock(return_value=None)
    publisher._connected = True
    publisher._running = True

    # Replay persisted findings
    await publisher._replay_persisted_findings()

    # Should have published
    publisher._client.publish.assert_called_once()
    payload = publisher._client.publish.call_args[1]["payload"]
    findings_data = json.loads(payload)
    assert len(findings_data) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Test: Configuration Loading
# ─────────────────────────────────────────────────────────────────────────────


def test_load_config_from_env(monkeypatch):
    """Test loading configuration from environment."""
    monkeypatch.setenv("MQTT_HOST", "test.example.com")
    monkeypatch.setenv("MQTT_PORT", "8883")
    monkeypatch.setenv("PUBLISHER_BATCH_SIZE", "20")
    monkeypatch.setenv("PUBLISHER_ENABLE", "false")

    config = load_config_from_env()

    assert config["mqtt_host"] == "test.example.com"
    assert config["mqtt_port"] == 8883
    assert config["batch_size"] == 20
    assert config["enabled"] is False


def test_load_config_from_env_defaults(monkeypatch):
    """Test loading configuration with defaults."""
    monkeypatch.delenv("MQTT_HOST", raising=False)
    monkeypatch.delenv("PUBLISHER_BATCH_SIZE", raising=False)

    config = load_config_from_env()

    assert config["mqtt_host"] == "botnet.floppydicks.net"
    assert config["batch_size"] == 10


# ─────────────────────────────────────────────────────────────────────────────
# Test: Integration
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_end_to_end_flow(temp_db):
    """Test end-to-end flow from finding extraction to publishing."""
    # Extract findings from deep_think result
    deep_think_result = {
        "validation": {
            "overall_confidence": 0.8,
            "contradictions": [{"description": "Contradiction found"}],
            "hallucination_details": [],
            "claims": [{"id": "c1"}],
        },
        "pass_cache": [],
    }

    findings = findings_from_deep_think_result(
        deep_think_result, device_id="ant_001"
    )
    assert len(findings) == 1

    # Queue to publisher
    publisher = MQTTFindingsPublisher(
        enabled=True,
        batch_size=5,
        batch_timeout_ms=1000,
        db_path=temp_db,
    )

    # Mock MQTT
    publisher._client = AsyncMock()
    publisher._client.publish = AsyncMock()
    publisher._connected = True
    publisher._running = True

    # Publish findings
    for finding in findings:
        await publisher.publish_finding(finding)

    # Should be batched
    await asyncio.sleep(0.2)
    # Since we have 1 finding and batch_size=5, should still be pending
    assert publisher._client.publish.call_count == 0

    await publisher.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
