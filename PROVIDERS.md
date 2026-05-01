# Ground Truth Provider API Reference

This document describes the provider APIs for validating claims against ground truth sources: Nova/Great Library and MQTT sensor telemetry.

## Table of Contents

1. [NovaGroundTruthProvider](#novagroundtruthprovider)
2. [MQTTGroundTruthProvider](#mqttgroundtruthprovider)
3. [Creating Custom Providers](#creating-custom-providers)
4. [Confidence Interpretation](#confidence-interpretation)
5. [Error Handling](#error-handling)

---

## NovaGroundTruthProvider

Validates claims against knowledge in the Great Library using semantic search and verification.

### Overview

```python
from ground_truth import NovaGroundTruthProvider, Claim, ValidationResult

# Initialize provider
provider = NovaGroundTruthProvider()

# Create a claim to validate
claim = Claim(
    id="gps_claim_1",
    statement="Device is in New York",
    claim_type="gps_position",
    subject="GPS.POSITION",
    expected_value={"latitude": 40.7128, "longitude": -74.0060},
    confidence_model=0.8
)

# Validate
result = await provider.validate(claim)
print(f"Is valid: {result.is_valid}")
print(f"Confidence: {result.confidence}")  # 0.0-1.0
```

### Methods

#### `validate(claim: Claim, context: Optional[Dict]) -> ValidationResult`

Validate a single claim against Great Library evidence.

**Parameters:**
- `claim` (Claim): Claim object with id, subject, expected_value
- `context` (Dict, optional): Additional context (prior_passes, task_class, etc.)

**Returns:** `ValidationResult` with:
- `is_valid` (bool): Whether claim is supported by evidence
- `ground_truth_value` (Any): Actual value from ground truth (if valid)
- `evidence` (List[Dict]): Supporting documents and passages
- `confidence` (float): 0.0-1.0 measured from evidence quality
- `metadata` (Dict): Provider status, latency, error details

**Behavior:**
- May retry up to 3 times on timeout using exponential backoff (1s, 2s, 4s)
- Returns `confidence=0.0` on persistent timeout
- Reduces confidence by 0.3 if contradictions found

**Example:**

```python
result = await provider.validate(claim)

if result.is_valid:
    print(f"Claim supported with confidence {result.confidence}")
    for evidence in result.evidence:
        print(f"  Source: {evidence['source']}")
else:
    print(f"Claim not supported")
    print(f"Latency: {result.metadata['latency_ms']}ms")
    if result.metadata.get('contradiction_count'):
        print(f"Contradictions: {result.metadata['contradiction_count']}")
```

#### `validate_batch(claims: List[Claim], context: Optional[Dict]) -> List[ValidationResult]`

Validate multiple claims in sequence.

**Parameters:**
- `claims` (List[Claim]): List of Claim objects
- `context` (Dict, optional): Additional context

**Returns:** List of `ValidationResult` objects (one per claim, in same order)

**Example:**

```python
claims = [
    Claim(id="c1", statement="...", claim_type="...", subject="GPS.POSITION", expected_value=...),
    Claim(id="c2", statement="...", claim_type="...", subject="WIFI.NETWORKS", expected_value=...),
]

results = await provider.validate_batch(claims)
valid_count = sum(1 for r in results if r.is_valid)
print(f"Validated {len(claims)} claims, {valid_count} supported by evidence")
```

#### `validate_multi_device(claims: List[Claim], device_ids: List[str]) -> ValidationResult`

Validate claims against sensor measurements from multiple DAMA phones.

**Parameters:**
- `claims` (List[Claim]): Claims to validate
- `device_ids` (List[str]): Device IDs to aggregate data from

**Returns:** Single `ValidationResult` with:
- `confidence`: Aggregated (minimum) confidence across devices
- `evidence`: Aggregated from all devices
- `metadata["device_confidences"]`: Per-device confidence scores

**Aggregation Method:** Uses minimum confidence across devices (conservative approach: if any device contradicts the claim, confidence is low)

**Example:**

```python
# Validate network claim against multiple phones
result = await provider.validate_multi_device(
    claims=[network_claim],
    device_ids=["phone_1", "phone_2", "phone_3"]
)

print(f"Confidence across devices: {result.confidence}")
print(f"Per-device scores: {result.metadata['device_confidences']}")
# Output: {'phone_1': 0.8, 'phone_2': 0.7, 'phone_3': 0.9}
# Aggregated: min = 0.7
```

#### `detect_contradictions(claims: List[Claim], prior_claims: Optional[List[Claim]]) -> List[Dict]`

Detect semantic contradictions between current and prior claims.

**Parameters:**
- `claims` (List[Claim]): Current pass claims
- `prior_claims` (List[Claim], optional): Claims from earlier passes

**Returns:** List of contradiction dicts with:
- `claim_1_id` (str): Prior claim ID
- `claim_2_id` (str): Current claim ID
- `subject` (str): What changed
- `contradiction` (str): Description
- `detection_method` (str): "nova_verify" or "heuristic"

**Example:**

```python
pass1_claims = [Claim(..., subject="BATTERY", expected_value=0.9)]
pass2_claims = [Claim(..., subject="BATTERY", expected_value=0.2)]

contradictions = await provider.detect_contradictions(pass2_claims, pass1_claims)
for c in contradictions:
    print(f"Semantic contradiction: {c['contradiction']}")
```

#### `available_domains() -> List[str]`

Return list of searchable domains in Great Library.

**Returns:** List of domain strings (e.g., ["gps", "wifi", "battery", "network"])

---

## MQTTGroundTruthProvider

Validates claims against live sensor telemetry from DAMA phones via MQTT.

### Overview

```python
from ground_truth import MQTTGroundTruthProvider, Claim

# Initialize provider
provider = MQTTGroundTruthProvider(
    broker_host="botnet.floppydicks.net",
    broker_port=1883,
    cache_ttl_seconds=30
)

# Connect to broker
if not await provider.connect():
    print("MQTT connection failed")
    return

# Validate claim against sensor data
claim = Claim(
    id="gps_claim",
    statement="GPS is locked",
    claim_type="gps_state",
    subject="GPS.POSITION",
    expected_value={"valid_fix": True}
)

result = await provider.validate(claim)
```

### Methods

#### `connect() -> bool`

Connect to MQTT broker and start background message loop.

**Returns:** `True` if connected, `False` on failure

**Side Effects:**
- Spawns background task `_message_loop()` to receive and cache messages
- Subscribes to topic `dama/+/telemetry`

**Example:**

```python
if not await provider.connect():
    log.error("MQTT broker unreachable")
    sys.exit(1)
```

#### `validate(claim: Claim, context: Optional[Dict]) -> ValidationResult`

Validate a claim against cached sensor data.

**Parameters:**
- `claim` (Claim): Claim with subject in ["GPS.POSITION", "WIFI.NEARBY_NETWORKS", "BT.NEARBY_DEVICES"]
- `context` (Dict, optional): Additional context

**Returns:** `ValidationResult`

**Behavior:**
- Searches sensor cache for matching device/sensor
- Returns `confidence=0.0` if sensor data missing or stale
- `confidence=0.0` if age_ms exceeds cache_ttl_seconds (default 30s)
- Validates payload schema (required fields, type checking)

**Example:**

```python
# GPS position claim
gps_claim = Claim(
    id="gps_1",
    statement="Device has GPS lock",
    claim_type="gps_state",
    subject="GPS.POSITION",
    expected_value={"valid_fix": True}
)

result = await provider.validate(gps_claim)
if result.is_valid:
    print(f"GPS data fresh: {result.ground_truth_value}")
else:
    print(f"No fresh GPS data (confidence: {result.confidence})")
```

#### `validate_batch(claims: List[Claim], context: Optional[Dict]) -> List[ValidationResult]`

Validate multiple claims in sequence against sensor cache.

**Parameters:**
- `claims` (List[Claim]): Claims to validate
- `context` (Dict, optional): Additional context

**Returns:** List of `ValidationResult` objects

**Example:**

```python
sensor_claims = [
    Claim(id="gps", subject="GPS.POSITION", claim_type="...", expected_value=...),
    Claim(id="wifi", subject="WIFI.NEARBY_NETWORKS", claim_type="...", expected_value=...),
    Claim(id="bt", subject="BT.NEARBY_DEVICES", claim_type="...", expected_value=...),
]

results = await provider.validate_batch(sensor_claims)
```

#### `available_devices() -> List[str]`

Return list of devices that have published telemetry within cache TTL.

**Returns:** List of device IDs (e.g., ["phone_001", "phone_002"])

**Example:**

```python
active_devices = await provider.available_devices()
print(f"Devices with recent telemetry: {active_devices}")
```

#### `available_domains() -> List[str]`

Return list of available sensor domains.

**Returns:** `["gps", "wifi", "bluetooth", "device_health"]`

#### `get_sensor_data(sensor_id: str, device_id: Optional[str], time_range: Optional[tuple]) -> Dict`

Fetch raw sensor data from cache.

**Parameters:**
- `sensor_id` (str): e.g., "GPS.POSITION", "WIFI.NEARBY_NETWORKS", "BT.NEARBY_DEVICES"
- `device_id` (str, optional): Specific device (if None, returns from all devices)
- `time_range` (tuple, optional): (start, end) datetime for filtering historical data

**Returns:** Dict with:
- `sensor_id` (str): The requested sensor
- `status` (str): "OK", "NO_DATA", "STALE", "TIMEOUT", "ERROR"
- `current_value` (Any): Latest sensor reading
- `freshness_ms` (int): Age of data
- `device_id` (str): Which device (if querying single device)
- `recent_values` (List, optional): Historical data (if time_range provided)

**Example:**

```python
# Get latest GPS position from phone_001
gps = await provider.get_sensor_data("GPS.POSITION", device_id="phone_001")
if gps["status"] == "OK":
    print(f"GPS position: {gps['current_value']}")
    print(f"Data age: {gps['freshness_ms']}ms")
else:
    print(f"GPS unavailable: {gps['status']}")
```

#### `close()`

Disconnect from MQTT broker.

**Example:**

```python
await provider.close()
```

### Message Format

MQTT messages must be published to `dama/{device_id}/telemetry` with JSON payload.

**Required Fields:**
- `device_id` (str): Device identifier
- `timestamp` (int): Unix timestamp in milliseconds

**Optional Sections:**

**GPS Section:**
```json
{
  "gps": {
    "valid_fix": true,
    "latitude": 40.7128,
    "longitude": -74.0060,
    "altitude_m": 10.5,
    "accuracy_m": 5.0,
    "age_ms": 1000
  }
}
```

Required fields if section present: `valid_fix`, `latitude`, `longitude`, `age_ms`

**WiFi Section:**
```json
{
  "wifi": {
    "networks": [
      {
        "ssid": "Network1",
        "rssi_dbm": -45,
        "frequency_mhz": 2437
      }
    ],
    "age_ms": 500
  }
}
```

Required fields if section present: `networks`, `age_ms`

**Bluetooth Section:**
```json
{
  "bluetooth": {
    "devices": [
      {
        "address": "AA:BB:CC:DD:EE:FF",
        "rssi_dbm": -60,
        "name": "Device Name"
      }
    ],
    "age_ms": 800
  }
}
```

Required fields if section present: `devices`, `age_ms`

**Full Example:**

```json
{
  "device_id": "phone_001",
  "timestamp": 1699564800000,
  "gps": {
    "valid_fix": true,
    "latitude": 40.7128,
    "longitude": -74.0060,
    "altitude_m": 10.5,
    "accuracy_m": 5.0,
    "age_ms": 1000
  },
  "wifi": {
    "networks": [
      {"ssid": "Home", "rssi_dbm": -45, "frequency_mhz": 2437}
    ],
    "age_ms": 500
  },
  "bluetooth": {
    "devices": [
      {"address": "AA:BB:CC:DD:EE:FF", "rssi_dbm": -60, "name": "Watch"}
    ],
    "age_ms": 800
  }
}
```

### Device Presence Tracking

- Devices are considered "present" if they published within `cache_ttl_seconds` (default 30s)
- After TTL expires, device is removed from `available_devices()`
- Last heartbeat timestamp tracked in `_device_presence`

### Sensor Data Freshness

- `freshness_ms` is the `age_ms` value from the sensor section
- Data is considered "stale" if `age_ms > cache_ttl_seconds`
- Validation returns `confidence=0.0` for stale data
- Negative `age_ms` values are treated as stale (logged as warning)

### Validation Edge Cases Handled

1. **Malformed Payload**: Missing required fields → logged warning, message rejected
2. **Empty Device ID**: From malformed topic → logged warning, message skipped
3. **Negative age_ms**: Indicates sensor error → logged warning, treated as stale (999999ms)
4. **Invalid cache_ttl_seconds**: ≤ 0 → logged error, reset to default 30s
5. **None values in payload**: Section is None → logged warning, section skipped
6. **Non-dict sections**: GPS/WiFi/BT not objects → logged warning, section skipped
7. **Missing section keys**: Required key absent → logged warning, section skipped

---

## Creating Custom Providers

Implement the `GroundTruthProvider` protocol to create custom ground truth sources.

### Protocol Definition

```python
from typing import Protocol, List, Dict, Any, Optional
from ground_truth import Claim, ValidationResult

class GroundTruthProvider(Protocol):
    """Protocol for ground truth providers."""
    
    async def validate(
        self,
        claim: Claim,
        context: Optional[Dict[str, Any]] = None,
    ) -> ValidationResult:
        """Validate a claim against ground truth."""
        ...
    
    async def validate_batch(
        self,
        claims: List[Claim],
        context: Optional[Dict[str, Any]] = None,
    ) -> List[ValidationResult]:
        """Validate multiple claims."""
        ...
    
    async def available_domains(self) -> List[str]:
        """Return list of domains this provider covers."""
        ...
```

### Example: Custom Database Provider

```python
import sqlite3
from ground_truth import GroundTruthProvider, Claim, ValidationResult

class DatabaseProvider:
    """Validate claims against SQLite database."""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
    
    async def validate(
        self,
        claim: Claim,
        context: Optional[Dict[str, Any]] = None,
    ) -> ValidationResult:
        """Query database for claim."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Query database based on claim subject
            cursor.execute(
                "SELECT value, timestamp FROM facts WHERE subject = ?",
                (claim.subject,)
            )
            row = cursor.fetchone()
            conn.close()
            
            if not row:
                return ValidationResult(
                    claim_id=claim.id,
                    is_valid=False,
                    ground_truth_value=None,
                    evidence=[],
                    confidence=0.0,
                    metadata={"provider": "database", "status": "no_data"}
                )
            
            value, timestamp = row
            is_valid = value == claim.expected_value
            
            return ValidationResult(
                claim_id=claim.id,
                is_valid=is_valid,
                ground_truth_value=value,
                evidence=[{"source": "database", "timestamp": timestamp}],
                confidence=1.0 if is_valid else 0.0,
                metadata={"provider": "database", "status": "verified"}
            )
        
        except Exception as e:
            return ValidationResult(
                claim_id=claim.id,
                is_valid=False,
                ground_truth_value=None,
                evidence=[],
                confidence=0.0,
                metadata={"provider": "database", "error": str(e)}
            )
    
    async def validate_batch(
        self,
        claims: List[Claim],
        context: Optional[Dict[str, Any]] = None,
    ) -> List[ValidationResult]:
        """Validate multiple claims."""
        results = []
        for claim in claims:
            result = await self.validate(claim, context)
            results.append(result)
        return results
    
    async def available_domains(self) -> List[str]:
        """Return available domains."""
        return ["database"]
```

### Integration with Orchestrator

```python
from engine.orchestrator import Orchestrator

# Initialize providers
nova_provider = NovaGroundTruthProvider()
mqtt_provider = MQTTGroundTruthProvider()
db_provider = DatabaseProvider("/path/to/facts.db")

# Connect
await mqtt_provider.connect()

# Create orchestrator with multiple providers
orchestrator = Orchestrator(
    ground_truth_providers={
        "nova": nova_provider,
        "mqtt": mqtt_provider,
        "database": db_provider,
    }
)

# Validate claims against all providers
results = await orchestrator.validate_claims(claims)
```

---

## Confidence Interpretation

All providers return confidence scores in range [0.0, 1.0]:

| Range | Interpretation | Action |
|-------|-----------------|--------|
| 0.0 - 0.3 | Low confidence | Claim likely unsupported or contradicted |
| 0.3 - 0.7 | Medium confidence | Partial support; may need additional validation |
| 0.7 - 1.0 | High confidence | Strong evidence supports claim |

**Nova Provider:**
- 0.7-1.0: Evidence found, claim grounded in Great Library
- 0.4-0.7: Some contradictions found (reduced by 0.3)
- 0.0: Nova service timeout, unavailable, or claim contradicted

**MQTT Provider:**
- 1.0: Sensor data matches claim exactly and is fresh
- 0.5-0.9: Partial match or slightly stale data
- 0.0: No sensor data, stale (>TTL), or validation failed

---

## Error Handling

### Nova Timeouts

Nova provider automatically retries on timeout:

```
Attempt 1 timeout → wait 1s → retry
Attempt 2 timeout → wait 2s → retry
Attempt 3 timeout → wait 4s → retry
All retries exhausted → return confidence=0.0
```

### MQTT Connection Failures

MQTT provider handles connection failures:

```python
if not await provider.connect():
    # Handle offline mode
    log.warning("MQTT broker unreachable, running in offline mode")
```

### Validation Errors

Both providers catch exceptions and return safe defaults:

```python
result = ValidationResult(
    claim_id=claim.id,
    is_valid=False,
    ground_truth_value=None,
    evidence=[],
    confidence=0.0,
    metadata={"provider": "...", "error": str(e)}
)
```

---

## Best Practices

1. **Check confidence, not just is_valid**: A claim may be syntactically valid but have low confidence
2. **Use context**: Pass prior_passes context for contradiction detection
3. **Batch operations**: Use `validate_batch()` instead of repeated `validate()` calls
4. **Monitor latency**: Check `metadata["latency_ms"]` for timeout patterns
5. **Device-specific validation**: Use `validate_multi_device()` for claims that should be consistent across devices
6. **Cache management**: MQTT provider caches for TTL; clear if needed for testing
