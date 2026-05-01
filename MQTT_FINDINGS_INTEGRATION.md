# MQTT Findings Publisher Integration Guide

## Overview

The `MQTTFindingsPublisher` module publishes findings (anomalies, contradictions, hallucinations) extracted from deep_think reasoning results to an MQTT broker for real-time device feedback and correlation.

**Key Features:**
- ✅ Automatic batching (configurable size N and timeout T)
- ✅ QoS=1 publishing with exponential backoff retry
- ✅ SQLite persistence during MQTT outages
- ✅ Auto-recovery and replay on reconnect
- ✅ Async/await throughout, non-blocking
- ✅ Type hints and comprehensive error handling
- ✅ Graceful degradation (continues even if MQTT unavailable)

## Quick Start

### 1. Configuration (.env)

```bash
# MQTT Broker Settings
MQTT_HOST=[REDACTED_MQTT_HOST]
MQTT_PORT=1883
MQTT_USERNAME=dama
MQTT_PASSWORD=[REDACTED_MQTT_PASSWORD]

# Publisher Settings
PUBLISHER_ENABLE=true
PUBLISHER_BATCH_SIZE=10
PUBLISHER_BATCH_TIMEOUT_MS=5000
PUBLISHER_MAX_RETRIES=8
```

### 2. Basic Usage

```python
from mqtt_findings_publisher import (
    MQTTFindingsPublisher,
    Finding,
    findings_from_deep_think_result,
    load_config_from_env
)
import asyncio

async def main():
    # Load config from environment
    config = load_config_from_env()
    
    # Initialize publisher
    publisher = MQTTFindingsPublisher(**config)
    
    # Start (connect to broker, load persisted findings)
    await publisher.start()
    
    try:
        # Extract findings from deep_think result
        deep_think_result = {
            "validation": {
                "overall_confidence": 0.8,
                "contradictions": [...],
                "hallucination_details": [...],
                "claims": [...]
            },
            "pass_cache": [...]
        }
        
        findings = findings_from_deep_think_result(
            deep_think_result,
            device_id="ant_001",
            anomaly_threshold=0.5
        )
        
        # Publish findings (queued for batching)
        for finding in findings:
            await publisher.publish_finding(finding)
            
    finally:
        # Stop publisher (flushes pending batches)
        await publisher.stop()

if __name__ == "__main__":
    asyncio.run(main())
```

## Integration with deep_think Engine

### Architecture

```
deep_think_passes()
    ↓
    [reasoning logic + pass validation]
    ↓
findings_from_deep_think_result()  ← Extract findings
    ↓
MQTTFindingsPublisher.publish_finding()  ← Queue for batching
    ↓
[Batch timer: N findings OR T seconds]
    ↓
MQTT Publish (QoS=1) → dama/colony/findings/{device_id}
    ↓
[On failure] → SQLite persist + Exponential backoff retry
```

### Engine Integration Points

#### 1. Initialize Publisher (at startup)

```python
# In your engine initialization code
from mqtt_findings_publisher import MQTTFindingsPublisher, load_config_from_env

class DeepThinkEngine:
    def __init__(self):
        # ... other initialization ...
        
        config = load_config_from_env()
        self.mqtt_publisher = MQTTFindingsPublisher(**config)
    
    async def start(self):
        # ... other startup logic ...
        await self.mqtt_publisher.start()
    
    async def stop(self):
        await self.mqtt_publisher.stop()
        # ... other shutdown logic ...
```

#### 2. Extract & Publish Findings (in reasoning flow)

```python
from mqtt_findings_publisher import findings_from_deep_think_result

async def deep_think_with_findings(self, question: str, device_id: str, ...):
    # Run deep_think reasoning
    result = await self.deep_think_passes(question, ...)
    
    # Extract findings from result
    findings = findings_from_deep_think_result(
        result,
        device_id=device_id,
        anomaly_threshold=0.5
    )
    
    # Publish findings asynchronously (non-blocking)
    for finding in findings:
        # This returns immediately; batching happens in background
        await self.mqtt_publisher.publish_finding(finding)
    
    return result
```

#### 3. Handle Confirmation Feedback (optional)

```python
async def on_confirmation(device_id: str, claim_id: str, status: str):
    """Called when device sends anomaly confirmation feedback."""
    log.info(f"Device {device_id} confirmed claim {claim_id}: {status}")
    # Update your ML model / scoring / etc.

# Register callback
publisher.set_confirmation_callback(on_confirmation)
```

## Data Models

### Finding Dataclass

```python
@dataclass
class Finding:
    device_id: str              # e.g., "ant_001"
    claim_ids: list[str]        # IDs of supporting claims
    anomalies: list[str]        # Descriptions of problems found
    confidence: float           # 0.0-1.0 confidence score
    severity: str               # "low", "medium", "high", "critical"
    timestamp: str              # ISO 8601 timestamp
    metadata: dict[str, Any]    # Extra context
```

### MQTT Message Format

Published to: `dama/colony/findings/{device_id}`

```json
[
  {
    "device_id": "ant_001",
    "claim_ids": ["claim_1", "claim_2"],
    "anomalies": [
      "Contradiction: Prior evidence suggests otherwise",
      "Hallucination: No evidence for this claim"
    ],
    "confidence": 0.82,
    "severity": "high",
    "timestamp": "2024-01-01T12:34:56Z",
    "metadata": {
      "anomaly_count": 2,
      "pass_count": 3,
      "hallucination_count": 1
    }
  }
]
```

### Confirmation Subscription

Subscribe to: `dama/{device_id}/anomaly_confirmation`

Expected payload:
```json
{
  "claim_id": "claim_1",
  "status": "confirmed"  // or "rejected", "uncertain"
}
```

## Batching Behavior

### Triggers

Findings are published when:

1. **Size threshold reached**: `batch_size` findings collected
   - Default: 10 findings per batch
   - Configurable via `PUBLISHER_BATCH_SIZE`

2. **Timeout reached**: `batch_timeout_ms` elapsed since first finding in batch
   - Default: 5000ms (5 seconds)
   - Configurable via `PUBLISHER_BATCH_TIMEOUT_MS`

3. **Shutdown**: Remaining batches published before stopping

### Per-Device Isolation

Each device maintains its own batch:
- Device `ant_001` batch size independent of `ant_002`
- Timeouts managed separately per device
- Allows natural load distribution

**Example Timeline:**

```
T=0.0s   : Finding 1 (ant_001) → batch=1, start timer
T=0.5s   : Finding 2 (ant_001) → batch=2
T=1.2s   : Finding 3 (ant_002) → batch=1 (different device)
T=1.5s   : Finding 4 (ant_001) → batch=3
...
T=4.8s   : Finding 10 (ant_001) → batch=10 → PUBLISH (size threshold)
           Reset ant_001 batch, cancel timer
...
T=5.2s   : Timeout on ant_002 → PUBLISH 3 findings
           Reset ant_002 batch
```

## Retry & Persistence

### Exponential Backoff

On MQTT publish failure:

```
Attempt 1: Wait 1s,  retry
Attempt 2: Wait 2s,  retry
Attempt 3: Wait 4s,  retry
Attempt 4: Wait 8s,  retry
Attempt 5+: Persist to SQLite, log error
```

Configurable via `PUBLISHER_MAX_RETRIES` (default: 8)

### SQLite Persistence

Location: `~/.deep_think/findings_queue.db`

**Tables:**

```sql
-- Persisted findings awaiting delivery
CREATE TABLE findings_queue (
    id INTEGER PRIMARY KEY,
    device_id TEXT NOT NULL,
    finding_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    retry_count INTEGER DEFAULT 0,
    last_retry_at TIMESTAMP
);

-- Device confirmations received
CREATE TABLE confirmations (
    id INTEGER PRIMARY KEY,
    device_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    status TEXT NOT NULL,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Auto-Recovery

On startup:
1. Connect to MQTT broker
2. Load persisted findings from SQLite
3. Replay persisted findings (with small delays between batches)
4. Delete successfully published findings from SQLite
5. Continue normal operation

If broker unavailable on startup:
- Load persisted findings
- Retry connection periodically
- Continue accepting new findings (queued to persistence)

## Findings Extraction

The `findings_from_deep_think_result()` converter analyzes deep_think results and extracts:

### 1. Validation-based Findings

```python
# Extracted from result["validation"]
- Contradictions: Claims conflicting with prior evidence
- Hallucinations: Claims with no supporting evidence
- Low confidence: Validation scores < threshold
```

### 2. Pass-cache Findings

```python
# Extracted from result["pass_cache"][*]["validation"]
- High-confidence anomalies from individual reasoning passes
- Framing and pass number for context
```

### 3. Severity Determination

| Condition | Severity |
|-----------|----------|
| >2 anomalies + confidence >0.8 | Critical |
| >1 anomaly + confidence >0.7 | High |
| >0 anomalies + confidence >0.6 | Medium |
| Otherwise | Low |

### 4. Threshold Filtering

- Only findings with `confidence >= anomaly_threshold` are published
- Default threshold: 0.5
- Customizable per extraction call

## Error Handling

### Graceful Degradation

If MQTT broker unavailable:
- ✅ Publisher continues running
- ✅ Findings persisted to SQLite
- ✅ No exceptions raised to calling code
- ✅ Auto-recovery on reconnection
- ⚠️ Warnings logged for monitoring

### Logging

```python
import logging
logging.basicConfig(level=logging.DEBUG)

# Producer debug logs
log.debug("Queued finding for ant_001: batch now 3/10")
log.debug("Scheduled batch publish in 4.5s")

# Info logs
log.info("Connected to MQTT broker [REDACTED_MQTT_HOST]:1883")
log.info("Published 10 findings to dama/colony/findings/ant_001")
log.info("Replaying 5 persisted findings...")

# Warning logs
log.warning("Publish failed (attempt 2/8): Connection timeout. Retrying in 2s...")
log.warning("Continuing without MQTT; findings will be persisted locally")

# Error logs
log.error("Failed to connect to MQTT broker: Connection refused")
log.error("Publish failed after 8 retries. Persisting 10 findings for recovery.")
```

## Testing

### Unit Tests

```bash
# Run all tests
pytest test_mqtt_findings_publisher.py -v

# Run specific test class
pytest test_mqtt_findings_publisher.py::TestPersistence -v

# Run with coverage
pytest test_mqtt_findings_publisher.py --cov=mqtt_findings_publisher
```

### Test Coverage

- ✅ Module imports (no errors)
- ✅ Finding dataclass (serialization/deserialization)
- ✅ Persistence store (CRUD operations)
- ✅ Findings converter (extraction + threshold logic)
- ✅ Publisher batching (size + timeout triggers)
- ✅ Exponential backoff retry
- ✅ SQLite persistence on failure
- ✅ Auto-recovery and replay
- ✅ Configuration loading
- ✅ End-to-end flow

### Mock Testing

```python
from unittest.mock import AsyncMock

# Create publisher with mock MQTT client
publisher = MQTTFindingsPublisher(enabled=True, batch_size=1)
publisher._client = AsyncMock()
publisher._connected = True

# Simulate publish
finding = Finding(...)
await publisher.publish_finding(finding)

# Verify
publisher._client.publish.assert_called_once()
```

## Troubleshooting

### Publisher not connected

```python
if publisher._connected:
    log.info("Connected to MQTT broker")
else:
    log.warning("Publisher not connected; findings persisted to SQLite")
```

### Findings not publishing

1. Check `PUBLISHER_ENABLE=true` in .env
2. Verify MQTT broker credentials: `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`
3. Check network connectivity to broker
4. Review logs: `log.error()` and `log.warning()` messages

### SQLite database locked

- Ensure only one process writes to `~/.deep_think/findings_queue.db`
- Check file permissions: `chmod 644 ~/.deep_think/findings_queue.db`
- Restart publisher to recover

### High memory usage

- Check batch size: `PUBLISHER_BATCH_SIZE` (lower = more frequent publishes)
- Verify MQTT broker is accepting messages
- Monitor persisted findings count: `SELECT COUNT(*) FROM findings_queue`

## Performance Tuning

### Batch Size vs Latency

```
Batch Size | Latency | Throughput | Notes
-----------|---------|------------|------
1          | Low     | Low        | Publish per finding
5          | Medium  | Medium     | Good balance
10         | Higher  | Higher     | More efficient
50         | Highest | Very High  | For high-volume scenarios
```

### Timeout vs Batching

```
Timeout    | Behavior
-----------|----------
100ms      | Very frequent publishes, low throughput
1000ms     | Good balance (default)
5000ms     | Waits longer, higher latency
```

### Retry Strategy

```
Max Retries | Total Backoff Time | Recovery Speed
------------|-------------------|----------------
3           | ~7 seconds        | Fast
8           | ~255 seconds      | Thorough
```

## Production Checklist

- [ ] MQTT credentials secured (use `.env`, not hardcoded)
- [ ] Broker TLS enabled for production (`MQTT_PORT=8883`)
- [ ] Database location writable: `~/.deep_think/findings_queue.db`
- [ ] Logging configured and monitored
- [ ] Batch size tuned for expected volume
- [ ] Retry strategy tested with broker outages
- [ ] Confirmation callback implemented if needed
- [ ] Error handling reviewed with ops team

## See Also

- **mqtt_findings_publisher.py**: Implementation
- **test_mqtt_findings_publisher.py**: Test suite
- **.env.example**: Configuration template
- **MQTT Broker Docs**: http://[REDACTED_MQTT_HOST] (internal)
- **deep_think Engine**: See engine.py integration points
