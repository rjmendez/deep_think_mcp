# MQTT Setup & Operations

## Installation

MQTT integration is built into deep_think_mcp. Install the package:

```bash
pip install -e .
# or with requirements.txt:
pip install -r requirements.txt
```

## Configuration

### Environment Variables

Create a `.env` file or set environment variables:

```bash
# Enable MQTT integration
MQTT_ENABLE=true

# Broker Connection
MQTT_HOST=[REDACTED_MQTT_HOST]
MQTT_PORT=1883
MQTT_USERNAME=dama
MQTT_PASSWORD=your_secret_password
MQTT_USE_TLS=false                    # Set to true for secure connection

# Subscriber Settings
MQTT_SUBSCRIBER_QUEUE_SIZE=1000       # Max claims in queue
MQTT_BATCH_SIZE=10                    # Claims per batch
MQTT_BATCH_TIMEOUT_MS=5000            # Flush timeout (ms)

# Publisher Settings
MQTT_FINDINGS_TOPIC=dama/colony/findings/{device_id}
```

### Validation

```python
from mqtt import MQTTConfig

config = MQTTConfig()
error = config.validate()
if error:
    print(f"Configuration error: {error}")
    exit(1)
```

## Running

### With FastMCP Server

The MQTT integration is automatically initialized when the server starts:

```bash
python3 server.py
```

The server will:
1. Load MQTT config from environment
2. Initialize subscriber and processor
3. Start accepting MQTT claims
4. Process through deep_think reasoning
5. Publish findings back to MQTT

### Standalone Integration

```python
import asyncio
from mqtt import mqtt_startup, mqtt_shutdown

async def main():
    # Initialize
    await mqtt_startup()
    
    try:
        # Your application runs here
        while True:
            await asyncio.sleep(1)
    finally:
        # Cleanup
        await mqtt_shutdown()

asyncio.run(main())
```

## Monitoring

### Health Endpoints

If running with FastAPI/Starlette:

```python
from mqtt.resilience import HealthCheckHandler

@app.get("/mqtt/health")
async def mqtt_health():
    """Get MQTT health status."""
    handler = HealthCheckHandler(mqtt_monitor)
    return await handler.health()

@app.get("/mqtt/metrics")
async def mqtt_metrics():
    """Get Prometheus metrics."""
    handler = HealthCheckHandler(mqtt_monitor)
    return await handler.metrics()
```

### Checking Status

```python
from mqtt import get_mqtt_processor

processor = get_mqtt_processor()
stats = await processor.get_stats()
print(f"Processed: {stats['processed']}")
print(f"Errors: {stats['errors']}")
print(f"Current batch size: {stats['batch_buffer_size']}")
```

### SQLite Persistence

Check for persisted findings during outages:

```bash
sqlite3 mqtt_failures.db
> SELECT * FROM findings;
> SELECT * FROM publish_confirmations;
```

## Troubleshooting

### Connection Issues

**Symptom**: "MQTT broker not available" in logs

**Solutions:**
- Verify MQTT_HOST and MQTT_PORT are correct
- Check network connectivity: `telnet MQTT_HOST MQTT_PORT`
- Verify broker is running
- Check firewall rules if on different networks

### Authentication Failures

**Symptom**: "Authentication refused" in MQTT logs

**Solutions:**
- Verify MQTT_USERNAME and MQTT_PASSWORD are correct
- Check broker's user database
- Verify user has publish/subscribe permissions

### Claims Not Processing

**Symptom**: Claims received but not processed

**Solutions:**
- Check MQTT_ENABLE=true
- Verify batch size with `MQTT_BATCH_SIZE` (too large?)
- Check batch timeout with `MQTT_BATCH_TIMEOUT_MS`
- Look for deep_think processing errors in logs
- Check circuit breaker state: is it OPEN?

### Findings Not Published

**Symptom**: No findings published to MQTT

**Solutions:**
- Verify publisher connection: check health endpoint
- Check findings topic: `MQTT_FINDINGS_TOPIC`
- Verify SQLite persistence: findings being saved?
- Check circuit breaker state

### High Latency

**Symptom**: Slow end-to-end processing

**Solutions:**
- Reduce `MQTT_BATCH_SIZE` to process faster
- Reduce `MQTT_BATCH_TIMEOUT_MS` to flush sooner
- Check deep_think reasoning time
- Profile Ollama model inference
- Check network latency to broker

## Logging

MQTT module uses Python logging. Configure in your app:

```python
import logging

# Enable debug logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("mqtt").setLevel(logging.DEBUG)

# Disable verbose logging from aiomqtt
logging.getLogger("aiomqtt").setLevel(logging.WARNING)
```

## Performance Tuning

### For High Throughput

```bash
MQTT_BATCH_SIZE=100              # Process more claims together
MQTT_BATCH_TIMEOUT_MS=1000       # Short timeout for frequent batching
MQTT_SUBSCRIBER_QUEUE_SIZE=5000  # Larger queue buffer
```

### For Low Latency

```bash
MQTT_BATCH_SIZE=1                # Process immediately
MQTT_BATCH_TIMEOUT_MS=100        # Very short timeout
MQTT_SUBSCRIBER_QUEUE_SIZE=100   # Smaller queue
```

### For Reliability

```bash
MQTT_BATCH_SIZE=10               # Reasonable batch size
MQTT_BATCH_TIMEOUT_MS=5000       # Generous timeout
MQTT_USE_TLS=true                # Encrypted connection
```

## Security

### Best Practices

1. **Credentials**: Never commit passwords to version control
   ```bash
   # Use .env file (not in git)
   MQTT_PASSWORD=your_secret_password
   
   # Or environment variables
   export MQTT_PASSWORD="..."
   ```

2. **TLS/SSL**: Enable for production
   ```bash
   MQTT_USE_TLS=true
   MQTT_PORT=8883  # Standard TLS port
   ```

3. **Access Control**: Use MQTT broker ACLs
   - Subscriber user: publish to `dama/colony/device_*/telemetry`, subscribe to `dama/colony/device_*/anomaly_confirmation`
   - Publisher user: publish to `dama/colony/findings/*`

4. **Network**: Restrict broker access
   - Firewall port 1883 (or 8883 for TLS)
   - Only allow trusted IPs

### SQLite Database Security

The `mqtt_failures.db` contains findings:

```bash
# Restrict access
chmod 600 mqtt_failures.db

# Back up regularly
cp mqtt_failures.db mqtt_failures.db.$(date +%Y%m%d)
```

## Maintenance

### Database Cleanup

Periodically remove old persisted findings:

```bash
sqlite3 mqtt_failures.db
> DELETE FROM findings WHERE published_at < datetime('now', '-30 days');
> VACUUM;
```

### Log Rotation

Configure log rotation for your application:

```python
from logging.handlers import RotatingFileHandler

handler = RotatingFileHandler(
    'deep_think.log',
    maxBytes=10*1024*1024,  # 10MB
    backupCount=5
)
```

### Health Checks

Set up monitoring:

```bash
# Health check script
curl -s http://localhost:8000/mqtt/health | jq .

# Prometheus scraping
# Add to Prometheus config:
# - job_name: 'deep_think'
#   static_configs:
#     - targets: ['localhost:8000']
#   metrics_path: '/mqtt/metrics'
```

## Examples

See `examples/` directory:

- `example_mqtt_findings_integration.py` - Basic integration pattern
- `mqtt_resilience_example.py` - Health monitoring setup
