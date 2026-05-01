# Health Check Endpoint Specification

## Overview

The `/health` endpoint provides a comprehensive status check of all critical system components required for ground_truth.py to function properly in production. This endpoint enables monitoring, load balancing decisions, and automated alerting.

## Endpoint Details

**Path:** `/health`  
**Method:** `GET`  
**Response Code:** 
- `200 OK` - All systems operational
- `503 Service Unavailable` - One or more critical services degraded

## Response Schema

```json
{
  "status": "healthy|degraded|error",
  "timestamp_utc": "2024-05-01T12:34:56Z",
  "nova_available": {
    "connected": true,
    "latency_ms": 45,
    "library_index_size": 2841,
    "last_error": null,
    "last_checked_utc": "2024-05-01T12:34:56Z"
  },
  "mqtt_connected": {
    "connected": true,
    "broker_uri": "mqtt.example.com:8883",
    "subscriptions": 5,
    "message_queue_depth": 12,
    "last_error": null,
    "last_checked_utc": "2024-05-01T12:34:56Z"
  },
  "db_ok": {
    "connected": true,
    "db_path": "/data/ground_truth.db",
    "size_bytes": 2097152,
    "tables": ["claims", "validation_results", "sensor_snapshots"],
    "last_write_utc": "2024-05-01T12:34:55Z",
    "last_error": null,
    "last_checked_utc": "2024-05-01T12:34:56Z"
  },
  "cache_status": {
    "entries": 342,
    "size_bytes": 5242880,
    "hit_rate": 0.87,
    "max_size_bytes": 10485760,
    "eviction_count": 23,
    "last_checked_utc": "2024-05-01T12:34:56Z"
  },
  "validation_metrics": {
    "total_validations": 1243,
    "validations_last_hour": 156,
    "avg_latency_ms": 234,
    "error_rate": 0.012,
    "last_checked_utc": "2024-05-01T12:34:56Z"
  }
}
```

## Component Checks

### Nova Availability
- **Check:** Connect to Nova at `NOVA_BASE_URL`, verify authentication with TOTP token
- **Metric:** Response latency in milliseconds
- **Failure Mode:** Returns `connected=false`, logs last error
- **Recovery:** Auto-retries on next health check; does not block deployment

### MQTT Connection
- **Check:** Verify active connection to MQTT broker specified in `MQTT_BROKER_HOST:MQTT_BROKER_PORT`
- **Metrics:** 
  - `connected`: Boolean indicating active subscription
  - `subscriptions`: Count of active topic subscriptions
  - `message_queue_depth`: Number of pending MQTT messages
- **Failure Mode:** Returns `connected=false`, queues messages locally
- **Recovery:** Attempts automatic reconnection with exponential backoff

### Database Persistence
- **Check:** SQLite database writable at `GROUND_TRUTH_DB_PATH`
- **Metrics:**
  - `size_bytes`: Current database file size
  - `tables`: List of schema tables (should contain: claims, validation_results, sensor_snapshots)
  - `last_write_utc`: Timestamp of last successful write
- **Failure Mode:** Falls back to in-memory cache; data loss risk on restart
- **Recovery:** Manual recovery: restore from backup, or delete corrupted DB and restart

### Cache Status
- **Check:** In-memory cache size and eviction stats
- **Metrics:**
  - `entries`: Number of cached items
  - `size_bytes`: Current memory usage
  - `max_size_bytes`: Configured maximum cache size
  - `hit_rate`: Ratio of cache hits to total lookups
  - `eviction_count`: Number of items evicted due to size limit
- **Failure Mode:** If cache exceeds max size, least-recently-used items are evicted
- **Recovery:** Adjust `MQTT_CACHE_SIZE_LIMIT` or `NOVA_CACHE_SIZE_LIMIT` environment variables

## Implementation Guidelines

1. **Non-blocking:** Health checks should complete in <1s. Use timeout on external calls.
2. **Caching:** Cache health check results for 10 seconds to avoid hammering services.
3. **Logging:** Log failures at WARN level; include component name and error details.
4. **Graceful Degradation:** If a component is unavailable, return status but continue operation.
5. **Alerting:** Integrate with monitoring system to alert on repeated failures (>3 consecutive failures).

## Integration Points

- **Kubernetes:** Use as liveness/readiness probe with timeout of 5s
- **Load Balancers:** Remove instance from rotation if status != "healthy" for >30s
- **Monitoring Dashboards:** Poll `/health` every 30s; graph component availability
- **Alerts:** Trigger incident if MQTT offline >5min or Nova latency >5s

## Example cURL Request

```bash
curl -s http://localhost:8080/health | jq .

# With authentication
curl -s -H "Authorization: Bearer <token>" \
     -H "X-TOTP-Challenge: <totp>" \
     http://localhost:8080/health | jq .
```

## Related Documentation

- [MONITORING.md](MONITORING.md) - Dashboard and metrics definitions
- [FAILURE_MODES.md](FAILURE_MODES.md) - Detailed failure scenarios
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) - Problem diagnosis guide
