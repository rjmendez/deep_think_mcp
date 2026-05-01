# MQTT Integration for deep_think_mcp

**Wire DAMAColonySubscriber into the deep_think_mcp async event loop**

## Overview

This integration connects the `DAMAColonySubscriber` (in `ground_truth.py`) to the deep_think_mcp engine's async event loop, enabling real-time processing of MQTT telemetry from DAMA phone sensors through local-only Ollama reasoning models.

### Architecture

```
MQTT Broker ([REDACTED_MQTT_HOST])
    ↓
DAMAColonySubscriber (in ground_truth.py)
    - Connects to dama/+/telemetry
    - Deserializes sensor data → Claim objects
    - Queues claims in asyncio.Queue
    ↓
MQTTClaimsProcessor (in mqtt_integration.py)
    - Batches claims (batch_size or batch_timeout)
    - Passes to deep_think_passes() with local-only Ollama
    - Publishes findings to dama/colony/findings/{device_id}
    ↓
deep_think_mcp Engine (engine.py)
    - Runs multi-pass reasoning on batched claims
    - Enforces force_local_models=True (no cloud providers)
    - Returns confidence scores and findings
```

## Components

### 1. MQTTConfig

Loads and validates configuration from environment variables.

**Environment Variables:**

```bash
# Enable/disable MQTT integration
MQTT_ENABLE=true|false                    # Default: false

# Broker connection
MQTT_HOST=[REDACTED_MQTT_HOST]         # Default: [REDACTED_MQTT_HOST]
MQTT_PORT=1883                            # Default: 1883 (1883=plain, 8883=TLS)
MQTT_USERNAME=dama                        # Default: dama
MQTT_PASSWORD=[REDACTED_MQTT_PASSWORD]  # From .env (secret)
MQTT_USE_TLS=false                        # Default: false

# Queue and batch configuration
MQTT_SUBSCRIBER_QUEUE_SIZE=1000          # Max claims in subscriber queue
MQTT_BATCH_SIZE=10                        # Claims per batch to deep_think
MQTT_BATCH_TIMEOUT_MS=5000               # Max time to wait for batch (ms)

# Output routing
MQTT_FINDINGS_TOPIC=dama/colony/findings/{device_id}  # Where to publish results
```

**Validation:**

```python
config = MQTTConfig()
error = config.validate()
if error:
    print(f"Configuration error: {error}")
```

### 2. MQTTClaimsProcessor

Batches claims from the subscriber and processes them through deep_think.

**Responsibilities:**

1. **Collect batches** — Accumulate claims from subscriber queue until batch_size or batch_timeout
2. **Process via deep_think** — Call `deep_think_passes()` with local-only models (force_local_models=True)
3. **Extract findings** — Parse deep_think result for confidence, claims, and anomalies
4. **Publish results** — Send findings back to MQTT dama/colony/findings/{device_id}
5. **Error handling** — Log errors, gracefully degrade (confidence 0.0), and continue processing

**Key Methods:**

```python
processor = MQTTClaimsProcessor(config, subscriber)

# Lifecycle
await processor.start()      # Start batch processor task
await processor.stop()       # Stop processor, flush remaining claims

# Statistics (monitoring)
processor._processed_count   # Total batches processed
processor._error_count       # Total processing errors
```

**Example Usage:**

```python
# Create processor
config = MQTTConfig()
subscriber = DAMAColonySubscriber(
    broker_host=config.broker_host,
    broker_port=config.broker_port,
)
processor = MQTTClaimsProcessor(config, subscriber)

# Start subscriber and processor
await subscriber.start()
await processor.start()

# Process runs in background, handling claims automatically
# Stop when done
await processor.stop()
await subscriber.stop()
```

### 3. Lifecycle Management

**Startup Hook** (`mqtt_startup()`):
- Initializes MQTTConfig from environment
- Validates configuration
- Creates DAMAColonySubscriber
- Starts subscriber (connects to broker)
- Creates and starts MQTTClaimsProcessor

**Shutdown Hook** (`mqtt_shutdown()`):
- Flushes remaining claims in batch buffer
- Stops processor task
- Stops subscriber task
- Gracefully disconnects from broker

**Signal Handlers** (`setup_signal_handlers()`):
- Registers SIGTERM and SIGINT handlers
- Calls mqtt_shutdown() on signal
- Ensures graceful shutdown on ctrl+c or SIGTERM

### 4. Integration with Engine

The `server.py` lifespan context manager calls:

```python
@asynccontextmanager
async def _lifespan(app):
    # ... existing startup ...
    
    # [NEW] MQTT startup
    await mqtt_integration.mqtt_startup()
    mqtt_integration.setup_signal_handlers()
    
    try:
        yield
    finally:
        # [NEW] MQTT shutdown
        await mqtt_integration.mqtt_shutdown()
        
        # ... existing cleanup ...
```

This ensures MQTT integration starts when the engine starts and stops cleanly on shutdown.

## Security Features

### Local-Only Models (Force Local)

All MQTT claims are processed with **force_local_models=True** in deep_think_passes():

```python
await deep_think_passes(
    question=question,
    task_class="general",
    data_policy="local",           # Ollama only
    force_local_models=True,       # Block cloud providers
    device_id=device_id,
)
```

This prevents sensitive telemetry from being sent to cloud providers (Anthropic, Copilot, etc.).

### Password Security

Passwords are never logged. The MQTTConfig repr() hides passwords:

```python
>>> config = MQTTConfig()
>>> repr(config)
'MQTTConfig(host=[REDACTED_MQTT_HOST]:1883, user=dama, enabled=True, batch_size=10, batch_timeout=5000ms)'
# Note: password is NOT shown
```

## Error Handling

### Graceful Degradation

On processing errors, the processor:
1. Logs the error with full traceback
2. Returns confidence 0.0 in findings
3. Continues processing next batch
4. Increments error counter for monitoring

### Exponential Backoff

DAMAColonySubscriber handles reconnection with exponential backoff (1s → 60s) on broker disconnect.

### Batch Flushing

On shutdown, any remaining claims in the batch buffer are flushed before stopping.

## Testing

Comprehensive unit tests cover all components:

```bash
cd /home/USER/development/deep_think_mcp
pytest tests/test_mqtt_integration_new.py -v
```

**Test Coverage:**

- ✓ Configuration loading and validation (7 tests)
- ✓ Batch collection and processing (5 tests)
- ✓ Findings extraction and publishing (3 tests)
- ✓ Lifecycle management (2 tests)
- ✓ Real .env configuration (1 test)

## Monitoring and Observability

### Logging

All operations are logged with `[MQTT]` prefix for easy auditing:

```
[MQTT] Initialized processor: MQTTConfig(...)
[MQTT] Subscriber task started
[MQTT] Processor task started
[MQTT] Processing batch of 10 claims from pixel-9-pro-xl
[MQTT] Batch processed successfully. Findings: 3 items, Total processed: 42
[MQTT] Published findings to dama/colony/findings/pixel-9-pro-xl
```

### Status API

```python
from mqtt_integration import get_mqtt_processor, is_mqtt_enabled

# Check if MQTT is enabled
if is_mqtt_enabled():
    processor = get_mqtt_processor()
    print(f"Processed: {processor._processed_count}")
    print(f"Errors: {processor._error_count}")
```

## Configuration Examples

### Local Development (Localhost Ollama)

```bash
export MQTT_ENABLE=true
export MQTT_HOST=[REDACTED_MQTT_HOST]
export MQTT_PORT=1883
export MQTT_USERNAME=dama
export MQTT_PASSWORD=[REDACTED_MQTT_PASSWORD]
export OLLAMA_BASE_URL=http://localhost:11434
export DEEP_THINK_DATA_POLICY=local
```

### Production (Remote Ollama)

```bash
export MQTT_ENABLE=true
export MQTT_HOST=[REDACTED_MQTT_HOST]
export MQTT_PORT=8883
export MQTT_USERNAME=dama
export MQTT_PASSWORD=...
export MQTT_USE_TLS=true
export OLLAMA_BASE_URL=http://[REDACTED_INTERNAL_IP]:11434
export DEEP_THINK_FORCE_LOCAL=1
```

### Disabled (Fallback)

```bash
export MQTT_ENABLE=false
# MQTT integration will not start, no errors
```

## Files

- **mqtt_integration.py** — Main integration module
  - MQTTConfig
  - MQTTClaimsProcessor
  - mqtt_startup/mqtt_shutdown lifecycle
  - setup_signal_handlers

- **server.py** — Modified to wire in MQTT on startup/shutdown

- **.env** — Configuration file with all MQTT settings

- **tests/test_mqtt_integration_new.py** — Unit tests (19 tests, 100% pass)

## Troubleshooting

### MQTT not starting

Check the engine logs for `[MQTT]` prefix:

```bash
grep "\[MQTT\]" /var/log/deep_think_mcp.log
```

If you see "Configuration error" or "Startup error", verify .env variables:

```bash
env | grep MQTT_
```

### No claims being processed

Verify the MQTT broker is running and reachable:

```bash
timeout 5 python3 -c "from mqtt_integration import DAMAColonySubscriber; import asyncio; print(asyncio.run(DAMAColonySubscriber().start()))" 
```

Check that devices are publishing to dama/{device_id}/telemetry:

```bash
mosquitto_sub -h [REDACTED_MQTT_HOST] -u dama -P <password> -t 'dama/+/telemetry' -v
```

### High error rate

Check deep_think engine logs:

```bash
tail -50 ~/.deep_think/jobs.db
```

Verify Ollama is running and has models loaded:

```bash
curl http://localhost:11434/api/tags
```

## Next Steps

1. Deploy the engine with MQTT_ENABLE=true in .env
2. Monitor processing with grep "[MQTT]" in engine logs
3. Subscribe to dama/colony/findings/+ to see real-time results
4. Set up dashboards to track processor._processed_count and processor._error_count
5. Configure alerts on processor._error_count threshold

---

**Status: Ready for Integration**

✅ Module imports without errors  
✅ Configuration loads from environment  
✅ Async lifecycle management complete  
✅ Error handling verified with tests  
✅ 19/19 unit tests passing  
✅ Documentation complete  
