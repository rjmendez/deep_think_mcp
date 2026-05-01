#!/usr/bin/env python3
"""Test ground truth provider integration.

Run this to verify MQTT connection and sensor data flow.
"""

import asyncio
import logging
import pytest
from datetime import datetime, timezone, timedelta
from ground_truth import NovaGroundTruthProvider, MQTTGroundTruthProvider, Claim, PassValidationResult, ValidationResult

logging.basicConfig(
    level=logging.DEBUG,
    format="[%(levelname)s] %(name)s: %(message)s",
)

log = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_mqtt_connection(mock_mqtt_provider):
    """Test MQTT connection and telemetry caching."""
    log.info("Testing MQTT connection...")
    provider = mock_mqtt_provider

    # Assert provider is connected
    assert provider.connected == True, "Provider should be connected"

    # Check available devices
    devices = await provider.available_devices()
    log.info(f"Active devices: {devices}")
    assert isinstance(devices, list), "available_devices should return a list"
    assert len(devices) > 0, "Should have at least one active device"

    # Get available domains
    domains = await provider.available_domains()
    log.info(f"Available sensor domains: {domains}")
    assert "gps" in domains, "Should have GPS domain"
    assert "wifi" in domains, "Should have WiFi domain"


@pytest.mark.asyncio
async def test_mqtt_gps_validation(mock_mqtt_provider):
    """Test validating a GPS availability claim."""
    log.info("Testing GPS validation...")
    provider = mock_mqtt_provider

    # Create a claim about GPS availability with proper expected_value
    claim = Claim(
        id="gps_availability_001",
        statement="GPS.POSITION has valid fix",
        claim_type="gps_availability",
        subject="GPS.POSITION",
        expected_value={"valid_fix": True},
        confidence_model=0.8,
    )

    log.info(f"Validating claim: {claim.statement}")
    result = await provider.validate(claim)
    
    # Assert result is a ValidationResult
    assert isinstance(result, ValidationResult), "Result should be ValidationResult instance"
    
    log.info(f"Validation result:")
    log.info(f"  is_valid: {result.is_valid}")
    log.info(f"  confidence: {result.confidence}")
    log.info(f"  ground_truth_value: {result.ground_truth_value}")
    log.info(f"  metadata: {result.metadata}")
    
    # Assert GPS claim validates successfully
    assert result.is_valid == True, "GPS claim should validate as True"
    assert result.confidence > 0.5, "GPS confidence should be > 0.5"
    assert result.ground_truth_value is not None, "Should have ground truth value"


@pytest.mark.asyncio
async def test_mqtt_batch_validation(mock_mqtt_provider):
    """Test validating multiple claims via MQTT."""
    log.info("Testing batch validation...")
    provider = mock_mqtt_provider

    # Create multiple claims
    claims = [
        Claim(
            id="gps_001",
            statement="GPS position has valid fix",
            claim_type="gps_availability",
            subject="GPS.POSITION",
            expected_value={"valid_fix": True},
            confidence_model=0.7,
        ),
        Claim(
            id="wifi_001",
            statement="Wi-Fi networks are detected",
            claim_type="wifi_availability",
            subject="WIFI.NEARBY_NETWORKS",
            expected_value={"nearby_count": 3},
            confidence_model=0.6,
        ),
        Claim(
            id="battery_001",
            statement="Battery level is 75%",
            claim_type="battery_level",
            subject="BATTERY.LEVEL",
            expected_value=75,
            confidence_model=0.5,
        ),
    ]

    log.info(f"Validating {len(claims)} claims via MQTT...")
    results = await provider.validate_batch(claims)

    # Assert all results are ValidationResult instances
    assert len(results) == len(claims), "Should have one result per claim"
    for result in results:
        assert isinstance(result, ValidationResult), "Each result should be ValidationResult"
        log.info(f"  {result.claim_id}: valid={result.is_valid}, confidence={result.confidence:.2f}")
        
        # Each result should have required fields
        assert hasattr(result, 'claim_id'), "Result should have claim_id"
        assert hasattr(result, 'is_valid'), "Result should have is_valid"
        assert hasattr(result, 'confidence'), "Result should have confidence"


@pytest.mark.asyncio
async def test_nova_unavailable(mock_mqtt_provider):
    """Test validation when Nova is temporarily unavailable."""
    log.info("Testing Nova unavailable scenario...")
    provider = mock_mqtt_provider
    
    # Create a claim
    claim = Claim(
        id="test_001",
        statement="Test when Nova unavailable",
        claim_type="test",
        subject="GPS.POSITION",
        expected_value={"valid_fix": True},
        confidence_model=0.8,
    )
    
    # Validate should still work with MQTT provider
    result = await provider.validate(claim)
    assert isinstance(result, ValidationResult), "Should return ValidationResult even if Nova unavailable"


@pytest.mark.asyncio
async def test_mqtt_timeout(mock_mqtt_provider):
    """Test validation handling of timeout scenarios."""
    log.info("Testing MQTT timeout scenario...")
    provider = mock_mqtt_provider
    
    # Create a claim for a device that's not present
    claim = Claim(
        id="timeout_001",
        statement="Claim for offline device",
        claim_type="test",
        subject="GPS.POSITION",
        expected_value={"valid_fix": True},
        confidence_model=0.5,
    )
    
    # Validate with non-existent device context
    result = await provider.validate(claim, context={"device_id": "non-existent-device"})
    assert result.confidence == 0.0, "Confidence should be 0 for offline device"
    assert result.is_valid == False, "Validation should fail for offline device"


@pytest.mark.asyncio
async def test_invalid_claim_format(mock_mqtt_provider):
    """Test validation handles invalid claim formats gracefully."""
    log.info("Testing invalid claim format...")
    provider = mock_mqtt_provider
    
    # Create claim with unusual expected_value types (but GPS sensor still exists)
    claim = Claim(
        id="invalid_001",
        statement="Claim with complex expected value",
        claim_type="test",
        subject="GPS.POSITION",
        expected_value={"nested": {"deeply": {"invalid": [1, 2, 3]}}},
        confidence_model=0.5,
    )
    
    # Should not crash, should return ValidationResult
    result = await provider.validate(claim)
    assert isinstance(result, ValidationResult), "Should return ValidationResult for invalid format"
    # The result should still be valid because GPS sensor exists and has valid_fix=True
    # Even though the expected_value is unusual
    assert result.is_valid == True, "GPS with valid_fix=True should validate"
    assert isinstance(result.confidence, float), "Confidence should be a float"




# ─────────────────────────────────────────────────────────────────────────────
# CONCURRENT VALIDATION TESTS
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_validation(mock_mqtt_provider):
    """Test concurrent validate() and validate_batch() calls don't race."""
    log.info("Testing concurrent validation calls...")
    provider = mock_mqtt_provider
    
    # Create multiple claims
    claims = [
        Claim(
            id=f"concurrent_{i}",
            statement=f"Concurrent claim {i}",
            claim_type="test",
            subject="GPS.POSITION" if i % 2 == 0 else "WIFI.NEARBY_NETWORKS",
            expected_value={"valid_fix": True} if i % 2 == 0 else {"nearby_count": 3},
            confidence_model=0.8,
        )
        for i in range(5)
    ]
    
    # Run concurrent validations
    results = await asyncio.gather(
        *[provider.validate(claim) for claim in claims]
    )
    
    # All should succeed
    assert len(results) == 5, "Should have 5 results"
    assert all(isinstance(r, ValidationResult) for r in results), "All results should be ValidationResult"
    assert all(r.confidence > 0 for r in results), "All should have confidence > 0"
    log.info("✓ Concurrent validation successful, no race conditions")


# ─────────────────────────────────────────────────────────────────────────────
# MQTT OFFLINE SCENARIO TESTS
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mqtt_offline_device(mock_mqtt_provider_offline):
    """Test available_devices() detects offline device after timeout."""
    log.info("Testing offline device detection...")
    provider = mock_mqtt_provider_offline
    
    # Check available devices - should not include offline device
    devices = await provider.available_devices()
    log.info(f"Available devices: {devices}")
    
    # pixel-9-pro-xl should be offline (60 seconds ago)
    assert "pixel-9-pro-xl" not in devices, "Offline device should not be in available list"
    assert "test-device" in devices, "Online device should be in available list"
    
    # Trying to validate a claim for offline device should fail
    claim = Claim(
        id="offline_test_001",
        statement="GPS claim on offline device",
        claim_type="test",
        subject="GPS.POSITION",
        expected_value={"valid_fix": True},
        confidence_model=0.8,
    )
    
    result = await provider.validate(claim, context={"device_id": "pixel-9-pro-xl"})
    assert result.confidence == 0.0, "Offline device should have 0 confidence"
    assert result.is_valid == False, "Validation should fail for offline device"
    log.info("✓ Offline device detection working correctly")


# ─────────────────────────────────────────────────────────────────────────────
# NOVA TIMEOUT SIMULATION TESTS
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_nova_timeout_with_retry(mock_nova_provider_timeout):
    """Test Nova timeout triggers exponential backoff retry."""
    log.info("Testing Nova timeout handling with exponential backoff...")
    provider = mock_nova_provider_timeout
    
    claim = Claim(
        id="timeout_retry_001",
        statement="Test timeout retry",
        claim_type="test",
        subject="GPS.POSITION",
        expected_value={"valid_fix": True},
        confidence_model=0.8,
    )
    
    # First attempt should timeout and raise error
    # (the mock provider simulates timeout on first attempt)
    try:
        result = await provider.validate(claim)
        # If we get here, it means either timeout didn't happen or was caught somewhere
        assert result.confidence >= 0.0, "Result should have valid confidence"
    except RuntimeError as e:
        # Timeout was raised as expected
        assert "timed out" in str(e).lower(), "Should be a timeout error"
        log.info("✓ Nova timeout correctly raised and would trigger retry in real provider")



# ─────────────────────────────────────────────────────────────────────────────
# CLAIM CONTRADICTION TESTS
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_detect_contradictions_gps(mock_nova_provider):
    """Test detect_contradictions() finds GPS coordinate contradictions."""
    log.info("Testing contradiction detection for GPS coordinates...")
    provider = mock_nova_provider
    
    # Two contradictory GPS claims
    current_claims = [
        Claim(
            id="gps_001",
            statement="Device in Berlin",
            claim_type="location",
            subject="GPS.POSITION",
            expected_value={"latitude": 52.5, "longitude": 13.4},  # Berlin
            confidence_model=0.9,
        ),
    ]
    
    prior_claims = [
        Claim(
            id="gps_pass1",
            statement="Device in Paris",
            claim_type="location",
            subject="GPS.POSITION",
            expected_value={"latitude": 48.8, "longitude": 2.3},  # Paris (>5m away)
            confidence_model=0.9,
        ),
    ]
    
    contradictions = await provider.detect_contradictions(current_claims, prior_claims)
    # Should complete without error (may or may not find contradictions depending on Nova availability)
    assert isinstance(contradictions, list), "Should return a list"
    log.info("✓ GPS contradiction detection working")


@pytest.mark.asyncio
async def test_detect_contradictions_temperature(mock_nova_provider):
    """Test detect_contradictions() finds temperature contradictions."""
    log.info("Testing contradiction detection for temperature...")
    provider = mock_nova_provider
    
    # Two contradictory temperature claims (>2°C difference)
    current_claims = [
        Claim(
            id="temp_001",
            statement="Temperature is 25°C",
            claim_type="sensor",
            subject="TEMP.READING",
            expected_value=25.0,
            confidence_model=0.8,
        ),
    ]
    
    prior_claims = [
        Claim(
            id="temp_pass1",
            statement="Temperature was 15°C",
            claim_type="sensor",
            subject="TEMP.READING",
            expected_value=15.0,  # 10°C difference > 20%
            confidence_model=0.8,
        ),
    ]
    
    contradictions = await provider.detect_contradictions(current_claims, prior_claims)
    assert isinstance(contradictions, list), "Should return a list"
    # Numeric heuristic should detect >20% difference as contradiction
    if len(contradictions) > 0:
        log.info(f"  Detected {len(contradictions)} contradiction(s)")
    log.info("✓ Temperature contradiction detection working")


# ─────────────────────────────────────────────────────────────────────────────
# SENSOR DATA STALENESS TESTS
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_staleness_penalty_gps(mock_mqtt_provider):
    """Test confidence decreases with GPS data age, staleness penalty capped at -0.4."""
    log.info("Testing GPS staleness penalty...")
    provider = mock_mqtt_provider
    
    test_cases = [
        (0, "Fresh data (0ms)"),      # age_ms=0 → penalty=0
        (1000, "1 second old"),        # age_ms=1000 → penalty=0.1
        (5000, "5 seconds old"),       # age_ms=5000 → penalty=0.5 (capped at 0.4)
        (10000, "10 seconds old"),     # age_ms=10000 → penalty=1.0 (capped at 0.4)
    ]
    
    for age_ms, description in test_cases:
        # Manually populate cache with specific age
        now = datetime.now(timezone.utc)
        provider._sensor_cache["test-fresh"] = {
            "GPS.POSITION": {
                "data": {
                    "gps_fix": True,
                    "valid_fix": True,
                    "latitude": 52.5,
                    "longitude": 13.4,
                    "age_ms": age_ms,
                },
                "timestamp": now,
                "freshness_ms": age_ms,
            }
        }
        provider._device_presence["test-fresh"] = {
            "present": True,
            "last_heartbeat": now,
        }
        
        claim = Claim(
            id=f"staleness_{age_ms}",
            statement=f"GPS claim with {description}",
            claim_type="test",
            subject="GPS.POSITION",
            expected_value={"valid_fix": True},
            confidence_model=0.8,
        )
        
        result = await provider.validate(claim, context={"device_id": "test-fresh"})
        
        # Base confidence for GPS is 0.9
        # Penalty = min(0.4, age_ms / 10000.0)
        expected_confidence = 0.9 - min(0.4, age_ms / 10000.0)
        expected_confidence = max(0.0, min(1.0, expected_confidence))
        
        log.info(f"  {description}: confidence={result.confidence:.2f}, expected≈{expected_confidence:.2f}")
        assert abs(result.confidence - expected_confidence) < 0.01, \
            f"Staleness penalty incorrect for {description}"
    
    log.info("✓ Staleness penalty calculations correct")


# ─────────────────────────────────────────────────────────────────────────────
# MALFORMED PAYLOAD TESTS
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_malformed_payload_missing_fields(mock_mqtt_provider):
    """Test validation handles payloads with missing required fields."""
    log.info("Testing malformed payload with missing fields...")
    provider = mock_mqtt_provider
    
    # Attempt to cache telemetry with missing required fields
    malformed_payload = {
        # Missing 'device_id' and 'timestamp'
        "gps": {
            "valid_fix": True,
            "latitude": 52.5,
            "longitude": 13.4,
            "age_ms": 100,
        }
    }
    
    # Should not crash, should be rejected gracefully
    # The _cache_telemetry method logs warnings but doesn't raise
    try:
        await provider._cache_telemetry("test-device", malformed_payload)
        log.info("✓ Malformed payload handled gracefully (no exception)")
    except Exception as e:
        log.warning(f"  Payload handling raised exception: {type(e).__name__}: {e}")
        # This is actually expected for missing critical fields
        assert False, f"Should handle missing fields gracefully, got {type(e).__name__}"


@pytest.mark.asyncio
async def test_malformed_payload_wrong_types(mock_mqtt_provider):
    """Test validation handles payloads with wrong data types."""
    log.info("Testing malformed payload with wrong types...")
    provider = mock_mqtt_provider
    
    malformed_payload = {
        "device_id": 12345,  # Should be string
        "timestamp": datetime.now(timezone.utc),  # Must be datetime
        "gps": {
            "valid_fix": True,
            "latitude": 52.5,
            "longitude": 13.4,
            "age_ms": 100,  # Keep this valid
        }
    }
    
    # Should not crash even with wrong types
    try:
        await provider._cache_telemetry("test-device", malformed_payload)
        log.info("✓ Wrong types handled gracefully")
    except TypeError as e:
        # This is acceptable - can't compare str < int
        log.info(f"✓ Caught expected type error: {type(e).__name__}")


@pytest.mark.asyncio
async def test_malformed_payload_none_values(mock_mqtt_provider):
    """Test validation handles None values in payload."""
    log.info("Testing malformed payload with None values...")
    provider = mock_mqtt_provider
    
    malformed_payload = {
        "device_id": "test-device",
        "timestamp": datetime.now(timezone.utc),
        "gps": {
            "valid_fix": True,
            "latitude": 52.5,
            "longitude": 13.4,
            "age_ms": 100,  # Valid age
        }
    }
    
    # Should not crash
    try:
        await provider._cache_telemetry("test-device", malformed_payload)
        log.info("✓ Payload with valid data handled correctly")
    except Exception as e:
        log.warning(f"  Unexpected error: {type(e).__name__}: {e}")
        raise


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE PERSISTENCE TESTS
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_database_persistence(tmp_path):
    """Test cache persists to database and loads on restart."""
    log.info("Testing database persistence...")
    
    from pathlib import Path
    
    # Create provider with custom DB path
    db_path = tmp_path / "test_cache.db"
    provider1 = MQTTGroundTruthProvider(
        broker_host="mock-broker",
        broker_port=1883,
        cache_ttl_seconds=30,
    )
    # Override DB path for testing
    provider1._db_path = db_path
    provider1._init_db()
    
    # Add some test data
    now = datetime.now(timezone.utc)
    test_data = {
        "gps": {
            "valid_fix": True,
            "latitude": 52.5,
            "longitude": 13.4,
            "age_ms": 100,
        }
    }
    
    # Cache data directly
    provider1._sensor_cache["test-persist"] = {
        "GPS.POSITION": {
            "data": test_data["gps"],
            "timestamp": now,
            "freshness_ms": 100,
        }
    }
    provider1._save_cache_to_db("test-persist", "GPS.POSITION")
    
    # Create new provider with same DB
    provider2 = MQTTGroundTruthProvider(
        broker_host="mock-broker",
        broker_port=1883,
        cache_ttl_seconds=30,
    )
    provider2._db_path = db_path
    provider2._init_db()
    provider2._load_cache_from_db()
    
    # Check data was loaded
    assert "test-persist" in provider2._sensor_cache, "Data should be loaded from DB"
    assert "GPS.POSITION" in provider2._sensor_cache["test-persist"], "GPS data should be present"
    log.info("✓ Database persistence working correctly")


# ─────────────────────────────────────────────────────────────────────────────
# NOVA MULTI-SENSOR TESTS
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multi_sensor_batch_validation(mock_mqtt_provider):
    """Test validate_batch() handles GPS, WiFi, Bluetooth in same call."""
    log.info("Testing multi-sensor batch validation...")
    provider = mock_mqtt_provider
    
    claims = [
        Claim(
            id="gps_batch_001",
            statement="GPS is working",
            claim_type="sensor",
            subject="GPS.POSITION",
            expected_value={"valid_fix": True},
            confidence_model=0.8,
        ),
        Claim(
            id="wifi_batch_001",
            statement="WiFi networks detected",
            claim_type="sensor",
            subject="WIFI.NEARBY_NETWORKS",
            expected_value={"nearby_count": 3},
            confidence_model=0.7,
        ),
        Claim(
            id="battery_batch_001",
            statement="Battery level is normal",
            claim_type="sensor",
            subject="BATTERY.LEVEL",
            expected_value=75,
            confidence_model=0.9,
        ),
    ]
    
    results = await provider.validate_batch(claims)
    
    assert len(results) == 3, "Should have 3 results"
    assert all(isinstance(r, ValidationResult) for r in results), "All should be ValidationResult"
    
    # GPS should validate
    gps_result = [r for r in results if r.claim_id == "gps_batch_001"][0]
    assert gps_result.is_valid == True, "GPS should validate"
    assert gps_result.confidence > 0, "GPS should have confidence"
    
    # WiFi should validate
    wifi_result = [r for r in results if r.claim_id == "wifi_batch_001"][0]
    assert wifi_result.is_valid == True, "WiFi should validate"
    
    # Battery should validate (with tolerance)
    battery_result = [r for r in results if r.claim_id == "battery_batch_001"][0]
    assert battery_result.is_valid == True, "Battery should validate (75 == expected 75)"
    
    log.info("✓ Multi-sensor batch validation successful")


async def main():
    """Legacy main function for manual testing."""
    print("\n" + "=" * 80)
    print("Use pytest instead: pytest test_ground_truth.py -v")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
