"""Tests for MQTT publisher findings integration.

Tests cover:
- Finding published with correct schema
- Finding ID normalized to hex format
- TTL set to 7 days
- UUID normalization in publish path
- Confidence validation (outside 0-1 raises error)
- JSON serialization handles all field types
"""

import asyncio
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mqtt.models import Finding, AnomalyType
from mqtt.publisher import MQTTFindingsPublisher


# ─────────────────────────────────────────────────────────────────────────────
# Test: Finding with correct schema
# ─────────────────────────────────────────────────────────────────────────────


def test_finding_created_with_all_fields():
    """Test: Finding published with correct schema."""
    finding = Finding(
        id=uuid4().hex,
        device_id="pixel_6_pro-AP21.240216.010",
        finding_type=AnomalyType.STEP_DUPLICATION,
        confidence=0.60,
        timestamp="2026-05-01T04:09:54Z",
        expires_at="2026-05-08T04:09:54Z",
        claim_ids=["claim_1", "claim_2"],
        anomalies=["anomaly_a"],
        severity="medium",
        metadata={"test": "data"},
    )
    
    # Verify all fields are set
    assert finding.id
    assert finding.device_id == "pixel_6_pro-AP21.240216.010"
    assert finding.finding_type == AnomalyType.STEP_DUPLICATION
    assert finding.confidence == 0.60
    assert finding.timestamp == "2026-05-01T04:09:54Z"
    assert finding.expires_at == "2026-05-08T04:09:54Z"


def test_finding_to_dict_serialization():
    """Test: JSON serialization handles all field types."""
    finding = Finding(
        id=uuid4().hex,
        device_id="device_001",
        finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
        confidence=0.85,
        timestamp="2026-05-01T04:09:54Z",
        expires_at="2026-05-08T04:09:54Z",
        claim_ids=["c1", "c2"],
        anomalies=["a1", "a2"],
        severity="high",
        metadata={"extra": "info"},
    )
    
    finding_dict = finding.to_dict()
    
    # Verify serialization
    assert finding_dict["id"] == finding.id
    assert finding_dict["device_id"] == "device_001"
    assert finding_dict["finding_type"] == "TemperatureQuantization"  # Enum as string
    assert finding_dict["confidence"] == 0.85
    assert finding_dict["timestamp"] == "2026-05-01T04:09:54Z"
    assert finding_dict["expires_at"] == "2026-05-08T04:09:54Z"
    assert isinstance(finding_dict["claim_ids"], list)
    assert isinstance(finding_dict["anomalies"], list)
    
    # Verify JSON serialization works
    json_str = json.dumps(finding_dict)
    assert json_str
    
    # Verify can deserialize
    deserialized = json.loads(json_str)
    assert deserialized["id"] == finding.id


# ─────────────────────────────────────────────────────────────────────────────
# Test: Finding ID normalized to hex format
# ─────────────────────────────────────────────────────────────────────────────


def test_finding_id_normalized_to_hex():
    """Test: Finding ID normalized to hex format."""
    # Generate a UUID and normalize it
    uuid_str = str(uuid4())
    hex_id = uuid_str.replace("-", "").lower()
    
    finding = Finding(
        id=hex_id,
        device_id="device_001",
        finding_type=AnomalyType.STEP_DUPLICATION,
        confidence=0.5,
        timestamp="2026-05-01T04:09:54Z",
        expires_at="2026-05-08T04:09:54Z",
    )
    
    # Verify hex format
    assert len(finding.id) == 32  # UUID hex is 32 characters
    assert all(c in "0123456789abcdef" for c in finding.id)


def test_finding_id_accepts_hyphenated_uuid():
    """Test: Finding normalizes hyphenated UUID."""
    # Standard UUID format with hyphens
    uuid_str = str(uuid4())  # e.g., "550e8400-e29b-41d4-a716-446655440000"
    
    finding = Finding(
        id=uuid_str,
        device_id="device_001",
        finding_type=AnomalyType.STEP_DUPLICATION,
        confidence=0.5,
        timestamp="2026-05-01T04:09:54Z",
        expires_at="2026-05-08T04:09:54Z",
    )
    
    # Should be normalized (no hyphens)
    assert "-" not in finding.id
    assert len(finding.id) == 32


# ─────────────────────────────────────────────────────────────────────────────
# Test: TTL set to 7 days
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_findings_sets_7_day_ttl():
    """Test: TTL set to 7 days."""
    publisher = MQTTFindingsPublisher(enabled=True)
    
    finding = Finding(
        id=uuid4().hex,  # Use a valid UUID
        device_id="device_001",
        finding_type=AnomalyType.STEP_DUPLICATION,
        confidence=0.5,
        timestamp="2026-05-01T04:09:54Z",
        expires_at="",  # Empty, should be set
    )
    
    # Mock the publish_finding method
    publisher.publish_finding = AsyncMock(return_value=True)
    
    # Store original expires_at (should be empty initially)
    original_expires = finding.expires_at
    
    # Publish findings
    result = await publisher.publish_findings([finding])
    
    # Verify finding has TTL set
    assert finding.expires_at is not None
    assert finding.expires_at != original_expires  # Should be changed
    assert "Z" in finding.expires_at
    
    # Parse and verify it's approximately 7 days
    expires_dt = datetime.fromisoformat(finding.expires_at.replace("Z", "+00:00"))
    timestamp_dt = datetime.fromisoformat(finding.timestamp.replace("Z", "+00:00"))
    delta = expires_dt - timestamp_dt
    
    # Should be close to 7 days (within 1 minute)
    assert 6.999 <= delta.days <= 7.001


# ─────────────────────────────────────────────────────────────────────────────
# Test: UUID normalization in publish path
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_findings_normalizes_uuid():
    """Test: UUID normalization in publish path."""
    publisher = MQTTFindingsPublisher(enabled=True)
    
    # Use a UUID with hyphens that will be normalized
    uuid_with_hyphens = str(uuid4())
    expected_hex = uuid_with_hyphens.replace("-", "").lower()
    
    finding = Finding(
        id=uuid_with_hyphens,  # Use hyphenated UUID
        device_id="device_001",
        finding_type=AnomalyType.STEP_DUPLICATION,
        confidence=0.5,
        timestamp="2026-05-01T04:09:54Z",
        expires_at="2026-05-08T04:09:54Z",
    )
    
    # After initialization, ID should be normalized
    assert finding.id == expected_hex
    
    # Mock the publish_finding method
    publisher.publish_finding = AsyncMock(return_value=True)
    
    # Publish findings
    result = await publisher.publish_findings([finding])
    
    # Verify ID is still normalized
    assert finding.id == expected_hex
    assert len(finding.id) == 32  # Hex format
    assert all(c in "0123456789abcdef" for c in finding.id)


@pytest.mark.asyncio
async def test_publish_findings_uses_existing_uuid():
    """Test: Existing UUID preserved if already set."""
    publisher = MQTTFindingsPublisher(enabled=True)
    
    original_id = uuid4().hex
    finding = Finding(
        id=original_id,
        device_id="device_001",
        finding_type=AnomalyType.STEP_DUPLICATION,
        confidence=0.5,
        timestamp="2026-05-01T04:09:54Z",
        expires_at="2026-05-08T04:09:54Z",
    )
    
    # Mock the publish_finding method
    publisher.publish_finding = AsyncMock(return_value=True)
    
    # Publish findings
    result = await publisher.publish_findings([finding])
    
    # Verify ID is unchanged
    assert finding.id == original_id


# ─────────────────────────────────────────────────────────────────────────────
# Test: Confidence validation (outside 0-1 raises error)
# ─────────────────────────────────────────────────────────────────────────────


def test_finding_rejects_confidence_below_zero():
    """Test: Confidence validation (outside 0-1 raises error)."""
    from mqtt.models import ValidationError
    
    with pytest.raises(ValidationError):
        Finding(
            id=uuid4().hex,
            device_id="device_001",
            finding_type=AnomalyType.STEP_DUPLICATION,
            confidence=-0.1,  # Invalid!
            timestamp="2026-05-01T04:09:54Z",
            expires_at="2026-05-08T04:09:54Z",
        )


def test_finding_rejects_confidence_above_one():
    """Test: Confidence validation (outside 0-1 raises error)."""
    from mqtt.models import ValidationError
    
    with pytest.raises(ValidationError):
        Finding(
            id=uuid4().hex,
            device_id="device_001",
            finding_type=AnomalyType.STEP_DUPLICATION,
            confidence=1.1,  # Invalid!
            timestamp="2026-05-01T04:09:54Z",
            expires_at="2026-05-08T04:09:54Z",
        )


@pytest.mark.asyncio
async def test_publish_findings_skips_invalid_confidence():
    """Test: Invalid confidence findings are skipped."""
    publisher = MQTTFindingsPublisher(enabled=True)
    
    # Create a valid finding
    finding = Finding(
        id=uuid4().hex,
        device_id="device_001",
        finding_type=AnomalyType.STEP_DUPLICATION,
        confidence=0.5,
        timestamp="2026-05-01T04:09:54Z",
        expires_at="2026-05-08T04:09:54Z",
    )
    
    # Mock the publish_finding method
    publisher.publish_finding = AsyncMock(return_value=True)
    
    # Manually set invalid confidence (bypass validation)
    finding.confidence = 1.5  # Invalid but set after initialization
    
    # Publish findings
    result = await publisher.publish_findings([finding])
    
    # Should have skipped this finding
    assert len(result) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test: JSON serialization handles all field types
# ─────────────────────────────────────────────────────────────────────────────


def test_json_serialization_enum_to_string():
    """Test: JSON serialization handles enum fields."""
    finding = Finding(
        id=uuid4().hex,
        device_id="device_001",
        finding_type=AnomalyType.ZERO_ERROR_RATES,
        confidence=0.75,
        timestamp="2026-05-01T04:09:54Z",
        expires_at="2026-05-08T04:09:54Z",
    )
    
    data_dict = finding.to_dict()
    
    # Verify enum is converted to string
    assert data_dict["finding_type"] == "ZeroErrorRates"
    assert isinstance(data_dict["finding_type"], str)


def test_json_serialization_datetime_to_iso():
    """Test: JSON serialization handles datetime fields (ISO format)."""
    finding = Finding(
        id=uuid4().hex,
        device_id="device_001",
        finding_type=AnomalyType.STEP_DUPLICATION,
        confidence=0.5,
        timestamp="2026-05-01T04:09:54Z",
        expires_at="2026-05-08T04:09:54Z",
    )
    
    data_dict = finding.to_dict()
    json_str = json.dumps(data_dict)
    
    # Verify timestamps are ISO format strings
    assert "2026-05-01T04:09:54Z" in json_str
    assert "2026-05-08T04:09:54Z" in json_str


def test_json_serialization_float_confidence():
    """Test: JSON serialization handles float confidence."""
    finding = Finding(
        id=uuid4().hex,
        device_id="device_001",
        finding_type=AnomalyType.STEP_DUPLICATION,
        confidence=0.6123456789,
        timestamp="2026-05-01T04:09:54Z",
        expires_at="2026-05-08T04:09:54Z",
    )
    
    data_dict = finding.to_dict()
    json_str = json.dumps(data_dict)
    
    # Verify float is serialized correctly
    assert isinstance(data_dict["confidence"], float)
    assert "0.612345" in json_str or "0.61234" in json_str


# ─────────────────────────────────────────────────────────────────────────────
# Test: publish_findings() method returns list of IDs
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_findings_returns_list_of_ids():
    """Test: publish_findings() method returns list of finding IDs published."""
    publisher = MQTTFindingsPublisher(enabled=True)
    
    findings = [
        Finding(
            id=uuid4().hex,
            device_id="device_001",
            finding_type=AnomalyType.STEP_DUPLICATION,
            confidence=0.5,
            timestamp="2026-05-01T04:09:54Z",
            expires_at="2026-05-08T04:09:54Z",
        ),
        Finding(
            id=uuid4().hex,
            device_id="device_001",
            finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
            confidence=0.75,
            timestamp="2026-05-01T04:09:54Z",
            expires_at="2026-05-08T04:09:54Z",
        ),
    ]
    
    # Mock the publish_finding method
    publisher.publish_finding = AsyncMock(return_value=True)
    
    # Publish findings
    result = await publisher.publish_findings(findings)
    
    # Verify result is list of IDs
    assert isinstance(result, list)
    assert len(result) == 2
    assert all(isinstance(id, str) for id in result)
    assert all(len(id) == 32 for id in result)  # Hex format


@pytest.mark.asyncio
async def test_publish_findings_disabled_returns_empty_list():
    """Test: publish_findings() returns empty list when disabled."""
    publisher = MQTTFindingsPublisher(enabled=False)
    
    findings = [
        Finding(
            id=uuid4().hex,
            device_id="device_001",
            finding_type=AnomalyType.STEP_DUPLICATION,
            confidence=0.5,
            timestamp="2026-05-01T04:09:54Z",
            expires_at="2026-05-08T04:09:54Z",
        ),
    ]
    
    # Publish findings
    result = await publisher.publish_findings(findings)
    
    # Should return empty list
    assert result == []
