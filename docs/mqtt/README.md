# MQTT Integration Module

A comprehensive MQTT integration for the deep_think_mcp reasoning engine, providing:

- **Subscriber**: Connects to MQTT broker, deserializes telemetry into claims
- **Publisher**: Publishes findings (anomalies, contradictions) back to MQTT
- **Resilience**: Circuit breaker pattern, health monitoring, metrics collection
- **Local-only**: Reasoning uses local Ollama models only (no cloud providers)

## Quick Start

### Configuration

Set environment variables:

```bash
MQTT_ENABLE=true
MQTT_HOST=botnet.floppydicks.net
MQTT_PORT=1883
MQTT_USERNAME=dama
MQTT_PASSWORD=<secret>
MQTT_USE_TLS=false
MQTT_BATCH_SIZE=10
MQTT_BATCH_TIMEOUT_MS=5000
```

### Basic Usage

```python
from mqtt import MQTTClaimsProcessor, mqtt_startup, mqtt_shutdown

# Start MQTT integration
await mqtt_startup()

# Run your application
# MQTT claims are automatically batched and processed

# Shutdown gracefully
await mqtt_shutdown()
```

## Architecture

```
MQTT Broker
    ↓
Subscriber (ground_truth.py: DAMAColonySubscriber)
    ↓
Claims Queue
    ↓
Batch Processor (mqtt.subscriber.MQTTClaimsProcessor)
    ↓
Deep Think Reasoning Engine
    ↓
Findings Publisher (mqtt.publisher.FindingsBatchPublisher)
    ↓
MQTT Broker (dama/colony/findings/{device_id})
```

## Module Structure

- **mqtt/subscriber.py**: Claims processor and MQTT lifecycle management
- **mqtt/publisher.py**: Findings publisher with batching and SQLite persistence
- **mqtt/resilience.py**: Circuit breaker, health monitoring, metrics
- **mqtt/config.py**: MQTT configuration from environment
- **mqtt/models.py**: Finding dataclass and other models
- **mqtt/utils.py**: Retry logic and helper functions
- **mqtt/tests/**: Comprehensive test suite with fixtures

## Documentation

See individual files for detailed documentation:

- [SETUP.md](SETUP.md) - Installation and configuration
- [OPERATIONS.md](OPERATIONS.md) - Running, monitoring, troubleshooting
- [API.md](API.md) - Module API documentation
- [ARCHITECTURE.md](ARCHITECTURE.md) - System design and data flows
