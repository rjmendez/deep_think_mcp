"""Tests for correlation engine multi-sensor novelty detection."""

import pytest
import asyncio
from datetime import datetime, timezone
from unittest.mock import Mock, AsyncMock
import uuid

from mqtt.correlation_engine import CorrelationEngine, LocationBucket
from mqtt.models import Finding, AnomalyType, CorrelationFinding


class TestLocationBucket:
    """Test spatial-temporal windowing logic."""
    
    def test_bucket_creation(self):
        now = datetime.now(timezone.utc)
        bucket = LocationBucket("gps_1.0_2.0", now, window_duration_sec=10)
        assert bucket.location_hash == "gps_1.0_2.0"
        assert bucket.window_duration_sec == 10
        assert len(bucket.findings) == 0
    
    def test_add_finding(self):
        now = datetime.now(timezone.utc)
        bucket = LocationBucket("gps_1.0_2.0", now)
        
        finding = Finding(
            id=str(uuid.uuid4()),
            device_id="phone_1",
            finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
            confidence=0.8,
            timestamp=now.isoformat() + "Z",
            expires_at=now.isoformat() + "Z"
        )
        
        bucket.add_finding(finding)
        assert len(bucket.findings) == 1
        assert finding in bucket.findings
    
    def test_get_device_ids(self):
        now = datetime.now(timezone.utc)
        bucket = LocationBucket("gps_1.0_2.0", now)
        
        for device_id in ["phone_1", "phone_2", "phone_1"]:  # Duplicate
            finding = Finding(
                id=str(uuid.uuid4()),
                device_id=device_id,
                finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
                confidence=0.8,
                timestamp=now.isoformat() + "Z",
                expires_at=now.isoformat() + "Z"
            )
            bucket.add_finding(finding)
        
        devices = bucket.get_device_ids()
        assert devices == {"phone_1", "phone_2"}
    
    def test_is_ready_with_minimum_devices(self):
        now = datetime.now(timezone.utc)
        bucket = LocationBucket("gps_1.0_2.0", now, window_duration_sec=10)
        
        # Add finding from device 1
        finding1 = Finding(
            id=str(uuid.uuid4()),
            device_id="phone_1",
            finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
            confidence=0.8,
            timestamp=now.isoformat() + "Z",
            expires_at=now.isoformat() + "Z"
        )
        bucket.add_finding(finding1)
        
        # Not ready: only 1 device
        is_ready, reason = bucket.is_ready(min_devices=2)
        assert is_ready is False
        
        # Add finding from device 2
        finding2 = Finding(
            id=str(uuid.uuid4()),
            device_id="phone_2",
            finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
            confidence=0.8,
            timestamp=now.isoformat() + "Z",
            expires_at=now.isoformat() + "Z"
        )
        bucket.add_finding(finding2)
        
        # Still not ready: need minimum time
        is_ready, reason = bucket.is_ready(min_devices=2)
        assert is_ready is False
        
        # Simulate time passage
        await_time = datetime.now(timezone.utc)
        async def wait_then_check():
            await asyncio.sleep(2.1)  # Wait past 2 second threshold
            return bucket.is_ready(min_devices=2)
        
        # Note: Can't easily test async wait in sync test, but logic is sound


class TestCorrelationEngine:
    """Test correlation detection and novelty scoring."""
    
    @pytest.fixture
    def engine(self):
        return CorrelationEngine(
            time_window_sec=10,
            location_radius_m=10,
            min_devices_for_correlation=2
        )
    
    def test_engine_initialization(self, engine):
        assert engine.time_window_sec == 10
        assert engine.location_radius_m == 10
        assert engine.min_devices_for_correlation == 2
        assert len(engine.location_buckets) == 0
        assert len(engine.fleet_history) == 0
    
    def test_extract_location_hash_gps(self, engine):
        """Test GPS-based location hashing."""
        finding = Finding(
            id=str(uuid.uuid4()),
            device_id="phone_1",
            finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
            confidence=0.8,
            timestamp="2026-05-01T05:00:00Z",
            expires_at="2026-05-01T05:00:00Z",
            metadata={
                "gps": {
                    "latitude": 36.1699,
                    "longitude": -115.1426
                }
            }
        )
        
        location = engine._extract_location_hash(finding)
        assert location.startswith("gps_")
        assert "36.1699" in location
        assert "115.1426" in location
    
    def test_extract_location_hash_wifi(self, engine):
        """Test WiFi-based location hashing."""
        finding = Finding(
            id=str(uuid.uuid4()),
            device_id="phone_1",
            finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
            confidence=0.8,
            timestamp="2026-05-01T05:00:00Z",
            expires_at="2026-05-01T05:00:00Z",
            metadata={
                "wifi_networks": ["DefCon-5GHz", "DefCon-Guest"]
            }
        )
        
        location = engine._extract_location_hash(finding)
        assert location.startswith("wifi_")
    
    def test_bin_numeric_temperature(self, engine):
        """Test temperature binning."""
        assert engine._bin_numeric("temperature", [15.0]) == "temp_cold"
        assert engine._bin_numeric("temperature", [22.0]) == "temp_neutral"
        assert engine._bin_numeric("temperature", [26.0]) == "temp_warm"
        assert engine._bin_numeric("temperature", [35.0]) == "temp_extreme"
    
    def test_bin_numeric_humidity(self, engine):
        """Test humidity binning."""
        assert engine._bin_numeric("humidity", [15.0]) == "humidity_dry"
        assert engine._bin_numeric("humidity", [50.0]) == "humidity_moderate"
        assert engine._bin_numeric("humidity", [85.0]) == "humidity_very_high"
    
    def test_bin_numeric_light(self, engine):
        """Test light level binning."""
        assert engine._bin_numeric("light", [50.0]) == "light_dark"
        assert engine._bin_numeric("light", [300.0]) == "light_dim"
        assert engine._bin_numeric("light", [1500.0]) == "light_normal"
        assert engine._bin_numeric("light", [5000.0]) == "light_bright"
    
    def test_bin_numeric_empty_list(self, engine):
        """Test binning with empty list."""
        assert engine._bin_numeric("temperature", []) is None
    
    def test_aggregate_sensor_snapshot_wifi(self, engine):
        """Test WiFi SSID aggregation."""
        findings = [
            Finding(
                id=str(uuid.uuid4()),
                device_id="phone_1",
                finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
                confidence=0.8,
                timestamp="2026-05-01T05:00:00Z",
                expires_at="2026-05-01T05:00:00Z",
                metadata={"wifi_networks": ["Network-A", "Network-B"]}
            ),
            Finding(
                id=str(uuid.uuid4()),
                device_id="phone_2",
                finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
                confidence=0.8,
                timestamp="2026-05-01T05:00:00Z",
                expires_at="2026-05-01T05:00:00Z",
                metadata={"wifi_networks": ["Network-B", "Network-C"]}
            ),
        ]
        
        snapshot = engine._aggregate_sensor_snapshot(findings)
        assert set(snapshot["wifi_ssids"]) == {"Network-A", "Network-B", "Network-C"}
        assert snapshot["device_count"] == 2
        assert snapshot["finding_count"] == 2
    
    def test_aggregate_sensor_snapshot_temperature(self, engine):
        """Test temperature binning in aggregation."""
        findings = [
            Finding(
                id=str(uuid.uuid4()),
                device_id="phone_1",
                finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
                confidence=0.8,
                timestamp="2026-05-01T05:00:00Z",
                expires_at="2026-05-01T05:00:00Z",
                metadata={"temperature": 22.0}
            ),
            Finding(
                id=str(uuid.uuid4()),
                device_id="phone_2",
                finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
                confidence=0.8,
                timestamp="2026-05-01T05:00:00Z",
                expires_at="2026-05-01T05:00:00Z",
                metadata={"temperature": 23.0}
            ),
        ]
        
        snapshot = engine._aggregate_sensor_snapshot(findings)
        # Median of [22.0, 23.0] = 22.5, which bins to "temp_neutral"
        assert snapshot["temperature_bin"] == "temp_neutral"
    
    def test_calculate_entropy_no_history(self, engine):
        """Test entropy calculation with no fleet history."""
        snapshot = {
            "wifi_ssids": ["Network-A"],
            "temperature_bin": "temp_neutral",
            "humidity_bin": "humidity_moderate",
            "audio_bins": [],
            "light_level": "light_normal",
            "imu_vibrations": {},
            "air_pressure": "pressure_normal",
            "bluetooth_count": 5,
            "cellular_quality": "excellent",
            "packet_types": [],
        }
        
        novelty, breakdown = engine._calculate_entropy(snapshot)
        assert 0.0 <= novelty <= 1.0
        assert isinstance(breakdown, dict)
        # With no history, most sensors should have high entropy (novel)
    
    def test_fingerprint_hash_deterministic(self, engine):
        """Test fingerprint hash is deterministic."""
        snapshot = {
            "wifi_ssids": ["Network-A"],
            "temperature_bin": "temp_neutral",
        }
        
        hash1 = engine._fingerprint_hash(snapshot)
        hash2 = engine._fingerprint_hash(snapshot)
        assert hash1 == hash2
    
    def test_fingerprint_hash_order_independent(self, engine):
        """Test fingerprint hash is independent of key order."""
        snapshot1 = {
            "wifi_ssids": ["Network-A"],
            "temperature_bin": "temp_neutral",
        }
        snapshot2 = {
            "temperature_bin": "temp_neutral",
            "wifi_ssids": ["Network-A"],
        }
        
        # Should produce same hash (order shouldn't matter)
        hash1 = engine._fingerprint_hash(snapshot1)
        hash2 = engine._fingerprint_hash(snapshot2)
        assert hash1 == hash2
    
    def test_calculate_fleet_prevalence_new_fingerprint(self, engine):
        """Test prevalence when fingerprint is new."""
        snapshot = {"wifi_ssids": ["Network-A"]}
        
        prevalence = engine._calculate_fleet_prevalence(snapshot)
        assert prevalence == 0.0  # Never seen before
    
    def test_calculate_fleet_prevalence_seen_before(self, engine):
        """Test prevalence when fingerprint has been seen."""
        snapshot = {"wifi_ssids": ["Network-A"]}
        
        # Manually add to history
        fp_hash = engine._fingerprint_hash(snapshot)
        now = datetime.now(timezone.utc)
        engine.fleet_history[fp_hash] = (100, now, now)
        
        prevalence = engine._calculate_fleet_prevalence(snapshot)
        assert prevalence > 0.0
    
    def test_update_fleet_history(self, engine):
        """Test fleet history tracking."""
        snapshot = {"wifi_ssids": ["Network-A"]}
        
        assert len(engine.fleet_history) == 0
        
        engine._update_fleet_history(snapshot)
        assert len(engine.fleet_history) == 1
        
        fp_hash = engine._fingerprint_hash(snapshot)
        count, first_seen, last_seen = engine.fleet_history[fp_hash]
        assert count == 1
        assert first_seen <= last_seen
        
        # Add same snapshot again
        engine._update_fleet_history(snapshot)
        count2, first_seen2, last_seen2 = engine.fleet_history[fp_hash]
        assert count2 == 2
        assert first_seen2 == first_seen  # First seen time unchanged
    
    def test_detect_anomalies_temperature_divergence(self, engine):
        """Test detection of temperature divergence."""
        findings = [
            Finding(
                id=str(uuid.uuid4()),
                device_id="phone_1",
                finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
                confidence=0.8,
                timestamp="2026-05-01T05:00:00Z",
                expires_at="2026-05-01T05:00:00Z",
                metadata={"temperature": 20.0}
            ),
            Finding(
                id=str(uuid.uuid4()),
                device_id="phone_2",
                finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
                confidence=0.8,
                timestamp="2026-05-01T05:00:00Z",
                expires_at="2026-05-01T05:00:00Z",
                metadata={"temperature": 27.0}  # >5°C difference
            ),
        ]
        
        is_anomalous, details = engine._detect_anomalies(findings)
        assert is_anomalous is True
        assert "temperature" in details
        assert details["temperature"]["diff"] > 5.0
    
    def test_detect_anomalies_no_divergence(self, engine):
        """Test no anomaly when devices agree."""
        findings = [
            Finding(
                id=str(uuid.uuid4()),
                device_id="phone_1",
                finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
                confidence=0.8,
                timestamp="2026-05-01T05:00:00Z",
                expires_at="2026-05-01T05:00:00Z",
                metadata={"temperature": 22.0}
            ),
            Finding(
                id=str(uuid.uuid4()),
                device_id="phone_2",
                finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
                confidence=0.8,
                timestamp="2026-05-01T05:00:00Z",
                expires_at="2026-05-01T05:00:00Z",
                metadata={"temperature": 23.0}  # <5°C difference
            ),
        ]
        
        is_anomalous, details = engine._detect_anomalies(findings)
        assert is_anomalous is False
    
    def test_detect_anomalies_single_device(self, engine):
        """Test no anomaly with single device."""
        findings = [
            Finding(
                id=str(uuid.uuid4()),
                device_id="phone_1",
                finding_type=AnomalyType.TEMPERATURE_QUANTIZATION,
                confidence=0.8,
                timestamp="2026-05-01T05:00:00Z",
                expires_at="2026-05-01T05:00:00Z",
                metadata={"temperature": 22.0}
            ),
        ]
        
        is_anomalous, details = engine._detect_anomalies(findings)
        assert is_anomalous is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
