# Health Check Endpoint

The deep_think_mcp server includes a `/health` endpoint for monitoring and load balancer integration.

## Endpoint: GET /health

Returns real-time metrics about the queue state and server health.

### Response Format

```json
{
  "status": "healthy",
  "http_status": 200,
  "timestamp": "2024-01-01T00:00:00.000000+00:00",
  "pending_count": 5,
  "avg_latency": 10.5,
  "last_success_timestamp": "2024-01-01T00:00:00.000000+00:00",
  "worker_count": 1,
  "db_status": "healthy",
  "completed_count": 100
}
```

### Status Codes

- **HTTP 200 OK**: Server is healthy and queue is within acceptable limits
- **HTTP 503 Service Unavailable**: Server is degraded (too many pending jobs or database error)

### Metrics

| Field | Description | Type |
|-------|-------------|------|
| `status` | "healthy" or "degraded" | string |
| `http_status` | HTTP response code | integer |
| `timestamp` | When the response was generated (ISO 8601) | string |
| `pending_count` | Number of queued jobs waiting to be processed | integer |
| `avg_latency` | Average duration of completed jobs in seconds | float |
| `last_success_timestamp` | When the last job completed successfully (ISO 8601) | string \| null |
| `worker_count` | Number of active worker processes | integer |
| `db_status` | Database connectivity status ("healthy", "unavailable", "error") | string |
| `completed_count` | Total number of completed jobs | integer |
| `reason` | Why the server is degraded (only if status != "healthy") | string |

## Configuration

### Pending Job Threshold

Control when the server is considered degraded using the environment variable:

```bash
export DEEP_THINK_HEALTH_MAX_PENDING=100
```

Default: 100 jobs

When `pending_count >= max_pending`, the endpoint returns HTTP 503 with status "degraded".

## Usage Examples

### Basic Health Check

```bash
curl http://localhost:8080/health
```

### With Load Balancer (Kubernetes)

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 30

readinessProbe:
  httpGet:
    path: /health
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 10
```

### Monitoring

```bash
# Monitor health every second
watch -n 1 'curl -s http://localhost:8080/health | jq .'

# Export as Prometheus metrics
curl http://localhost:8080/health | jq '.pending_count, .avg_latency, .completed_count'
```

## Performance

- **Response time**: < 100ms (uses cached metrics with 10-second TTL)
- **DB queries**: Lightweight aggregates only (COUNT, AVG, MAX)
- **No blocking**: Health checks don't interfere with job processing

## Troubleshooting

### Status is "degraded" with db_status: "unavailable"

The database connection failed. Check:
- Database file permissions
- Disk space
- Network connectivity (if using remote DB)

### Too many pending jobs

If `pending_count >= max_pending`:
- More workers are needed (scale up)
- Jobs are taking longer than expected (check job complexity)
- Check logs for worker failures

### Database integrity error

If status is "degraded" with reason containing "integrity check failed":
- Server will attempt to restore from backup
- Check logs for details
- May require manual database recovery

## Testing

Run the health endpoint tests:

```bash
pytest tests/test_health.py -v
```

Test coverage includes:
- Response format validation
- Queue state reflection
- Concurrent requests handling
- Database failure scenarios
- Edge cases (empty queue, high latency, etc.)
