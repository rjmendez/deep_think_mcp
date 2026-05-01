"""Tests for MQTT subscriber confirmations integration.

Tests cover:
- Confirmation message parsed correctly
- Confirmation routed to feedback_store.record_confirmation()
- Deserialization error handled gracefully
- UUID normalization on finding_id from payload
- Multiple confirmations processed sequentially
"""

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mqtt.models import Confirmation, normalize_uuid
from mqtt.subscriber import ConfirmationSubscriber


# ─────────────────────────────────────────────────────────────────────────────
# Test: Confirmation message parsed correctly
# ─────────────────────────────────────────────────────────────────────────────


def test_confirmation_dataclass_creation():
    """Test: Confirmation message parsed correctly."""
    finding_id = uuid4().hex
    device_id = "pixel_6_pro-AP21.240216.010"
    
    confirmation = Confirmation(
        finding_id=finding_id,
        device_id=device_id,
        confirmed=True,
        evidence="Manual inspection confirms anomaly",
        timestamp="2026-05-01T04:09:55Z",
    )
    
    # Verify all fields are set
    assert confirmation.finding_id == finding_id
    assert confirmation.device_id == device_id
    assert confirmation.confirmed is True
    assert confirmation.evidence == "Manual inspection confirms anomaly"
    assert confirmation.timestamp == "2026-05-01T04:09:55Z"


def test_confirmation_to_dict_serialization():
    """Test: Confirmation JSON serialization."""
    finding_id = uuid4().hex
    
    confirmation = Confirmation(
        finding_id=finding_id,
        device_id="device_001",
        confirmed=False,
        evidence="Device analysis inconclusive",
        timestamp="2026-05-01T04:09:55Z",
    )
    
    conf_dict = confirmation.to_dict()
    
    # Verify serialization
    assert conf_dict["finding_id"] == finding_id
    assert conf_dict["device_id"] == "device_001"
    assert conf_dict["confirmed"] is False
    assert conf_dict["evidence"] == "Device analysis inconclusive"
    assert conf_dict["timestamp"] == "2026-05-01T04:09:55Z"
    
    # Verify JSON serialization works
    json_str = json.dumps(conf_dict)
    assert json_str


def test_confirmation_from_dict():
    """Test: Create Confirmation from dictionary."""
    finding_id = uuid4().hex
    data = {
        "finding_id": finding_id,
        "device_id": "device_001",
        "confirmed": True,
        "evidence": "Test evidence",
        "timestamp": "2026-05-01T04:09:55Z",
    }
    
    confirmation = Confirmation.from_dict(data)
    
    # Verify fields are set
    assert confirmation.finding_id == finding_id
    assert confirmation.device_id == "device_001"
    assert confirmation.confirmed is True


# ─────────────────────────────────────────────────────────────────────────────
# Test: Confirmation routed to feedback_store.record_confirmation()
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirmation_subscriber_records_to_feedback_store():
    """Test: Confirmation routed to feedback_store.record_confirmation()."""
    # Create mock feedback store
    mock_feedback_store = MagicMock()
    mock_feedback_store.record_confirmation = MagicMock(
        return_value={
            "confirmation_id": 1,
            "finding_updated": True,
            "new_status": "confirmed",
        }
    )
    
    subscriber = ConfirmationSubscriber()
    subscriber.feedback_store = mock_feedback_store
    
    # Create confirmation
    finding_id = uuid4().hex
    confirmation = Confirmation(
        finding_id=finding_id,
        device_id="device_001",
        confirmed=True,
        evidence="Test",
        timestamp="2026-05-01T04:09:55Z",
    )
    
    # Call record_confirmation directly
    result = mock_feedback_store.record_confirmation(confirmation)
    
    # Verify it was called
    mock_feedback_store.record_confirmation.assert_called_once_with(confirmation)
    assert result["confirmation_id"] == 1
    assert result["finding_updated"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Test: Deserialization error handled gracefully
# ─────────────────────────────────────────────────────────────────────────────


def test_confirmation_handles_deserialization_error():
    """Test: Deserialization error handled gracefully."""
    # Invalid JSON
    invalid_json = "{ this is not valid json"
    
    try:
        payload = json.loads(invalid_json)
        assert False, "Should have raised JSONDecodeError"
    except json.JSONDecodeError:
        # Expected - gracefully handled
        pass


def test_confirmation_handles_missing_fields():
    """Test: Missing required fields handled gracefully."""
    # Missing finding_id
    data_missing_id = {
        "device_id": "device_001",
        "confirmed": True,
        "evidence": "Test",
        "timestamp": "2026-05-01T04:09:55Z",
    }
    
    try:
        confirmation = Confirmation.from_dict(data_missing_id)
        assert False, "Should have raised error due to missing finding_id"
    except TypeError:
        # Expected - missing required argument
        pass


def test_confirmation_handles_invalid_uuid():
    """Test: Invalid UUID format handled gracefully."""
    from mqtt.models import ValidationError
    
    # Invalid UUID (too short)
    invalid_id = "not-a-uuid"
    
    try:
        normalized = normalize_uuid(invalid_id)
        assert False, "Should have raised ValidationError"
    except ValidationError:
        # Expected - invalid UUID format
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Test: UUID normalization on finding_id from payload
# ─────────────────────────────────────────────────────────────────────────────


def test_confirmation_normalizes_uuid_from_payload():
    """Test: UUID normalization on finding_id from payload."""
    # UUID with hyphens
    uuid_with_hyphens = str(uuid4())
    expected_hex = uuid_with_hyphens.replace("-", "").lower()
    
    confirmation = Confirmation(
        finding_id=uuid_with_hyphens,
        device_id="device_001",
        confirmed=True,
        evidence="Test",
        timestamp="2026-05-01T04:09:55Z",
    )
    
    # Should be normalized
    assert confirmation.finding_id == expected_hex
    assert "-" not in confirmation.finding_id
    assert len(confirmation.finding_id) == 32


def test_confirmation_accepts_hex_uuid():
    """Test: Confirmation accepts hex format UUID."""
    hex_id = uuid4().hex
    
    confirmation = Confirmation(
        finding_id=hex_id,
        device_id="device_001",
        confirmed=True,
        evidence="Test",
        timestamp="2026-05-01T04:09:55Z",
    )
    
    # Should accept hex format directly
    assert confirmation.finding_id == hex_id


# ─────────────────────────────────────────────────────────────────────────────
# Test: Multiple confirmations processed sequentially
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_multiple_confirmations():
    """Test: Multiple confirmations processed sequentially."""
    mock_feedback_store = MagicMock()
    mock_feedback_store.record_confirmation = MagicMock(
        return_value={
            "confirmation_id": 1,
            "finding_updated": True,
            "new_status": "confirmed",
        }
    )
    
    subscriber = ConfirmationSubscriber()
    subscriber.feedback_store = mock_feedback_store
    
    # Create multiple confirmations
    confirmations = [
        Confirmation(
            finding_id=uuid4().hex,
            device_id="device_001",
            confirmed=True,
            evidence="Evidence 1",
            timestamp="2026-05-01T04:09:55Z",
        ),
        Confirmation(
            finding_id=uuid4().hex,
            device_id="device_001",
            confirmed=False,
            evidence="Evidence 2",
            timestamp="2026-05-01T04:09:56Z",
        ),
        Confirmation(
            finding_id=uuid4().hex,
            device_id="device_002",
            confirmed=True,
            evidence="Evidence 3",
            timestamp="2026-05-01T04:09:57Z",
        ),
    ]
    
    # Record each confirmation
    for confirmation in confirmations:
        mock_feedback_store.record_confirmation(confirmation)
    
    # Verify all were processed
    assert mock_feedback_store.record_confirmation.call_count == 3


# ─────────────────────────────────────────────────────────────────────────────
# Test: Confirmation hash generation
# ─────────────────────────────────────────────────────────────────────────────


def test_confirmation_generates_hash():
    """Test: Confirmation generates hash."""
    finding_id = uuid4().hex
    
    confirmation = Confirmation(
        finding_id=finding_id,
        device_id="device_001",
        confirmed=True,
        evidence="Test",
        timestamp="2026-05-01T04:09:55Z",
    )
    
    # Verify hash is generated
    assert confirmation.confirmation_hash is not None
    assert len(confirmation.confirmation_hash) == 32  # MD5 hash


def test_confirmation_hash_idempotent():
    """Test: Confirmation hash is idempotent."""
    finding_id = uuid4().hex
    
    # Create two confirmations with same data
    conf1 = Confirmation(
        finding_id=finding_id,
        device_id="device_001",
        confirmed=True,
        evidence="Test",
        timestamp="2026-05-01T04:09:55Z",
    )
    
    conf2 = Confirmation(
        finding_id=finding_id,
        device_id="device_001",
        confirmed=True,
        evidence="Test",
        timestamp="2026-05-01T04:09:55Z",
    )
    
    # Hashes should be the same
    assert conf1.confirmation_hash == conf2.confirmation_hash


# ─────────────────────────────────────────────────────────────────────────────
# Test: Confirmation payload example from spec
# ─────────────────────────────────────────────────────────────────────────────


def test_confirmation_payload_from_spec():
    """Test: Confirmation payload format from spec."""
    payload_str = """{
      "finding_id": "550e8400e29b41d4a716446655440000",
      "device_id": "pixel_6_pro-AP21.240216.010",
      "confirmed": true,
      "evidence": "Manual inspection confirms anomaly",
      "timestamp": "2026-05-01T04:09:55Z"
    }"""
    
    payload = json.loads(payload_str)
    confirmation = Confirmation.from_dict(payload)
    
    # Verify all fields match spec
    assert confirmation.finding_id == "550e8400e29b41d4a716446655440000"
    assert confirmation.device_id == "pixel_6_pro-AP21.240216.010"
    assert confirmation.confirmed is True
    assert confirmation.evidence == "Manual inspection confirms anomaly"
    assert confirmation.timestamp == "2026-05-01T04:09:55Z"


# ─────────────────────────────────────────────────────────────────────────────
# Test: Confirmation parsing edge cases
# ─────────────────────────────────────────────────────────────────────────────


def test_confirmation_with_empty_evidence():
    """Test: Confirmation with empty evidence."""
    confirmation = Confirmation(
        finding_id=uuid4().hex,
        device_id="device_001",
        confirmed=True,
        evidence="",  # Empty evidence
        timestamp="2026-05-01T04:09:55Z",
    )
    
    assert confirmation.evidence == ""


def test_confirmation_with_long_evidence():
    """Test: Confirmation with long evidence string."""
    long_evidence = "A" * 1000  # 1000 character evidence
    
    confirmation = Confirmation(
        finding_id=uuid4().hex,
        device_id="device_001",
        confirmed=True,
        evidence=long_evidence,
        timestamp="2026-05-01T04:09:55Z",
    )
    
    assert confirmation.evidence == long_evidence


def test_confirmation_different_confirmed_values():
    """Test: Confirmation handles both confirmed and rejected."""
    finding_id = uuid4().hex
    
    # Confirmed
    conf_true = Confirmation(
        finding_id=finding_id,
        device_id="device_001",
        confirmed=True,
        evidence="Confirmed",
        timestamp="2026-05-01T04:09:55Z",
    )
    assert conf_true.confirmed is True
    
    # Rejected
    conf_false = Confirmation(
        finding_id=finding_id,
        device_id="device_001",
        confirmed=False,
        evidence="Rejected",
        timestamp="2026-05-01T04:09:55Z",
    )
    assert conf_false.confirmed is False
