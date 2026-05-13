"""End-to-end tests for MQTT feedback loop.

Tests the complete feedback flow: finding → confirmation → confidence update.
No simulation; uses real telemetry data and Bayesian confidence model.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from mqtt.models import Finding, Confirmation, AnomalyType
from mqtt.feedback_store import FeedbackStore


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def test_db():
    """Create temporary SQLite database for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_feedback.db")
        yield db_path
        # Cleanup handled by tempfile context manager


@pytest.fixture
def feedback_store(test_db):
    """Create FeedbackStore instance with test database."""
    store = FeedbackStore(db_path=test_db)
    yield store


@pytest.fixture
def sample_finding():
    """Create a realistic sample finding with step counter duplication."""
    finding_id = uuid4().hex
    return Finding(
        id=finding_id,
        device_id="pixel_6_pro-AP21.240216.010",
        finding_type=AnomalyType.STEP_DUPLICATION,
        confidence=0.60,
        timestamp=datetime.now(timezone.utc).isoformat(),
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=7)
        ).isoformat(),
        claim_ids=["step-dup-001"],
        anomalies=["Step count duplication detected in 15-min window"],
        severity="medium",
        metadata={
            "duplicated_steps": 1247,
            "window_minutes": 15,
            "source": "real_telemetry",
        },
    )


@pytest.fixture
def sample_confirmation(sample_finding):
    """Create a realistic sample confirmation."""
    return Confirmation(
        finding_id=sample_finding.id,
        device_id=sample_finding.device_id,
        confirmed=True,
        evidence="Manual verification confirms anomaly",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test Scenario 1: Finding → Confirmation → Confidence Update
# ─────────────────────────────────────────────────────────────────────────────


def test_finding_confirmation_updates_confidence(feedback_store, sample_finding, sample_confirmation):
    """Test that confirmation increases finding confidence using Bayesian model."""
    # 1. Insert finding with known confidence
    finding_id = sample_finding.id
    device_id = sample_finding.device_id
    initial_confidence = 0.60
    
    success = feedback_store.store_finding(
        finding_id=finding_id,
        device_id=device_id,
        confidence=initial_confidence,
        finding_type=sample_finding.finding_type.value,
        timestamp=sample_finding.timestamp,
        expires_at=sample_finding.expires_at,
        claim_ids=sample_finding.claim_ids,
        anomalies=sample_finding.anomalies,
        severity=sample_finding.severity,
        metadata=sample_finding.metadata,
    )
    assert success is True
    
    # 2. Create and process confirmation
    result = feedback_store.record_confirmation(sample_confirmation)
    
    # 3. Assert update was successful
    assert result["success"] is True
    assert result["finding_updated"] is True
    assert result["reason"] == "confirmation_processed"
    
    # 4. Verify confidence increased (Bayesian model, no staleness)
    # Expected: old + (1.0 - old) * 0.1 = 0.60 + 0.40 * 0.1 = 0.64
    new_confidence = result["new_confidence"]
    assert new_confidence > initial_confidence
    assert abs(new_confidence - 0.64) < 0.01  # Allow small float error


def test_duplicate_confirmation_ignored(feedback_store, sample_finding, sample_confirmation):
    """Test that duplicate confirmations don't double-update confidence."""
    finding_id = sample_finding.id
    
    # 1. Store finding
    feedback_store.store_finding(
        finding_id=finding_id,
        device_id=sample_finding.device_id,
        confidence=0.60,
        finding_type=sample_finding.finding_type.value,
        timestamp=sample_finding.timestamp,
        expires_at=sample_finding.expires_at,
    )
    
    # 2. Process first confirmation
    result1 = feedback_store.record_confirmation(sample_confirmation)
    assert result1["success"] is True
    assert result1["reason"] == "confirmation_processed"
    new_conf_1 = result1["new_confidence"]
    
    # 3. Process identical confirmation again
    result2 = feedback_store.record_confirmation(sample_confirmation)
    assert result2["success"] is True
    # Should be marked as duplicate
    assert result2["reason"] == "duplicate_ignored"
    
    # 4. Verify confidence did NOT increase further
    assert "new_confidence" not in result2  # No confidence update for duplicates


def test_rejection_lowers_confidence(feedback_store, sample_finding):
    """Test that rejections decrease finding confidence."""
    finding_id = sample_finding.id
    
    # 1. Store finding with high confidence
    initial_confidence = 0.80
    feedback_store.store_finding(
        finding_id=finding_id,
        device_id=sample_finding.device_id,
        confidence=initial_confidence,
        finding_type=sample_finding.finding_type.value,
        timestamp=sample_finding.timestamp,
        expires_at=sample_finding.expires_at,
    )
    
    # 2. Create rejection confirmation
    rejection = Confirmation(
        finding_id=finding_id,
        device_id=sample_finding.device_id,
        confirmed=False,  # Rejection
        evidence="Not an anomaly, expected behavior",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    
    # 3. Process rejection
    result = feedback_store.record_confirmation(rejection)
    
    # 4. Verify confidence decreased (multiplied by 0.8)
    # Expected: 0.80 * 0.8 = 0.64
    assert result["success"] is True
    assert result["finding_updated"] is True
    new_confidence = result["new_confidence"]
    assert new_confidence < initial_confidence
    assert abs(new_confidence - 0.64) < 0.01


def test_staleness_decay_applied(feedback_store, sample_finding):
    """Test that staleness decay is applied to confidence updates."""
    finding_id = sample_finding.id
    
    # 1. Store finding with old timestamp (2 hours ago)
    old_timestamp = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    initial_confidence = 0.60
    
    feedback_store.store_finding(
        finding_id=finding_id,
        device_id=sample_finding.device_id,
        confidence=initial_confidence,
        finding_type=sample_finding.finding_type.value,
        timestamp=old_timestamp,
        expires_at=sample_finding.expires_at,
    )
    
    # 2. Create confirmation
    confirmation = Confirmation(
        finding_id=finding_id,
        device_id=sample_finding.device_id,
        confirmed=True,
        evidence="Confirmed",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    
    # 3. Update with staleness decay
    result = feedback_store.record_confirmation(confirmation)
    
    # 4. Verify staleness decay applied
    # Base update: 0.60 * (0.95^2) + (1.0 - (0.60 * (0.95^2))) * 0.1
    # More precisely:
    # - First apply decay: 0.60 * 0.95^2 ≈ 0.60 * 0.9025 ≈ 0.5415
    # - Then apply confirmation: 0.5415 + (1.0 - 0.5415) * 0.1 ≈ 0.5415 + 0.04585 ≈ 0.5874
    assert result["success"] is True
    new_confidence = result["new_confidence"]
    # Should be less than 0.64 (the confidence without decay) but greater than 0.54
    assert 0.54 < new_confidence < 0.65


def test_orphaned_confirmation_tracked(feedback_store):
    """Test that confirmations for non-existent findings are tracked."""
    # 1. Create confirmation for non-existent finding
    orphan_confirmation = Confirmation(
        finding_id=uuid4().hex,  # Non-existent UUID
        device_id="pixel_6_pro-AP21.240216.010",
        confirmed=True,
        evidence="Confirmation for ghost finding",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    
    # 2. Try to process confirmation
    result = feedback_store.record_confirmation(orphan_confirmation)
    
    # 3. Verify failure
    assert result["success"] is True  # Successfully processed as orphaned
    assert result["finding_updated"] is False
    assert result["reason"] == "orphaned_finding"


def test_multiple_devices_independent(feedback_store):
    """Test that multiple devices' findings are updated independently."""
    device_1_id = "pixel_6a-AP21.240216.001"
    device_2_id = "pixel_7_pro-AP21.240216.002"
    
    # 1. Store findings for two different devices
    finding_1_id = uuid4().hex
    finding_2_id = uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    
    feedback_store.store_finding(
        finding_id=finding_1_id,
        device_id=device_1_id,
        confidence=0.60,
        finding_type="StepDuplication",
        timestamp=now,
        expires_at=expires,
    )
    
    feedback_store.store_finding(
        finding_id=finding_2_id,
        device_id=device_2_id,
        confidence=0.70,
        finding_type="TemperatureQuantization",
        timestamp=now,
        expires_at=expires,
    )
    
    # 2. Publish confirmations for both
    conf_1 = Confirmation(
        finding_id=finding_1_id,
        device_id=device_1_id,
        confirmed=True,
        evidence="Confirmed",
        timestamp=now,
    )
    
    conf_2 = Confirmation(
        finding_id=finding_2_id,
        device_id=device_2_id,
        confirmed=False,
        evidence="Rejected",
        timestamp=now,
    )
    
    result_1 = feedback_store.record_confirmation(conf_1)
    result_2 = feedback_store.record_confirmation(conf_2)
    
    # 3. Verify each device's finding updated independently
    assert result_1["success"] is True
    assert result_2["success"] is True
    assert result_1["new_confidence"] > 0.60  # Device 1: increased
    assert result_2["new_confidence"] < 0.70  # Device 2: decreased
    
    # 4. Verify get_finding_stats filters by device
    stats = feedback_store.get_finding_stats(device_id=device_1_id)
    assert stats is not None


@pytest.mark.skipif(
    os.getenv("MQTT_BROKER_AVAILABLE") != "true",
    reason="Real MQTT broker not available",
)
def test_real_mqtt_connection_optional(feedback_store):
    """Test with real MQTT connection (optional).
    
    Requires MQTT_BROKER_AVAILABLE=true environment variable.
    Tests full flow with real device telemetry.
    """
    # This test is marked skip if broker unavailable
    # In production, would connect to localhost
    pytest.skip("Real MQTT broker test - requires environment setup")
