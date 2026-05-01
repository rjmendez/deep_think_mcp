"""Integration tests for MQTT provider.

These tests validate real MQTT communication with a broker.
They are marked as 'integration' and can be skipped with:
    pytest -m "not integration"
"""

import asyncio
from datetime import datetime, timezone

import pytest

from ground_truth import MQTTGroundTruthProvider, Claim


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_mqtt_device_online_detection():
    """Test MQTT device online detection via telemetry."""
    pytest.importorskip("aiomqtt", reason="aiomqtt not installed")
    
    provider = MQTTGroundTruthProvider(
        broker_host="[REDACTED_MQTT_HOST]",
        broker_port=1883,
    )
    
    try:
        # Try to connect
        connected = await provider.connect()
        if not connected:
            pytest.skip("MQTT broker not available")
        
        # Wait for devices to send telemetry
        await asyncio.sleep(1)
        
        # Get available devices
        devices = await provider.available_devices()
        
        if not devices:
            pytest.skip("No active MQTT devices sending telemetry")
        
        # Check if first device is online
        device_id = devices[0]
        is_online = await provider._is_device_online(device_id)
        
        assert is_online == True, f"Device {device_id} should be online after telemetry"
    
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_mqtt_device_offline_after_timeout():
    """Test MQTT device offline detection after cache TTL expires."""
    pytest.importorskip("aiomqtt", reason="aiomqtt not installed")
    
    provider = MQTTGroundTruthProvider(
        broker_host="[REDACTED_MQTT_HOST]",
        broker_port=1883,
        cache_ttl_seconds=30,
    )
    
    try:
        # Connect and get initial devices
        connected = await provider.connect()
        if not connected:
            pytest.skip("MQTT broker not available")
        
        await asyncio.sleep(1)
        initial_devices = await provider.available_devices()
        
        if not initial_devices:
            pytest.skip("No active MQTT devices")
        
        device_id = initial_devices[0]
        
        # Manually expire the device by manipulating presence
        async with provider._cache_lock:
            # Set last_heartbeat to far past
            old_time = datetime.fromtimestamp(0, timezone.utc)
            provider._device_presence[device_id]["last_heartbeat"] = old_time
        
        # Device should now appear offline
        is_online = await provider._is_device_online(device_id)
        assert is_online == False, f"Device {device_id} should be offline after TTL expiry"
    
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_mqtt_topic_parsing():
    """Test MQTT topic parsing and device discovery."""
    pytest.importorskip("aiomqtt", reason="aiomqtt not installed")
    
    provider = MQTTGroundTruthProvider(
        broker_host="[REDACTED_MQTT_HOST]",
        broker_port=1883,
    )
    
    try:
        connected = await provider.connect()
        if not connected:
            pytest.skip("MQTT broker not available")
        
        # Wait for telemetry messages
        await asyncio.sleep(2)
        
        # Get available devices
        devices = await provider.available_devices()
        
        # Should have parsed at least one device from dama/{device}/telemetry topics
        assert isinstance(devices, list), "available_devices should return a list"
        
        # Each device ID should be valid (not empty, not containing slashes)
        for device_id in devices:
            assert isinstance(device_id, str), "Device ID should be string"
            assert len(device_id) > 0, "Device ID should not be empty"
            assert "/" not in device_id, "Device ID should not contain path separators"
    
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_mqtt_sensor_data_caching():
    """Test that MQTT sensor data is properly cached."""
    pytest.importorskip("aiomqtt", reason="aiomqtt not installed")
    
    provider = MQTTGroundTruthProvider(
        broker_host="[REDACTED_MQTT_HOST]",
        broker_port=1883,
    )
    
    try:
        connected = await provider.connect()
        if not connected:
            pytest.skip("MQTT broker not available")
        
        # Wait for telemetry
        await asyncio.sleep(2)
        
        # Check sensor cache
        assert len(provider._sensor_cache) > 0, "Should have cached sensor data"
        
        # Verify cache structure
        for device_id, sensors in provider._sensor_cache.items():
            assert isinstance(sensors, dict), f"Sensors for {device_id} should be dict"
            
            # Each sensor should have data, timestamp, freshness_ms
            for sensor_type, sensor_data in sensors.items():
                if sensor_type == "_raw":
                    continue
                assert "data" in sensor_data, f"{sensor_type} should have 'data'"
                assert "timestamp" in sensor_data, f"{sensor_type} should have 'timestamp'"
                assert "freshness_ms" in sensor_data, f"{sensor_type} should have 'freshness_ms'"
    
    finally:
        await provider.close()
