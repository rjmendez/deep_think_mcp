#!/usr/bin/env markdown
# MQTT Integration — Quick Reference

## Files Changed/Created

```
deep_think_mcp/
├── mqtt_integration.py              [NEW] 545 lines - Core MQTT integration
├── server.py                         [MODIFIED] - Added MQTT lifecycle hooks
├── .env                              [MODIFIED] - Added MQTT configuration
├── tests/test_mqtt_integration_new.py [NEW] 347 lines - 19 unit tests
├── MQTT_INTEGRATION.md               [NEW] Documentation
└── DELIVERY_SUMMARY_MQTT.md          [NEW] This delivery summary
```

## Configuration (in .env)

```ini
# Enable/disable MQTT integration
MQTT_ENABLE=true

# Broker settings ([REDACTED_MQTT_HOST] is configured)
MQTT_HOST=[REDACTED_MQTT_HOST]
MQTT_PORT=1883
MQTT_USERNAME=dama
MQTT_PASSWORD=[REDACTED_MQTT_PASSWORD]

# Queue and batch settings
MQTT_SUBSCRIBER_QUEUE_SIZE=1000
MQTT_BATCH_SIZE=10
MQTT_BATCH_TIMEOUT_MS=5000

# Output topic
MQTT_FINDINGS_TOPIC=dama/colony/findings/{device_id}
```

## How It Works

1. **MQTT Subscriber** connects to `dama/+/telemetry`
2. **Deserializes** sensor data → Claim objects
3. **Processor** batches claims (10 at a time or after 5s)
4. **deep_think** analyzes batch with local-only Ollama
5. **Results** published to `dama/colony/findings/{device_id}`

## Start/Stop

```bash
# Start engine (MQTT auto-starts)
python3 -m deep_think_mcp

# Graceful shutdown
kill -TERM <pid>  # or Ctrl+C
```

## Verify It's Working

```bash
# Check configuration
grep "MQTT_ENABLE\|MQTT_HOST" .env

# Run tests
pytest tests/test_mqtt_integration_new.py -v

# Check if running
ps aux | grep deep_think_mcp
```

## Monitor Logs

```bash
# Watch MQTT operations
tail -f engine.log | grep "\[MQTT\]"

# Or use your logging system
journalctl -u deep_think_mcp -f | grep "\[MQTT\]"
```

## Security Notes

- ✅ All MQTT data processed locally (force_local_models=True)
- ✅ No cloud provider leakage
- ✅ Passwords hidden in logs
- ✅ Graceful error handling

## Troubleshooting

| Issue | Check |
|-------|-------|
| MQTT not starting | `MQTT_ENABLE=true` in .env |
| No claims processed | MQTT broker reachable? Devices sending? |
| High error rate | Ollama running? Deep_think working? |
| Connection failing | Broker credentials correct? Firewall? |

## API (if needed)

```python
from mqtt_integration import (
    is_mqtt_enabled,                  # Check if enabled
    get_mqtt_processor,               # Get processor instance
    get_mqtt_subscriber,              # Get subscriber instance
    mqtt_startup,                     # Start manually
    mqtt_shutdown,                    # Stop manually
    MQTTConfig,                       # Configuration class
    MQTTClaimsProcessor,              # Processor class
)

# Check status
if is_mqtt_enabled():
    proc = get_mqtt_processor()
    print(f"Processed: {proc._processed_count}")
    print(f"Errors: {proc._error_count}")
```

## Test Coverage

- ✅ Configuration loading (7 tests)
- ✅ Batch processing (6 tests)
- ✅ Results extraction (3 tests)
- ✅ Lifecycle management (2 tests)
- ✅ Real .env loading (1 test)
- **Total: 19/19 PASS**

## Next Steps

1. Deploy with `MQTT_ENABLE=true` (already set in .env)
2. Verify MQTT broker is reachable
3. Verify devices are sending telemetry to `dama/+/telemetry`
4. Monitor `dama/colony/findings/+` for results
5. Watch `[MQTT]` logs for processing updates

---

**Status: ✅ READY FOR PRODUCTION**

The integration is complete, tested, documented, and ready to deploy.
