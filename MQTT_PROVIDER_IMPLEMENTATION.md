# MQTT Ground Truth Provider Implementation

## Overview

Implemented a complete `MQTTGroundTruthProvider` class that validates claims against **live DAMA phone telemetry** streamed via MQTT. This enables the deep_think_mcp engine to measure reasoning accuracy against real sensor measurements instead of speculative assumptions.

## Architecture

### Key Design Decisions (Per Rubber-Duck Critique)

1. **Pure Async/Await with aiomqtt** ✅
   - Switched from paho-mqtt (threading-based) to aiomqtt (pure async)
   - Eliminates race conditions on shared state
   - No thread synchronization needed

2. **Device Presence Tracking** ✅
   - Tracks last heartbeat from each device
   - Distinguishes "offline device" from "no recent sensor data"
   - Returns 0.0 confidence when device is offline

3. **Explicit Confidence Algorithms** ✅
   - Per-sensor-type confidence calculation
   - GPS: 0.9 base + staleness penalty
   - WiFi: 0.85 base + signal strength bonus + staleness penalty
   - Bluetooth: 0.80 base + staleness penalty
   - All bounded to [0.0, 1.0]

4. **Graceful Degradation** ✅
   - MQTT broker unavailable → returns 0.0 confidence, doesn't crash
   - Missing sensor data → returns specific error reasons in metadata
   - Malformed payloads → logged and skipped

5. **Thread-Safe Caching** ✅
   - Single `asyncio.Lock` protects sensor cache
   - No data corruption even with concurrent validation

## Implementation Details

### Class: `MQTTGroundTruthProvider`

**Connection Management**
```python
await provider.connect()  # Connects to broker, starts message loop
await provider.close()    # Graceful shutdown
```

**Sensor Data Access**
```python
# Get data from specific device
data = await provider.get_sensor_data("GPS.POSITION", device_id="pixel-9-pro-xl")

# Get data across all devices
data = await provider.get_sensor_data("WIFI.NEARBY_NETWORKS")

# Check which devices are active
devices = await provider.available_devices()

# Check available sensor domains
domains = await provider.available_domains()
```

**Claim Validation**
```python
claim = Claim(
    id="gps_001",
    subject="GPS.POSITION",
    statement="GPS is available",
    claim_type="gps_availability",
    expected_value={"available": True},
    confidence_model=0.8,
)

result = await provider.validate(claim)
# → ValidationResult(
#     is_valid: bool,
#     confidence: 0.0-1.0 (measured),
#     ground_truth_value: actual sensor data,
#     metadata: {provider, sensor_id, freshness_ms, device_id}
# )

# Batch validation
results = await provider.validate_batch(claims)
```

## Sensor Mapping

### GPS.POSITION
- **Source**: `payload["gps"]`
- **Validation**: `valid_fix` key must be True
- **Confidence**: 0.9 - staleness_penalty (age_ms / 10000, capped at 0.4)
- **Metadata**: freshness_ms, ground_truth_value includes full GPS struct

### WIFI.NEARBY_NETWORKS
- **Source**: `payload["wifi"]["networks"]` (list)
- **Validation**: List must be non-empty
- **Confidence**: 0.85 + signal_bonus (if best RSSI > -60dB) - staleness_penalty
- **Metadata**: freshness_ms, network count

### BT.NEARBY_DEVICES
- **Source**: `payload["bluetooth"]["devices"]` (list)
- **Validation**: List must be non-empty
- **Confidence**: 0.80 - staleness_penalty
- **Metadata**: freshness_ms, device count

## Configuration

```python
provider = MQTTGroundTruthProvider(
    broker_host="[REDACTED_MQTT_HOST]",  # Default
    broker_port=1883,                      # Default
    keepalive=30,                          # MQTT keepalive in seconds
    cache_ttl_seconds=30,                  # Device presence TTL
)
```

**Environment Variables** (Optional)
- `MQTT_BROKER_HOST`: Override broker host
- `MQTT_BROKER_PORT`: Override broker port
- `MQTT_CACHE_TTL_SECONDS`: Override cache TTL

## Message Format

MQTT topic: `dama/{device_id}/telemetry`

Expected JSON payload:
```json
{
  "device_id": "pixel-9-pro-xl",
  "timestamp": "2026-05-01T01:05:00Z",
  
  "gps": {
    "valid_fix": true,
    "latitude": 52.5,
    "longitude": 13.4,
    "accuracy_m": 12.5,
    "altitude_m": 45.0,
    "age_ms": 250
  },
  
  "wifi": {
    "networks": [
      {"ssid": "HomeNetwork", "rssi": -45, "channel": 6},
      {"ssid": "Guest", "rssi": -72, "channel": 11}
    ],
    "age_ms": 500
  },
  
  "bluetooth": {
    "devices": [
      {"name": "Pixel Watch", "rssi": -35},
      {"name": "Earbuds", "rssi": -55}
    ],
    "age_ms": 800
  }
}
```

## Error Handling

| Scenario | Result | Confidence | Reason |
|----------|--------|-----------|--------|
| Device offline (no heartbeat for 30s) | Invalid | 0.0 | device_not_found |
| Sensor not in payload | Invalid | 0.0 | no_sensor_data |
| GPS has valid_fix=false | Invalid | 0.0-0.3 | GPS confidence formula |
| WiFi networks list empty | Invalid | 0.0-0.3 | WiFi confidence formula |
| MQTT broker unreachable | N/A | 0.0 | broker_unavailable (caught at connect()) |
| Malformed JSON | Skipped | - | Logged as debug, not processed |

## Testing

Run the test suite with:
```bash
cd /home/USER/development/deep_think_mcp
python3 test_ground_truth.py
```

### Test Cases

1. **test_mqtt_connection()**: Verify MQTT connection, device discovery, domain availability
2. **test_mqtt_gps_validation()**: Validate GPS availability claim against live data
3. **test_mqtt_batch_validation()**: Validate GPS, WiFi, Bluetooth claims in batch

### Expected Test Output

```
TEST 1: MQTT Connection and Device Discovery
Creating MQTT provider...
Connecting to MQTT broker...
Waiting 5 seconds for telemetry...
Active devices: ['pixel-9-pro-xl', 'pixel-7-pro', ...]
Available sensor domains: ['gps', 'wifi', 'bluetooth', 'device_health']

TEST 2: MQTT GPS Claim Validation
Validating claim: GPS.POSITION is available
Validation result:
  is_valid: True
  confidence: 0.89  (0.9 - 0.01 staleness penalty)
  ground_truth_value: {'valid_fix': True, 'latitude': 52.5, ...}
  metadata: {'provider': 'mqtt', 'sensor_id': 'GPS.POSITION', 'freshness_ms': 250, 'device_id': 'pixel-9-pro-xl'}

TEST 3: MQTT Batch Claim Validation
Validating 3 claims via MQTT...
  gps_001: valid=True, confidence=0.89
  wifi_001: valid=True, confidence=0.87
  bt_001: valid=True, confidence=0.80
```

## Integration with engine.py

The engine can now use either provider:

```python
# Nova (Great Library) provider
ground_truth_provider = NovaGroundTruthProvider()

# OR MQTT (Real sensor data) provider
ground_truth_provider = MQTTGroundTruthProvider()
await ground_truth_provider.connect()

# Then pass to engine for validation
validation_result = await engine._validate_with_ground_truth(
    pass_text, claims, ground_truth_provider, context
)
```

## Future Enhancements

1. **Multi-device aggregation**: Average confidence across multiple devices for the same sensor
2. **Sensor fusion**: Cross-validate GPS against WiFi triangulation
3. **Temporal analysis**: Track sensor stability over time
4. **Confidence thresholds**: Configurable thresholds for claim acceptance
5. **Custom validation rules**: Allow users to define claim-to-sensor mappings
6. **Metrics export**: Prometheus-compatible metrics for validation accuracy

## Dependencies

- **aiomqtt** (>= 0.16.0): Pure async MQTT client
- **paho-mqtt** (no longer used directly): Was replaced by aiomqtt

Added to requirements.txt:
```
aiomqtt>=0.16.0
```

## Files Modified

1. **ground_truth.py**
   - Added `MQTTGroundTruthProvider` class (450+ lines)
   - Fixed `detect_contradictions()` to handle dict inputs
   - Kept `NovaGroundTruthProvider` as alternative implementation

2. **test_ground_truth.py**
   - Updated imports to include `MQTTGroundTruthProvider`
   - Refactored tests to use MQTT provider
   - Added MQTT-specific test scenarios

3. **requirements.txt**
   - Added `aiomqtt>=0.16.0`

## Known Limitations

1. **No TLS/Auth**: Current implementation assumes open MQTT broker (suitable for local/dev)
   - Can be extended with `tls_params` and `username`/`password` in aiomqtt.Client()

2. **In-Memory Cache**: Sensor data lost on provider shutdown
   - Could be extended with Redis/persistent storage

3. **No Query Replay**: Can't validate against historical data (only current state)
   - Time-range support is a TODO

4. **Single Device Context**: Validates one device at a time
   - Multi-device validation would require context parameter

## License & Attribution

Implemented per rubber-duck critique to address async/threading safety, device tracking, and confidence calculation. Architecture follows best practices for async MQTT clients and sensor data handling.
