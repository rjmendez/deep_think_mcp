# MQTT Module API Reference

## Configuration (mqtt.config)

### MQTTConfig

Configuration class loaded from environment variables.

**Constructor:**
```python
config = MQTTConfig()
```

**Attributes:**
- `enabled: bool` - Whether MQTT is enabled (MQTT_ENABLE)
- `broker_host: str` - MQTT broker hostname (MQTT_HOST)
- `broker_port: int` - MQTT broker port (MQTT_PORT)
- `broker_user: str` - Username for authentication (MQTT_USERNAME)
- `broker_password: str` - Password for authentication (MQTT_PASSWORD)
- `use_tls: bool` - Whether to use TLS/SSL (MQTT_USE_TLS)
- `queue_size: int` - Max size of claims queue (MQTT_SUBSCRIBER_QUEUE_SIZE)
- `batch_size: int` - Claims per batch (MQTT_BATCH_SIZE)
- `batch_timeout_ms: int` - Batch timeout in milliseconds (MQTT_BATCH_TIMEOUT_MS)
- `batch_timeout_sec: float` - Batch timeout in seconds (derived)
- `findings_topic_template: str` - Topic template for publishing (MQTT_FINDINGS_TOPIC)

**Methods:**
- `validate() -> Optional[str]` - Validate config, return error message or None

## Models (mqtt.models)

### Finding

Dataclass representing a finding from reasoning.

**Attributes:**
```python
@dataclass
class Finding:
    device_id: str              # Device identifier
    claim_ids: List[str]        # Claim IDs supporting this finding
    anomalies: List[str]        # Anomaly descriptions
    confidence: float           # Confidence score (0.0-1.0)
    severity: str              # 'low', 'medium', 'high', 'critical'
    timestamp: str             # ISO 8601 timestamp
    metadata: Dict[str, Any]   # Additional context
```

**Methods:**
- `to_dict() -> Dict[str, Any]` - Convert to dictionary
- `from_dict(data) -> Finding` - Create from dictionary

## Subscriber (mqtt.subscriber)

### MQTTClaimsProcessor

Main batch processor for MQTT claims.

**Constructor:**
```python
processor = MQTTClaimsProcessor(config, subscriber)
```

**Methods:**
- `async start()` - Start the processor task
- `async stop()` - Stop the processor gracefully
- `async get_stats()` - Get processing statistics
- `get_processor_status()` - Get current status

**Lifecycle Functions:**
```python
# Initialize MQTT integration
await mqtt_startup()

# Setup signal handlers (SIGTERM, SIGINT)
setup_signal_handlers()

# Cleanup MQTT integration
await mqtt_shutdown()

# Get processor instance
processor = get_mqtt_processor()

# Get subscriber instance
subscriber = get_mqtt_subscriber()

# Check if MQTT is enabled
enabled = is_mqtt_enabled()
```

## Publisher (mqtt.publisher)

### MQTTFindingsPublisher

Publishes findings to MQTT with batching and persistence.

**Constructor:**
```python
publisher = MQTTFindingsPublisher(
    mqtt_host="broker.example",
    mqtt_port=1883,
    mqtt_username="dama",
    mqtt_password="secret",
)
```

**Methods:**
- `async start()` - Start publisher task
- `async stop()` - Stop gracefully
- `async publish_finding(finding)` - Publish single finding
- `async publish_findings(findings)` - Publish batch of findings
- `async get_stats()` - Get publisher statistics

### FindingsPersistenceStore

SQLite persistence for findings during outages.

**Constructor:**
```python
store = FindingsPersistenceStore(db_path="~/.deep_think/findings_queue.db")
```

**Methods:**
- `save_finding(finding)` - Persist finding to database
- `load_pending_findings(limit=100) -> List[(row_id, Finding)]` - Load unpublished findings
- `mark_finding_published(row_id)` - Mark as successfully published
- `get_stats()` - Get persistence statistics

**Utility Functions:**
```python
# Extract findings from deep_think result
findings = findings_from_deep_think_result(result)

# Load config from environment
config = load_config_from_env()
```

## Resilience (mqtt.resilience)

### CircuitBreakerState

Enum for circuit breaker states.

```python
class CircuitBreakerState(Enum):
    CLOSED = "CLOSED"           # Normal operation
    OPEN = "OPEN"               # Failing, reject requests
    HALF_OPEN = "HALF_OPEN"     # Testing recovery
```

### CircuitBreaker

State machine for circuit breaker pattern.

**Constructor:**
```python
breaker = CircuitBreaker(failure_threshold=50, cooldown_secs=300)
```

**Methods:**
- `async call(func)` - Execute function with circuit breaker protection
- `record_success()` - Record successful call
- `record_failure()` - Record failed call
- `get_state()` - Get current state
- `get_stats()` - Get breaker statistics

### MQTTHealthMonitor

Monitor health of subscriber and publisher.

**Constructor:**
```python
monitor = MQTTHealthMonitor()
```

**Methods:**
- `record_subscriber_message(count)` - Record received messages
- `record_subscriber_error(error)` - Record error
- `record_publisher_send(count)` - Record sent messages
- `record_publisher_error(error)` - Record error
- `record_heartbeat(timestamp)` - Record heartbeat
- `get_snapshot() -> MetricsSnapshot` - Get current metrics
- `is_healthy() -> bool` - Overall health status

### MetricsSnapshot

Snapshot of current metrics.

**Attributes:**
- `timestamp: datetime`
- `messages_received: int`
- `messages_published: int`
- `errors_total: int`
- `subscriber_healthy: bool`
- `publisher_healthy: bool`

**Methods:**
- `to_json()` - Convert to JSON
- `to_prometheus_format()` - Format as Prometheus exposition text

### HeartbeatPublisher

Periodic heartbeat task for health monitoring.

**Constructor:**
```python
publisher = HeartbeatPublisher(topic, interval_secs=30)
```

**Methods:**
- `async start()` - Start publishing heartbeats
- `async stop()` - Stop publishing

### HealthCheckHandler

HTTP endpoint handler for health checks.

```python
# In FastAPI/Starlette:
@app.get("/health")
async def health_check():
    handler = HealthCheckHandler(mqtt_monitor)
    return await handler.handle_health_check()

@app.get("/metrics")
async def metrics():
    handler = HealthCheckHandler(mqtt_monitor)
    return await handler.handle_metrics()
```

## Utilities (mqtt.utils)

```python
# Retry with exponential backoff
result = await retry_with_backoff(
    func=async_function,
    max_attempts=3,
    initial_delay_sec=1.0,
    max_delay_sec=30.0,
    backoff_multiplier=2.0,
)

# Parse device ID from topic
device_id = parse_device_id_from_topic("dama/colony/device_1/telemetry")
```

## Error Handling

All modules raise standard exceptions:
- `ValueError` - Configuration validation error
- `ConnectionError` - MQTT broker connection failure
- `RuntimeError` - Operation failed (processing, publishing, etc.)
- `TimeoutError` - Operation timed out

Async operations may raise `asyncio.TimeoutError`.

## Best Practices

1. **Configuration**: Load config once at startup, validate immediately
2. **Initialization**: Call `mqtt_startup()` in your app's startup sequence
3. **Shutdown**: Call `mqtt_shutdown()` in cleanup sequence
4. **Error Handling**: Catch exceptions from async operations
5. **Health Monitoring**: Expose health endpoints for observability
6. **Persistence**: Check SQLite database for failed publishes
7. **Circuit Breaker**: Monitor state to detect systemic failures
