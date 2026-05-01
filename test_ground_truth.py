#!/usr/bin/env python3
"""Test ground truth provider integration.

Run this to verify MQTT connection and sensor data flow.
"""

import asyncio
import logging
from ground_truth import NovaGroundTruthProvider, MQTTGroundTruthProvider, Claim, PassValidationResult

logging.basicConfig(
    level=logging.DEBUG,
    format="[%(levelname)s] %(name)s: %(message)s",
)

log = logging.getLogger(__name__)


async def test_mqtt_connection():
    """Test MQTT connection and telemetry caching."""
    log.info("Creating MQTT provider...")
    provider = MQTTGroundTruthProvider(
        broker_host="botnet.floppydicks.net",
        broker_port=1883,
    )

    log.info("Connecting to MQTT broker...")
    connected = await provider.connect()
    if not connected:
        log.error("Failed to connect to MQTT broker")
        return False

    # Wait for telemetry to arrive
    log.info("Waiting 5 seconds for telemetry...")
    await asyncio.sleep(5)

    # Check what devices have published
    devices = await provider.available_devices()
    log.info(f"Active devices: {devices}")

    # Get available domains
    domains = await provider.available_domains()
    log.info(f"Available sensor domains: {domains}")

    await provider.close()
    return True


async def test_mqtt_gps_validation():
    """Test validating a GPS availability claim."""
    log.info("Creating MQTT provider...")
    provider = MQTTGroundTruthProvider()

    log.info("Connecting to MQTT broker...")
    if not await provider.connect():
        log.error("Failed to connect")
        return False

    # Wait for telemetry
    log.info("Waiting 5 seconds for telemetry...")
    await asyncio.sleep(5)

    # Create a claim about GPS availability
    claim = Claim(
        id="gps_availability_001",
        statement="GPS.POSITION is available",
        claim_type="telemetry_availability",
        subject="GPS.POSITION",
        expected_value={"available": True},
        confidence_model=0.8,
    )

    log.info(f"Validating claim: {claim.statement}")
    result = await provider.validate(claim)
    log.info(f"Validation result:")
    log.info(f"  is_valid: {result.is_valid}")
    log.info(f"  confidence: {result.confidence}")
    log.info(f"  ground_truth_value: {result.ground_truth_value}")
    log.info(f"  metadata: {result.metadata}")

    await provider.close()
    return True


async def test_mqtt_batch_validation():
    """Test validating multiple claims via MQTT."""
    log.info("Creating MQTT provider...")
    provider = MQTTGroundTruthProvider()

    log.info("Connecting to MQTT broker...")
    if not await provider.connect():
        log.error("Failed to connect")
        return False

    await asyncio.sleep(5)

    # Create multiple claims
    claims = [
        Claim(
            id="gps_001",
            statement="GPS position is available",
            claim_type="gps_availability",
            subject="GPS.POSITION",
            expected_value={"available": True},
            confidence_model=0.7,
        ),
        Claim(
            id="wifi_001",
            statement="Wi-Fi networks are detected",
            claim_type="wifi_availability",
            subject="WIFI.NEARBY_NETWORKS",
            expected_value={"count": ">=1"},
            confidence_model=0.6,
        ),
        Claim(
            id="bt_001",
            statement="Bluetooth devices are detected",
            claim_type="bt_availability",
            subject="BT.NEARBY_DEVICES",
            expected_value={"count": ">=0"},
            confidence_model=0.5,
        ),
    ]

    log.info(f"Validating {len(claims)} claims via MQTT...")
    results = await provider.validate_batch(claims)

    for result in results:
        log.info(f"  {result.claim_id}: valid={result.is_valid}, confidence={result.confidence:.2f}")

    await provider.close()
    return True


async def main():
    """Run all tests."""
    print("\n" + "=" * 80)
    print("TEST 1: MQTT Connection and Device Discovery")
    print("=" * 80)
    if not await test_mqtt_connection():
        return

    print("\n" + "=" * 80)
    print("TEST 2: MQTT GPS Claim Validation")
    print("=" * 80)
    if not await test_mqtt_gps_validation():
        return

    print("\n" + "=" * 80)
    print("TEST 3: MQTT Batch Claim Validation")
    print("=" * 80)
    if not await test_mqtt_batch_validation():
        return

    print("\n" + "=" * 80)
    print("All MQTT tests passed!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
