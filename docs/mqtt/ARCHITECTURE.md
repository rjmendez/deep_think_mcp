# MQTT Architecture & Design

## System Overview

The MQTT integration provides a complete pipeline for:
1. Subscribing to telemetry claims from MQTT brokers
2. Batching and processing claims through deep_think reasoning
3. Publishing findings (anomalies, contradictions) back to MQTT
4. Monitoring health and resilience

## Components

### Subscriber (mqtt/subscriber.py)

**Responsibilities:**
- Connects DAMAColonySubscriber to async deep_think loop
- Batches claims (by size or timeout)
- Passes batches to deep_think_passes()
- Handles errors gracefully

**Key Classes:**
- `MQTTClaimsProcessor`: Main batch processor
- `MQTTConfig`: Configuration management

**Lifecycle:**
- `mqtt_startup()`: Initialize subscriber and processor
- `mqtt_shutdown()`: Graceful shutdown, flush pending batches
- `setup_signal_handlers()`: SIGTERM/SIGINT handling

### Publisher (mqtt/publisher.py)

**Responsibilities:**
- Publishes findings to dama/colony/findings/{device_id}
- Batches findings (by size or timeout)
- Retries with exponential backoff
- Persists failed publishes to SQLite
- Auto-recovers on reconnect

**Key Classes:**
- `FindingsBatchPublisher`: Main publisher
- `FindingsPersistenceStore`: SQLite persistence
- `Finding`: Finding dataclass

**Features:**
- QoS=1 publishing
- Automatic persistence during outages
- Replay on reconnect
- Confirmation subscription handling

### Resilience (mqtt/resilience.py)

**Patterns:**
- **Circuit Breaker**: Pause on >50% consecutive failures, test recovery
- **Health Monitoring**: Track subscriber/publisher health
- **Metrics**: Prometheus-style metrics collection
- **Heartbeat**: Periodic health updates to MQTT

**Key Classes:**
- `CircuitBreaker`: State machine (CLOSED → OPEN → HALF_OPEN)
- `MQTTHealthMonitor`: Health tracking for both subscriber and publisher
- `MetricsSnapshot`: Metrics collection
- `HeartbeatPublisher`: Periodic heartbeat task

### Configuration (mqtt/config.py)

**Environment Variables:**
```
MQTT_ENABLE              Enable/disable MQTT (default: false)
MQTT_HOST                Broker hostname (default: [REDACTED_MQTT_HOST])
MQTT_PORT                Broker port (default: 1883)
MQTT_USERNAME            Auth username (default: dama)
MQTT_PASSWORD            Auth password
MQTT_USE_TLS             Enable TLS/SSL (default: false)
MQTT_SUBSCRIBER_QUEUE_SIZE  Max claims queue size (default: 1000)
MQTT_BATCH_SIZE          Claims per batch (default: 10)
MQTT_BATCH_TIMEOUT_MS    Batch timeout (default: 5000)
MQTT_FINDINGS_TOPIC      Topic template (default: dama/colony/findings/{device_id})
```

### Models (mqtt/models.py)

**Finding Dataclass:**
```python
@dataclass
class Finding:
    device_id: str
    claim_ids: List[str]
    anomalies: List[str]
    confidence: float
    severity: str  # 'low', 'medium', 'high', 'critical'
    timestamp: str  # ISO 8601
    metadata: Dict[str, Any]
```

### Utils (mqtt/utils.py)

**Functions:**
- `retry_with_backoff()`: Async retry with exponential backoff
- `parse_device_id_from_topic()`: Extract device ID from MQTT topic

## Data Flows

### Claims → Reasoning → Findings

```
1. MQTT Broker publishes telemetry to dama/colony/device_*/telemetry
2. DAMAColonySubscriber deserializes into Claim objects
3. MQTTClaimsProcessor batches claims (10 claims or 5 seconds)
4. deep_think_passes(claims) processes through reasoning engine
5. Findings extracted from reasoning result
6. FindingsBatchPublisher publishes to dama/colony/findings/{device_id}
```

### Persistence on Outage

```
1. Publisher tries to publish findings
2. If broker is down, persist to SQLite (mqtt_failures.db)
3. Retry with backoff
4. On successful reconnect, replay persisted findings
5. Delete from SQLite after confirmed publish
```

### Resilience Lifecycle

```
Normal (CLOSED)
    ↓ (>50% consecutive failures)
Failing (OPEN) → reject requests fast
    ↓ (after cooldown, test recovery)
Testing (HALF_OPEN) → allow 1 request
    ↓ (success)
Back to Normal (CLOSED)
    ↓ (failure)
Back to Failing (OPEN)
```

## Testing

All modules have comprehensive tests in `mqtt/tests/`:

- `test_subscriber.py`: Subscriber/processor tests
- `test_publisher.py`: Publisher/persistence tests
- `test_resilience.py`: Circuit breaker, health monitoring tests
- `test_integration.py`: End-to-end integration tests
- `conftest.py`: Shared fixtures (MockMQTTProvider, MockNovaProvider, sample claims)

**Run tests:**
```bash
pytest mqtt/tests/ -v
pytest mqtt/tests/ -m integration  # Real broker tests
```

## Error Handling

**Subscriber errors:**
- Broker connection failure → retry with backoff, log error
- Claim processing failure → skip claim, log error, continue

**Publisher errors:**
- Broker connection failure → persist to SQLite, retry on reconnect
- Finding publishing failure → backoff and retry, circuit break if threshold exceeded

**Circuit breaker:**
- Tracks consecutive failures
- Opens if >50% failure rate
- Switches to HALF_OPEN after cooldown
- Fully recovers after successful HALF_OPEN test

## Performance

- **Latency**: End-to-end <1 second for typical claims
- **Throughput**: Supports 100+ devices, 1000s of claims/min
- **Memory**: O(batch_size) for processing, SQLite for persistent queue
- **Network**: Async I/O, no blocking operations

## Security

- **Credentials**: Loaded from environment variables (not hardcoded)
- **TLS/SSL**: Supported via MQTT_USE_TLS flag
- **Local-only**: Deep_think reasoning uses only local Ollama models
- **Isolation**: Runs in dedicated async tasks, separate from main engine
