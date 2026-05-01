"""Pytest fixtures for ground truth provider testing."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from ground_truth import Claim, ValidationResult, MQTTGroundTruthProvider, NovaGroundTruthProvider


# ─────────────────────────────────────────────────────────────────────────────
# Mock MQTT Provider Fixture
# ─────────────────────────────────────────────────────────────────────────────


class MockMQTTProvider(MQTTGroundTruthProvider):
    """Mock MQTT provider for testing without broker dependency."""

    def __init__(self, offline_device_id: Optional[str] = None):
        """Initialize with test data.
        
        Args:
            offline_device_id: Optional device ID that should be marked offline
        """
        super().__init__(
            broker_host="mock-broker",
            broker_port=1883,
            cache_ttl_seconds=30,
        )
        self.connected = True
        self.offline_device_id = offline_device_id
        self._populate_test_data()

    def _populate_test_data(self):
        """Pre-populate sensor cache with test data."""
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(seconds=60)  # 60 seconds ago (offline)
        
        # Device 1: pixel-9-pro-xl with GPS and WiFi data
        if self.offline_device_id != "pixel-9-pro-xl":
            self._sensor_cache["pixel-9-pro-xl"] = {
                "GPS.POSITION": {
                    "data": {
                        "gps_fix": True,
                        "valid_fix": True,
                        "latitude": 52.5,
                        "longitude": 13.4,
                        "accuracy_m": 5.0,
                        "age_ms": 100,
                    },
                    "timestamp": now,
                    "freshness_ms": 100,
                },
                "WIFI.NEARBY_NETWORKS": {
                    "data": {
                        "networks": [
                            {"ssid": "HomeNet", "rssi": -45},
                            {"ssid": "CoffeeWiFi", "rssi": -65},
                            {"ssid": "GuestNet", "rssi": -78},
                        ],
                        "nearby_count": 3,
                        "age_ms": 500,
                    },
                    "timestamp": now,
                    "freshness_ms": 500,
                },
            }
        
        # Device 2: test-device with battery and CPU data
        if self.offline_device_id != "test-device":
            self._sensor_cache["test-device"] = {
                "BATTERY.LEVEL": {
                    "data": {
                        "battery_pct": 75,
                        "age_ms": 200,
                    },
                    "timestamp": now,
                    "freshness_ms": 200,
                },
                "CPU.USAGE": {
                    "data": {
                        "cpu_usage": 45.5,
                        "age_ms": 300,
                    },
                    "timestamp": now,
                    "freshness_ms": 300,
                },
            }
        
        # Update device presence
        heartbeat_time = old_time if self.offline_device_id == "pixel-9-pro-xl" else now
        self._device_presence["pixel-9-pro-xl"] = {
            "present": self.offline_device_id != "pixel-9-pro-xl",
            "last_heartbeat": heartbeat_time,
        }
        
        heartbeat_time = old_time if self.offline_device_id == "test-device" else now
        self._device_presence["test-device"] = {
            "present": self.offline_device_id != "test-device",
            "last_heartbeat": heartbeat_time,
        }

    async def connect(self) -> bool:
        """Mock connect (always succeeds)."""
        self.connected = True
        return True

    async def close(self):
        """Mock close."""
        self.connected = False


# ─────────────────────────────────────────────────────────────────────────────
# Mock Nova Provider Fixture
# ─────────────────────────────────────────────────────────────────────────────


class MockNovaProvider(NovaGroundTruthProvider):
    """Mock Nova provider for testing without Great Library dependency."""

    def __init__(self, timeout_mode: Optional[str] = None, timeout_seconds: int = 2):
        """Initialize with test data.
        
        Args:
            timeout_mode: Optional "always" or "first_attempt" to simulate timeouts
            timeout_seconds: Seconds to sleep when in timeout mode
        """
        super().__init__()
        self.nova_available = True
        self.timeout_mode = timeout_mode
        self.timeout_seconds = timeout_seconds
        self.attempt_count = 0

    async def validate(
        self,
        claim: Claim,
        context: Optional[Dict[str, Any]] = None,
    ) -> ValidationResult:
        """Mock Nova validation with optional timeout simulation."""
        # Simulate timeouts if configured
        if self.timeout_mode:
            self.attempt_count += 1
            if self.timeout_mode == "always":
                await asyncio.sleep(self.timeout_seconds)
                raise RuntimeError(f"Nova validation timed out")
            elif self.timeout_mode == "first_attempt" and self.attempt_count == 1:
                await asyncio.sleep(self.timeout_seconds)
                raise RuntimeError(f"Nova validation timed out on first attempt")
        
        # GPS claims should validate successfully
        if "GPS" in claim.subject.upper():
            return ValidationResult(
                claim_id=claim.id,
                is_valid=True,
                ground_truth_value={"valid_fix": True},
                evidence=[
                    {"source": "mock_library", "relevance": 0.95}
                ],
                confidence=0.85,
                metadata={
                    "provider": "nova_mock",
                    "status": "verified",
                    "latency_ms": 50,
                },
            )
        
        # WiFi claims should validate successfully
        if "WIFI" in claim.subject.upper():
            return ValidationResult(
                claim_id=claim.id,
                is_valid=True,
                ground_truth_value={"networks": ["HomeNet", "CoffeeWiFi"]},
                evidence=[
                    {"source": "mock_library", "relevance": 0.90}
                ],
                confidence=0.80,
                metadata={
                    "provider": "nova_mock",
                    "status": "verified",
                    "latency_ms": 50,
                },
            )
        
        # Default: return valid
        return ValidationResult(
            claim_id=claim.id,
            is_valid=True,
            ground_truth_value=claim.expected_value,
            evidence=[],
            confidence=0.75,
            metadata={
                "provider": "nova_mock",
                "status": "verified",
                "latency_ms": 50,
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# Pytest Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def mock_mqtt_provider():
    """Provide a mock MQTT provider with test data pre-populated."""
    provider = MockMQTTProvider()
    await provider.connect()
    yield provider
    await provider.close()


@pytest.fixture
async def mock_mqtt_provider_offline():
    """Provide a mock MQTT provider with a device offline."""
    provider = MockMQTTProvider(offline_device_id="pixel-9-pro-xl")
    await provider.connect()
    yield provider
    await provider.close()


@pytest.fixture
async def mock_nova_provider():
    """Provide a mock Nova provider with test data pre-populated."""
    provider = MockNovaProvider()
    yield provider


@pytest.fixture
async def mock_nova_provider_timeout():
    """Provide a mock Nova provider that times out on first attempt."""
    provider = MockNovaProvider(timeout_mode="first_attempt", timeout_seconds=0.1)
    yield provider


# ─────────────────────────────────────────────────────────────────────────────
# Sample Data Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_claim():
    """Create a sample Claim for testing."""
    return Claim(
        id="test-claim-1",
        statement="GPS position is valid",
        claim_type="telemetry",
        subject="GPS.POSITION",
        expected_value={"valid_fix": True, "latitude": 52.5, "longitude": 13.4},
        confidence_model=0.8,
    )


@pytest.fixture
def sample_claims():
    """Create multiple sample Claims for batch testing."""
    return [
        Claim(
            id="test-claim-1",
            statement="GPS has valid fix",
            claim_type="telemetry",
            subject="GPS.POSITION",
            expected_value={"valid_fix": True},
            confidence_model=0.8,
        ),
        Claim(
            id="test-claim-2",
            statement="WiFi networks detected",
            claim_type="telemetry",
            subject="WIFI.NEARBY_NETWORKS",
            expected_value={"nearby_count": 2},
            confidence_model=0.7,
        ),
        Claim(
            id="test-claim-3",
            statement="Bluetooth devices nearby",
            claim_type="telemetry",
            subject="BLUETOOTH",
            expected_value={"bt_device_count": 2},
            confidence_model=0.6,
        ),
    ]
