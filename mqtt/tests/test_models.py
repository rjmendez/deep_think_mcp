"""Tests for MQTT models: Finding, Confirmation, and UUID normalization."""

import pytest
from datetime import datetime, timezone
from mqtt.models import (
    Finding,
    Confirmation,
    AnomalyType,
    ValidationError,
    normalize_uuid,
)


class TestUUIDNormalization:
    """Tests for normalize_uuid() utility function."""

    def test_normalize_uuid_with_hyphens(self) -> None:
        """Test normalization of standard hyphenated UUID format."""
        uuid_with_hyphens = "550e8400-e29b-41d4-a716-446655440000"
        expected = "550e8400e29b41d4a716446655440000"
        assert normalize_uuid(uuid_with_hyphens) == expected

    def test_normalize_uuid_without_hyphens(self) -> None:
        """Test that already-normalized UUIDs are returned unchanged."""
        uuid_normalized = "550e8400e29b41d4a716446655440000"
        assert normalize_uuid(uuid_normalized) == uuid_normalized

    def test_normalize_uuid_uppercase_to_lowercase(self) -> None:
        """Test that uppercase hex is converted to lowercase."""
        uuid_upper = "550E8400E29B41D4A716446655440000"
        expected = "550e8400e29b41d4a716446655440000"
        assert normalize_uuid(uuid_upper) == expected

    def test_normalize_uuid_mixed_case_with_hyphens(self) -> None:
        """Test mixed case with hyphens."""
        uuid_mixed = "550E8400-e29b-41d4-A716-446655440000"
        expected = "550e8400e29b41d4a716446655440000"
        assert normalize_uuid(uuid_mixed) == expected

    def test_normalize_uuid_invalid_length(self) -> None:
        """Test that UUID with wrong length raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            normalize_uuid("550e8400-e29b-41d4-a716")
        assert "expected 32 hex characters" in str(exc_info.value)

    def test_normalize_uuid_invalid_hex_characters(self) -> None:
        """Test that non-hex characters raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            normalize_uuid("550e8400e29b41d4a716446655440zzz")
        assert "non-hexadecimal characters" in str(exc_info.value)

    def test_normalize_uuid_empty_string(self) -> None:
        """Test that empty string raises ValidationError."""
        with pytest.raises(ValidationError):
            normalize_uuid("")

    def test_normalize_uuid_with_only_hyphens(self) -> None:
        """Test that string with only hyphens raises ValidationError."""
        with pytest.raises(ValidationError):
            normalize_uuid("----")


class TestAnomalyTypeEnum:
    """Tests for AnomalyType enumeration."""

    def test_anomaly_type_step_duplication(self) -> None:
        """Test StepDuplication enum value."""
        assert AnomalyType.STEP_DUPLICATION.value == "StepDuplication"

    def test_anomaly_type_temperature_quantization(self) -> None:
        """Test TemperatureQuantization enum value."""
        assert AnomalyType.TEMPERATURE_QUANTIZATION.value == "TemperatureQuantization"

    def test_anomaly_type_zero_error_rates(self) -> None:
        """Test ZeroErrorRates enum value."""
        assert AnomalyType.ZERO_ERROR_RATES.value == "ZeroErrorRates"

    def test_anomaly_type_step_cadence_contradiction(self) -> None:
        """Test StepCadenceContradiction enum value."""
        assert AnomalyType.STEP_CADENCE_CONTRADICTION.value == "StepCadenceContradiction"

    def test_anomaly_type_memory_saturation(self) -> None:
        """Test MemorySaturation enum value."""
        assert AnomalyType.MEMORY_SATURATION.value == "MemorySaturation"


class TestFindingModel:
    """Tests for Finding dataclass."""

    def test_finding_creation_with_all_fields(self) -> None:
        """Test creating a Finding with all required fields."""
        now = datetime.now(timezone.utc).isoformat()
        expires = datetime.now(timezone.utc).isoformat()
        
        finding = Finding(
            id="550e8400e29b41d4a716446655440000",
            device_id="pixel_001",
            finding_type=AnomalyType.STEP_DUPLICATION,
            confidence=0.95,
            timestamp=now,
            expires_at=expires,
        )
        
        assert finding.id == "550e8400e29b41d4a716446655440000"
        assert finding.device_id == "pixel_001"
        assert finding.finding_type == AnomalyType.STEP_DUPLICATION
        assert finding.confidence == 0.95
        assert finding.timestamp == now
        assert finding.expires_at == expires

    def test_finding_normalizes_hyphenated_uuid(self) -> None:
        """Test that Finding normalizes hyphenated UUIDs."""
        now = datetime.now(timezone.utc).isoformat()
        expires = datetime.now(timezone.utc).isoformat()
        
        finding = Finding(
            id="550e8400-e29b-41d4-a716-446655440000",
            device_id="pixel_001",
            finding_type=AnomalyType.ZERO_ERROR_RATES,
            confidence=0.75,
            timestamp=now,
            expires_at=expires,
        )
        
        assert finding.id == "550e8400e29b41d4a716446655440000"

    def test_finding_confidence_validation_too_high(self) -> None:
        """Test that confidence > 1.0 raises ValidationError."""
        now = datetime.now(timezone.utc).isoformat()
        
        with pytest.raises(ValidationError) as exc_info:
            Finding(
                id="550e8400e29b41d4a716446655440000",
                device_id="pixel_001",
                finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
                confidence=1.5,
                timestamp=now,
                expires_at=now,
            )
        assert "between 0.0 and 1.0" in str(exc_info.value)

    def test_finding_confidence_validation_too_low(self) -> None:
        """Test that confidence < 0.0 raises ValidationError."""
        now = datetime.now(timezone.utc).isoformat()
        
        with pytest.raises(ValidationError) as exc_info:
            Finding(
                id="550e8400e29b41d4a716446655440000",
                device_id="pixel_001",
                finding_type=AnomalyType.MEMORY_SATURATION,
                confidence=-0.1,
                timestamp=now,
                expires_at=now,
            )
        assert "between 0.0 and 1.0" in str(exc_info.value)

    def test_finding_confidence_boundary_values(self) -> None:
        """Test that confidence boundaries (0.0 and 1.0) are valid."""
        now = datetime.now(timezone.utc).isoformat()
        
        # Test confidence = 0.0
        finding_low = Finding(
            id="550e8400e29b41d4a716446655440000",
            device_id="pixel_001",
            finding_type=AnomalyType.STEP_CADENCE_CONTRADICTION,
            confidence=0.0,
            timestamp=now,
            expires_at=now,
        )
        assert finding_low.confidence == 0.0
        
        # Test confidence = 1.0
        finding_high = Finding(
            id="550e8400e29b41d4a716446655440001",
            device_id="pixel_002",
            finding_type=AnomalyType.STEP_CADENCE_CONTRADICTION,
            confidence=1.0,
            timestamp=now,
            expires_at=now,
        )
        assert finding_high.confidence == 1.0

    def test_finding_invalid_uuid(self) -> None:
        """Test that invalid UUID raises ValidationError."""
        now = datetime.now(timezone.utc).isoformat()
        
        with pytest.raises(ValidationError) as exc_info:
            Finding(
                id="not-a-valid-uuid",
                device_id="pixel_001",
                finding_type=AnomalyType.STEP_DUPLICATION,
                confidence=0.5,
                timestamp=now,
                expires_at=now,
            )
        assert "Invalid finding ID" in str(exc_info.value)

    def test_finding_invalid_finding_type(self) -> None:
        """Test that invalid finding_type raises ValidationError."""
        now = datetime.now(timezone.utc).isoformat()
        
        with pytest.raises(ValidationError) as exc_info:
            Finding(
                id="550e8400e29b41d4a716446655440000",
                device_id="pixel_001",
                finding_type="InvalidType",  # type: ignore
                confidence=0.5,
                timestamp=now,
                expires_at=now,
            )
        assert "must be an AnomalyType" in str(exc_info.value)

    def test_finding_to_dict(self) -> None:
        """Test Finding.to_dict() serialization."""
        now = datetime.now(timezone.utc).isoformat()
        expires = datetime.now(timezone.utc).isoformat()
        
        finding = Finding(
            id="550e8400e29b41d4a716446655440000",
            device_id="pixel_001",
            finding_type=AnomalyType.STEP_DUPLICATION,
            confidence=0.95,
            timestamp=now,
            expires_at=expires,
        )
        
        data = finding.to_dict()
        assert data["id"] == "550e8400e29b41d4a716446655440000"
        assert data["device_id"] == "pixel_001"
        assert data["finding_type"] == "StepDuplication"
        assert data["confidence"] == 0.95

    def test_finding_from_dict(self) -> None:
        """Test Finding.from_dict() deserialization."""
        now = datetime.now(timezone.utc).isoformat()
        expires = datetime.now(timezone.utc).isoformat()
        
        data = {
            "id": "550e8400e29b41d4a716446655440000",
            "device_id": "pixel_001",
            "finding_type": "StepDuplication",
            "confidence": 0.95,
            "timestamp": now,
            "expires_at": expires,
        }
        
        finding = Finding.from_dict(data)
        assert finding.id == "550e8400e29b41d4a716446655440000"
        assert finding.finding_type == AnomalyType.STEP_DUPLICATION


class TestConfirmationModel:
    """Tests for Confirmation dataclass."""

    def test_confirmation_creation(self) -> None:
        """Test creating a Confirmation."""
        now = datetime.now(timezone.utc).isoformat()
        
        confirmation = Confirmation(
            finding_id="550e8400e29b41d4a716446655440000",
            device_id="pixel_001",
            confirmed=True,
            evidence="Device confirmed via user feedback",
            timestamp=now,
        )
        
        assert confirmation.finding_id == "550e8400e29b41d4a716446655440000"
        assert confirmation.device_id == "pixel_001"
        assert confirmation.confirmed is True
        assert confirmation.evidence == "Device confirmed via user feedback"
        assert confirmation.confirmation_hash is not None

    def test_confirmation_normalizes_hyphenated_uuid(self) -> None:
        """Test that Confirmation normalizes hyphenated UUIDs."""
        now = datetime.now(timezone.utc).isoformat()
        
        confirmation = Confirmation(
            finding_id="550e8400-e29b-41d4-a716-446655440000",
            device_id="pixel_001",
            confirmed=True,
            evidence="Confirmed",
            timestamp=now,
        )
        
        assert confirmation.finding_id == "550e8400e29b41d4a716446655440000"

    def test_confirmation_generates_hash(self) -> None:
        """Test that confirmation_hash is generated automatically."""
        now = datetime.now(timezone.utc).isoformat()
        
        confirmation = Confirmation(
            finding_id="550e8400e29b41d4a716446655440000",
            device_id="pixel_001",
            confirmed=True,
            evidence="Test evidence",
            timestamp=now,
        )
        
        assert confirmation.confirmation_hash is not None
        assert len(confirmation.confirmation_hash) == 32  # MD5 hex length

    def test_confirmation_hash_uses_timestamp_bucket(self) -> None:
        """Test that confirmation hash buckets timestamp to nearest minute."""
        # Create two confirmations with timestamps in the same minute
        base_time = datetime(2024, 1, 1, 12, 30, 0, tzinfo=timezone.utc)
        time1 = base_time.replace(second=15).isoformat()
        time2 = base_time.replace(second=45).isoformat()
        
        confirmation1 = Confirmation(
            finding_id="550e8400e29b41d4a716446655440000",
            device_id="pixel_001",
            confirmed=True,
            evidence="Test",
            timestamp=time1,
        )
        
        confirmation2 = Confirmation(
            finding_id="550e8400e29b41d4a716446655440000",
            device_id="pixel_001",
            confirmed=True,
            evidence="Test",
            timestamp=time2,
        )
        
        # Hashes should be the same because timestamps bucket to the same minute
        assert confirmation1.confirmation_hash == confirmation2.confirmation_hash

    def test_confirmation_hash_different_confirmed_value(self) -> None:
        """Test that different confirmed values produce different hashes."""
        now = datetime.now(timezone.utc).isoformat()
        
        confirmation_true = Confirmation(
            finding_id="550e8400e29b41d4a716446655440000",
            device_id="pixel_001",
            confirmed=True,
            evidence="Confirmed",
            timestamp=now,
        )
        
        confirmation_false = Confirmation(
            finding_id="550e8400e29b41d4a716446655440000",
            device_id="pixel_001",
            confirmed=False,
            evidence="Rejected",
            timestamp=now,
        )
        
        assert confirmation_true.confirmation_hash != confirmation_false.confirmation_hash

    def test_confirmation_invalid_uuid(self) -> None:
        """Test that invalid finding_id raises ValidationError."""
        now = datetime.now(timezone.utc).isoformat()
        
        with pytest.raises(ValidationError) as exc_info:
            Confirmation(
                finding_id="not-a-valid-uuid",
                device_id="pixel_001",
                confirmed=True,
                evidence="Test",
                timestamp=now,
            )
        assert "Invalid finding_id" in str(exc_info.value)

    def test_confirmation_to_dict(self) -> None:
        """Test Confirmation.to_dict() serialization."""
        now = datetime.now(timezone.utc).isoformat()
        
        confirmation = Confirmation(
            finding_id="550e8400e29b41d4a716446655440000",
            device_id="pixel_001",
            confirmed=True,
            evidence="Device confirmed",
            timestamp=now,
        )
        
        data = confirmation.to_dict()
        assert data["finding_id"] == "550e8400e29b41d4a716446655440000"
        assert data["device_id"] == "pixel_001"
        assert data["confirmed"] is True
        assert "confirmation_hash" in data

    def test_confirmation_from_dict(self) -> None:
        """Test Confirmation.from_dict() deserialization."""
        now = datetime.now(timezone.utc).isoformat()
        
        data = {
            "finding_id": "550e8400e29b41d4a716446655440000",
            "device_id": "pixel_001",
            "confirmed": True,
            "evidence": "Confirmed",
            "timestamp": now,
            "confirmation_hash": "somehash",
        }
        
        confirmation = Confirmation.from_dict(data)
        assert confirmation.finding_id == "550e8400e29b41d4a716446655440000"
        assert confirmation.confirmed is True


class TestValidationError:
    """Tests for ValidationError exception."""

    def test_validation_error_raised(self) -> None:
        """Test that ValidationError can be raised and caught."""
        with pytest.raises(ValidationError):
            normalize_uuid("invalid")

    def test_validation_error_has_message(self) -> None:
        """Test that ValidationError includes descriptive message."""
        try:
            normalize_uuid("invalid")
        except ValidationError as e:
            assert str(e) != ""
            assert "UUID" in str(e) or "hex" in str(e)
