# Test Strategy for Ground Truth Providers

This document describes the testing approach for the ground truth validation system, which validates claims against real sensor data via MQTT and the Nova/Great Library.

## Overview

The ground truth provider system has two main implementations:
- **MQTTGroundTruthProvider**: Validates claims against real-time MQTT telemetry from DAMA phones
- **NovaGroundTruthProvider**: Validates claims against the Great Library (Nova knowledge base)

Testing is split into **unit tests** (using mocks, no network) and **integration tests** (requiring live services).

---

## Unit Tests (No Network Required)

Unit tests use mock providers that pre-populate cache with test data. They run quickly and don't depend on external services.

### Running Unit Tests

```bash
# Run all unit tests
pytest test_ground_truth.py -v -m "not integration"

# Run a specific test
pytest test_ground_truth.py::test_mqtt_connection -v
```

### Test Files

- **test_ground_truth.py** - Core unit tests
  - `test_mqtt_connection()` - Verify mock provider is connected and lists devices
  - `test_mqtt_gps_validation()` - Validate GPS claim with mock data
  - `test_mqtt_batch_validation()` - Validate multiple claims in batch
  - `test_nova_unavailable()` - Handle Nova service unavailability
  - `test_mqtt_timeout()` - Handle offline device timeout
  - `test_invalid_claim_format()` - Handle invalid claim formats gracefully
  
#### New Enhanced Tests (Added in Latest Coverage Expansion)

- `test_concurrent_validation()` - Test race conditions with concurrent validate() and validate_batch() calls
- `test_mqtt_offline_device()` - Test available_devices() detects offline devices after heartbeat timeout
- `test_nova_timeout_with_retry()` - Test Nova timeout handling with exponential backoff retry
- `test_detect_contradictions_gps()` - Test semantic contradiction detection for GPS coordinates
- `test_detect_contradictions_temperature()` - Test contradiction detection for numeric values (>20% difference)
- `test_staleness_penalty_gps()` - Verify confidence penalty for stale data (age_ms): max -0.4 for GPS
- `test_malformed_payload_missing_fields()` - Verify graceful handling of payloads missing required fields
- `test_malformed_payload_wrong_types()` - Verify type validation and error handling
- `test_malformed_payload_none_values()` - Test payload processing with None values
- `test_database_persistence()` - Verify cache persists to SQLite DB and loads on provider restart
- `test_multi_sensor_batch_validation()` - Test validate_batch() with GPS, WiFi, Battery in same call

### Unit Test Fixtures

Defined in `conftest.py`:

```python
@pytest.fixture
async def mock_mqtt_provider():
    """Provides MockMQTTProvider with pre-populated test data."""
    provider = MockMQTTProvider()
    await provider.connect()
    yield provider
    await provider.close()

@pytest.fixture
async def mock_nova_provider():
    """Provides MockNovaProvider with test data."""
    provider = MockNovaProvider()
    yield provider
```

### Mock Data Structure

**Device: pixel-9-pro-xl**
- GPS.POSITION: `{valid_fix: true, latitude: 52.5, longitude: 13.4, accuracy_m: 5.0}`
- WIFI.NEARBY_NETWORKS: `{networks: [{ssid: HomeNet, rssi: -45}, ...], nearby_count: 3}`

**Device: test-device**
- BATTERY.LEVEL: `{battery_pct: 75}`
- CPU.USAGE: `{cpu_usage: 45.5}`

---

## Integration Tests (Requires Live MQTT Broker)

Integration tests use real MQTT connections to validate provider behavior against live telemetry. These are marked with `@pytest.mark.integration` and can be skipped.

### Running Integration Tests

```bash
# Run only integration tests
pytest tests/test_mqtt_integration.py -v -m "integration"

# Run all tests (unit + integration)
pytest -v

# Run unit tests only, skip integration
pytest -v -m "not integration"
```

### Test Files

- **tests/test_mqtt_integration.py** - MQTT integration tests
  - `test_mqtt_device_online_detection()` - Detect device after telemetry
  - `test_mqtt_device_offline_after_timeout()` - Detect device offline after TTL
  - `test_mqtt_topic_parsing()` - Parse topics and discover devices
  - `test_mqtt_sensor_data_caching()` - Verify cache structure

### Prerequisites for Integration Tests

```bash
# MQTT broker must be accessible
# Default: [REDACTED_MQTT_HOST]:1883

# Devices must be sending telemetry
# Topics: dama/{device_id}/telemetry

# Install aiomqtt
pip install aiomqtt
```

### Integration Test Behavior

- **Auto-skip**: Tests skip automatically if MQTT broker is unavailable
- **No pre-existing data required**: Tests wait for telemetry to arrive
- **Timeout handling**: Tests set 35+ second wait for offline detection

---

## Nova/Great Library Tests

Tests for Nova provider require the nova_tools module and Great Library access.

### Mock Nova Testing (Unit)

Unit tests use `mock_nova_provider` fixture that returns deterministic results:

```python
@pytest.mark.asyncio
async def test_nova_gps_claim(mock_nova_provider):
    claim = Claim(
        id="gps_001",
        statement="GPS is available",
        subject="GPS.POSITION",
        expected_value={"valid_fix": True},
    )
    
    result = await mock_nova_provider.validate(claim)
    assert result.is_valid == True
    assert result.confidence > 0.8
```

### Real Nova Testing (Requires nova_tools)

If nova_tools is available and Great Library is accessible:

```bash
# Tests that import nova_verify, nova_search will use real implementation
pytest test_nova_integration.py -v
```

---

## Test Markers

Tests are marked for selective execution:

```python
@pytest.mark.asyncio        # Async test
@pytest.mark.integration    # Requires external service (MQTT, Nova)
@pytest.mark.unit           # Unit test (fast, no network)
```

### Run by Marker

```bash
pytest -m "asyncio and unit"          # Unit tests only
pytest -m "asyncio and integration"   # Integration tests only
pytest -m "integration and mqtt"      # MQTT integration tests
```

---

## Claim Validation Flow

### 1. GPS.POSITION Claims

```python
claim = Claim(
    subject="GPS.POSITION",
    expected_value={"valid_fix": True},  # or {"valid_fix": False}
)

result = await provider.validate(claim)
# result.is_valid: True if GPS has valid fix
# result.confidence: 0.9 (fresh fix) to 0.3 (stale)
```

### 2. WIFI.NEARBY_NETWORKS Claims

```python
claim = Claim(
    subject="WIFI.NEARBY_NETWORKS",
    expected_value={"nearby_count": 3},  # or integer
)

result = await provider.validate(claim)
# result.is_valid: True if count matches (±2 tolerance)
# result.confidence: 0.85 (detected) to 0.0 (no networks)
```

### 3. Sensor Level Claims (Battery, CPU, etc.)

```python
claim = Claim(
    subject="BATTERY.LEVEL",
    expected_value=75,  # percentage
)

result = await provider.validate(claim)
# result.is_valid: True if within ±10% tolerance
# result.confidence: Linear decay from 1.0 based on difference
```

---

## Adding New Tests

### 1. Unit Test Template

```python
@pytest.mark.asyncio
async def test_my_feature(mock_mqtt_provider):
    """Test description."""
    provider = mock_mqtt_provider
    
    claim = Claim(...)
    result = await provider.validate(claim)
    
    assert result.is_valid == True
    assert isinstance(result, ValidationResult)
```

### 2. Integration Test Template

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_my_mqtt_feature():
    """Test description."""
    pytest.importorskip("aiomqtt", reason="aiomqtt not installed")
    
    provider = MQTTGroundTruthProvider()
    try:
        connected = await provider.connect()
        if not connected:
            pytest.skip("MQTT broker not available")
        
        # Test code here
        
    finally:
        await provider.close()
```

### 3. Adding Mock Data

Edit `conftest.py`, `MockMQTTProvider._populate_test_data()`:

```python
self._sensor_cache["device-id"]["SENSOR.TYPE"] = {
    "data": {
        "field1": value1,
        "field2": value2,
    },
    "timestamp": now,
    "freshness_ms": 100,
}
```

---

## Stress Tests (Optional Performance Testing)

Stress tests simulate high-load scenarios and are marked with `@pytest.mark.stress` for optional execution.

### Running Stress Tests

```bash
# Run all stress tests
pytest tests/test_stress.py -v -m stress

# Run a specific stress test
pytest tests/test_stress.py::test_1000_claims_validation -v -m stress

# Run without stress tests (default)
pytest -v -m "not stress"
```

### Stress Test Scenarios

- **test_1000_claims_validation()** - Throughput test validating 1000 claims
  - Measures: Time, successful validations, throughput (claims/second)
  - Expected: <100ms per claim on modern hardware

- **test_concurrent_validations_100()** - Concurrency test with 100 concurrent validate() calls
  - Measures: Throughput, lock contention impact
  - Expected: No deadlocks, linear scaling with thread pool

- **test_rapid_device_updates()** - Simulate 10 devices sending 100 updates each (1000 total)
  - Measures: Cache write performance, device presence tracking
  - Expected: Millisecond-scale update processing

- **test_cache_contention()** - Readers and writers competing for cache lock
  - Measures: Lock fairness, serialization impact
  - Expected: Stable performance under contention

### Running Stress Tests in CI

```bash
# Skip stress tests (default for CI)
pytest -v -m "not stress"

# Optional: Run stress tests with longer timeout
pytest tests/test_stress.py -v -m stress --timeout=300
```

---

## Integration Test Requirements

### What Integration Tests Require

| Service | Required | Purpose | Location |
|---------|----------|---------|----------|
| MQTT Broker | Optional | Validate real MQTT connections | `[REDACTED_MQTT_HOST]:1883` |
| Nova/Great Library | Optional | Test real evidence validation | `http://[REDACTED_INTERNAL_IP]:30850` |
| SQLite | Built-in | Cache persistence tests | `~/.deep_think/mqtt_cache.db` |
| Network | Optional | Integration test runs | Firewall rules needed |

### Running with Missing Services

By default, integration tests are **skipped** if services are unavailable:

```bash
# This automatically skips tests if MQTT is down
pytest tests/test_mqtt_integration.py -v -m integration

# Force integration tests (will fail if services down)
pytest tests/test_mqtt_integration.py -v -m integration --strict-markers
```

### Integration Tests Can Require External Services

- **MQTT Broker**: If not available, tests using `@pytest.mark.integration` skip automatically
- **Nova**: If `nova_tools` not installed or endpoint unreachable, tests fall back to mock
- **Network**: Ensure firewall allows connections to external services

### Recommended CI Configuration

```yaml
# GitHub Actions Example
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Install dependencies
        run: pip install -r requirements.txt
      
      - name: Run unit tests (required)
        run: pytest -v -m "not integration" test_ground_truth.py
      
      - name: Run stress tests (optional)
        run: pytest -v -m "not stress" tests/test_stress.py
        continue-on-error: true
      
      - name: Run integration tests (optional)
        run: pytest -v -m integration tests/test_mqtt_integration.py
        continue-on-error: true  # Don't fail CI if services unavailable
```

### Test Markers for CI

- `@pytest.mark.unit` - No dependencies, always run
- `@pytest.mark.asyncio` - Async test, requires pytest-asyncio
- `@pytest.mark.integration` - Requires MQTT broker, skip if unavailable
- `@pytest.mark.stress` - Performance test, optional, may be slow
- `@pytest.mark.mqtt` - MQTT-specific tests, skip if broker down
- `@pytest.mark.nova` - Nova/Great Library tests, skip if unavailable

---

### GitHub Actions

The test suite is designed for CI/CD:

```yaml
# .github/workflows/test.yml
- name: Run unit tests
  run: pytest -m "not integration" -v

- name: Run integration tests (optional)
  run: pytest -m "integration" -v
  continue-on-error: true  # Fails gracefully if broker unavailable
```

### Coverage

```bash
# Generate coverage report
pytest --cov=ground_truth --cov-report=html test_ground_truth.py

# View report
open htmlcov/index.html
```

---

## Troubleshooting

### Issue: "aiomqtt not installed"
```bash
pip install aiomqtt
```

### Issue: "MQTT broker not available"
- Check broker host/port in MQTTGroundTruthProvider
- Default: `[REDACTED_MQTT_HOST]:1883`
- Tests auto-skip if unavailable

### Issue: "nova_tools not available"
- nova_tools is optional; tests use mock provider
- To use real Nova: ensure nova_tools is installed and Great Library is accessible
- See: `ground_truth.py` lines 19-24

### Issue: Tests are slow
- Run unit tests only: `pytest -m "not integration"`
- Run specific test: `pytest test_ground_truth.py::test_name -v`

---

## See Also

- `ground_truth.py` - Provider implementations
- `test_ground_truth.py` - Unit tests
- `tests/test_mqtt_integration.py` - Integration tests
- `conftest.py` - Fixtures and mock providers
