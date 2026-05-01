# Failure Modes and Recovery Procedures

## Overview

This document describes known failure scenarios in ground_truth.py production deployment, their symptoms, impact on users, and recovery procedures.

---

## Scenario 1: Nova/Great Library Unreachable

### Symptoms
- HTTP connection timeout to Nova at `NOVA_BASE_URL`
- TOTP authentication failures
- Claims cannot be validated against the Great Library
- Log messages: `nova_available: false`, `latency_ms: null`

### Root Causes
- Network connectivity loss to Oxalis k3s cluster
- Nova service crashed or restarted
- TLS certificate mismatch (SSL verification failure)
- NOVA_TOKEN or NOVA_TOTP_SEED expired or invalid
- Nova rate limiting (HTTP 429 Too Many Requests)

### Impact on Users
- Validation confidence scores return 0.0 (no data)
- Fallback to MQTT-only validation (lower accuracy)
- Model claims cannot be corrected with ground truth
- Reports to users show "validation data unavailable"

### Recovery Steps
1. **Immediate (automated):**
   - Provider falls back to MQTT broker if configured
   - Uses cached validation results if available (up to 24 hours old)
   - Requests are queued and retried with exponential backoff (1s, 2s, 4s, 8s... max 60s)

2. **Manual intervention:**
   - Verify Nova service health: `curl -H "Authorization: Bearer $NOVA_TOKEN" http://[REDACTED_INTERNAL_IP]:30850/health`
   - Check pod status: `kubectl get pods -n agents -l app=nova`
   - Restart Nova if needed: `kubectl rollout restart deployment/nova -n agents`
   - Verify network connectivity: `ping [REDACTED_INTERNAL_IP]`
   - Validate credentials: Ensure NOVA_TOKEN and NOVA_TOTP_SEED are correct
   - Check TLS certificate: `openssl s_client -connect [REDACTED_INTERNAL_IP]:30850 -showcerts`

3. **Monitoring/Alerts:**
   - Health check endpoint reports `nova_available: false`
   - Prometheus metric: `ground_truth_nova_latency_seconds_count` stops incrementing
   - Alert fires if Nova unavailable for >5 minutes
   - Dashboard shows Nova section in red

### Prevention
- Monitor Nova service logs for crash patterns
- Rotate credentials regularly
- Use circuit breaker pattern (fail fast after 3 consecutive failures)
- Implement rate limit awareness (check headers, back off if needed)

---

## Scenario 2: MQTT Broker Offline

### Symptoms
- Cannot connect to MQTT broker at `MQTT_BROKER_HOST:MQTT_BROKER_PORT`
- MQTT connection timeout (typically 10 seconds)
- No new sensor data available
- Log messages: `mqtt_connected: false`, `reconnecting...`

### Root Causes
- MQTT broker service crashed
- Network routing broken
- MQTT credentials (username/password) invalid
- Firewall blocking connection
- TLS certificate expired (for MQTT over TLS)

### Impact on Users
- Real-time sensor data not available
- Validation falls back to cached data (increasing age)
- Cache becomes stale over time (up to 24 hours)
- User reports show "last updated X hours ago"

### Recovery Steps
1. **Immediate (automated):**
   - Provider retries connection with exponential backoff (1s, 2s, 4s, 8s... max 60s)
   - Uses in-memory cache for pending validations
   - Marks cache entries as "stale" if older than 30 minutes

2. **Manual intervention:**
   - Check MQTT broker logs: `docker logs mqtt-broker` or broker pod logs
   - Verify broker is listening: `nc -zv <MQTT_HOST> <MQTT_PORT>`
   - Verify credentials: Check MQTT_USERNAME and MQTT_PASSWORD in .env
   - Restart MQTT broker if needed
   - Check firewall rules: Ensure port is open from provider host
   - Validate TLS certificate expiration (for TLS connections)

3. **Monitoring/Alerts:**
   - Health check reports `mqtt_connected: false`
   - Prometheus metric: `ground_truth_mqtt_connection_failures_total` increments
   - Alert fires if MQTT offline for >30 seconds
   - Dashboard shows MQTT section in red

### Prevention
- Run MQTT broker with automatic restart (systemd, Docker, k3s)
- Monitor MQTT connection health in real-time
- Implement connection pooling with health checks
- Rotate credentials periodically

---

## Scenario 3: Database Corrupted or Unavailable

### Symptoms
- SQLite database file is corrupted, locked, or unreadable
- Write errors when storing validation results
- Cannot query historical validation data
- Log messages: `db_ok: false`, `disk I/O error`, `database is locked`

### Root Causes
- Disk space full (no room for writes)
- File permissions changed (database not readable)
- Unclean process shutdown (incomplete writes)
- Storage corruption (hardware failure, filesystem error)
- Multiple processes trying to write simultaneously

### Impact on Users
- Validation results cannot be persisted
- Historical data lost on process restart
- Falls back to in-memory cache only (limited size)
- Audit trail of validations is incomplete

### Recovery Steps
1. **Immediate (automated):**
   - Provider detects DB unavailable and falls back to in-memory cache
   - Logs warning-level message with error details
   - Continues operating with reduced persistence

2. **Manual intervention:**
   - Check database file permissions: `ls -la /data/ground_truth.db`
   - Verify disk space: `df -h /data`
   - Check if database is locked: `fuser /data/ground_truth.db`
   - Attempt to repair: `sqlite3 /data/ground_truth.db "PRAGMA integrity_check;"`
   - If repair fails, restore from backup: `cp /data/backup/ground_truth.db.bak /data/ground_truth.db`
   - If no backup, delete corrupted file and restart: `rm /data/ground_truth.db` (WARNING: data loss)

3. **Monitoring/Alerts:**
   - Health check reports `db_ok: false`
   - Prometheus metric: `ground_truth_validation_errors_total` increments
   - Alert fires if DB unavailable for >1 minute
   - Disk usage alerts if disk >90% full

### Prevention
- Regular database backups (hourly snapshots)
- Monitor disk space and alert if <10% free
- Use fsync=FULL for SQLite to prevent corruption
- Implement process locking to prevent concurrent writes
- Monitor file permissions and restore on change

---

## Scenario 4: Excessive Memory Usage / Cache OOM

### Symptoms
- In-memory cache grows without bound
- Process memory usage exceeds `MQTT_CACHE_SIZE_LIMIT`
- Operating system kills process (OOM killer)
- Log messages: `cache_size_bytes: 10GB, max_size_bytes: 512MB`, `evicting LRU entries`

### Root Causes
- Cache size limit not configured
- No cache eviction policy
- High validation throughput (many claims cached)
- Memory leak in cache cleanup code
- Circular references preventing garbage collection

### Impact on Users
- Process restarts unexpectedly (breaking ongoing validations)
- Services depending on ground_truth.py become unavailable
- Cached validation data is lost

### Recovery Steps
1. **Immediate (automated):**
   - Cache evicts least-recently-used (LRU) entries when size limit reached
   - Logs warning when evictions occur
   - Continues operating with reduced cache size

2. **Manual intervention:**
   - Check current memory usage: `ps aux | grep ground_truth`
   - Check cache size: Query health endpoint to see `cache_status.size_bytes`
   - Increase cache limit (if hardware supports): `export MQTT_CACHE_SIZE_LIMIT=1000000000` (1GB)
   - Restart process to clear cache: `systemctl restart ground_truth`
   - Investigate memory leaks: Profile with `memory_profiler` or `py-spy`

3. **Monitoring/Alerts:**
   - Health check reports `cache_status.size_bytes` and `eviction_count`
   - Prometheus metric: `ground_truth_cache_evictions_total` increments
   - Alert fires if cache_size_bytes >90% of max_size_bytes
   - Alert fires if eviction_count increases rapidly (>100 per minute)

### Prevention
- Set appropriate cache size limits based on hardware (e.g., 10% of total RAM)
- Monitor cache hit rate; if <50%, cache is too small
- Implement TTL-based expiration (not just LRU)
- Profile memory usage under load during testing
- Use `--max-old-space-size` or equivalent for memory-constrained environments

---

## Scenario 5: Nova Rate Limiting (HTTP 429)

### Symptoms
- HTTP 429 (Too Many Requests) responses from Nova
- Validation latency increases significantly (Nova requests queued)
- Some validations fail with "rate limit exceeded" error
- Log messages: `nova_error: 429 Too Many Requests`

### Root Causes
- Validation request rate exceeds Nova's limit (e.g., 100 req/sec)
- Multiple instances of ground_truth.py hitting same Nova backend
- Nova rate limit reset not honored (no backoff)
- Burst of validation requests due to model inference spike

### Impact on Users
- Some validations are not performed (confidence = 0.0)
- User reports show incomplete validation coverage
- Claims cannot be fact-checked against Great Library

### Recovery Steps
1. **Immediate (automated):**
   - Provider detects 429 and implements exponential backoff (start 1s, double each attempt)
   - Requests are queued for retry
   - Falls back to MQTT validation if available
   - Logs warning with retry estimate

2. **Manual intervention:**
   - Check Nova rate limit configuration: `kubectl get configmap -n agents nova-config`
   - Verify Nova is not overloaded: Check Nova pod CPU/memory usage
   - Distribute load across multiple instances (if possible)
   - Increase Nova's rate limit if appropriate
   - Reduce validation request rate on client side (batching, sampling)

3. **Monitoring/Alerts:**
   - Prometheus metric: `ground_truth_nova_latency_seconds` increases (queuing delays)
   - Alert fires if 429 rate >5% of requests
   - Alert fires if backoff queue depth >1000 items
   - Dashboard shows "Nova rate-limited" indicator

### Prevention
- Implement client-side rate limiting (token bucket or sliding window)
- Batch validation requests (validate 10 claims per API call, not 10 separate calls)
- Implement circuit breaker (stop sending after 5 consecutive 429s)
- Monitor rate limit headers from Nova (`X-RateLimit-Remaining`, etc.)

---

## Scenario 6: Validation Engine Deadlock

### Symptoms
- Validation requests hang (no response for >30 seconds)
- CPU usage remains high but validation throughput is 0
- Log messages stop being written
- Health check endpoint becomes unresponsive

### Root Causes
- Deadlock in validation logic (two threads waiting on each other)
- Circular dependency in claim validation
- Nova request waiting for MQTT message that depends on Nova validation
- Thread holding lock while calling blocking function

### Impact on Users
- All validations blocked until restart
- Users cannot get results
- Downstream systems depending on ground_truth.py hang

### Recovery Steps
1. **Immediate (automated):**
   - Health check timeout (>5 seconds) triggers alert
   - Kubernetes liveness probe fails, triggers pod restart
   - In-progress validations are lost, queued requests are retried

2. **Manual intervention:**
   - Get thread dump: `python3 -m tracemalloc` or `gdb attach <pid>`
   - Identify stuck threads and their lock holders
   - Restart process: `systemctl restart ground_truth`
   - Investigate root cause in logs and source code

3. **Monitoring/Alerts:**
   - Health check endpoint timeout (response time >5s)
   - Alert fires immediately on timeout
   - Dashboard shows red status for ground_truth.py component

### Prevention
- Use timeouts on all blocking calls (Nova, MQTT, DB)
- Avoid holding locks while calling external services
- Use async/await patterns instead of threads where possible
- Regular thread safety audits and code reviews
- Load testing to detect deadlock conditions before production

---

## General Recovery Patterns

### Exponential Backoff
```
retry_delay = min(base_delay * (2 ^ attempt), max_delay)
# Example: 1s, 2s, 4s, 8s, 16s, 30s (max), 30s, ...
```

### Circuit Breaker
```
if (failures > threshold):
    circuit = OPEN  # stop sending requests
    if (time_since_open > cooldown):
        circuit = HALF_OPEN  # test one request
        if (test_succeeds):
            circuit = CLOSED  # resume normal operation
```

### Graceful Degradation
1. Try Nova validation first (highest accuracy)
2. Fall back to MQTT validation (medium accuracy)
3. Fall back to cached results (lower accuracy, but available)
4. Return 0.0 confidence (unknown, let user decide)

---

## Related Documentation

- [HEALTH_CHECK.md](HEALTH_CHECK.md) - Health endpoint monitoring
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) - Problem diagnosis guide
- [MONITORING.md](MONITORING.md) - Metrics and alerting
