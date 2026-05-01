# Monitoring Dashboard Specification

## Overview

Defines metrics, dashboards, and alerting rules for ground_truth.py production monitoring. All metrics exposed via `/metrics` endpoint in Prometheus text format.

---

## Metrics Endpoint

**Path:** `/metrics`  
**Format:** Prometheus text format (HELP + TYPE + samples)  
**Refresh:** On-demand (no caching)  
**Authentication:** Optional (set `METRICS_API_KEY` if production)

### Example Request
```bash
curl http://localhost:8080/metrics | head -20

# Output:
# HELP ground_truth_validations_total Total validation attempts
# TYPE ground_truth_validations_total counter
ground_truth_validations_total 1243
# HELP ground_truth_validation_errors_total Validation errors
# TYPE ground_truth_validation_errors_total counter
ground_truth_validation_errors_total 12
...
```

---

## Key Metrics

### Validation Metrics

| Metric | Type | Description | Alert Threshold |
|--------|------|-------------|-----------------|
| `ground_truth_validations_total` | Counter | Total validation attempts | N/A |
| `ground_truth_validations_failed_total` | Counter | Failed validations | >5% error rate |
| `ground_truth_validation_latency_ms` | Histogram | Validation latency (P50, P95, P99) | P95 >5s |
| `ground_truth_validation_confidence_mean` | Gauge | Mean confidence across validations | <0.5 (low confidence) |
| `ground_truth_hallucinations_detected_total` | Counter | Hallucinations found | >100/hour |

### Nova Integration Metrics

| Metric | Type | Description | Alert Threshold |
|--------|------|-------------|-----------------|
| `ground_truth_nova_available` | Gauge | Is Nova available (1=yes, 0=no) | 0 for >5min |
| `ground_truth_nova_latency_ms` | Histogram | Nova API latency | P95 >5s, P99 >10s |
| `ground_truth_nova_errors_total` | Counter | Nova API errors | >5% error rate |
| `ground_truth_nova_timeouts_total` | Counter | Nova request timeouts | >10/min |
| `ground_truth_nova_rate_limit_hits_total` | Counter | Nova 429 rate limit hits | >5/min |

### MQTT Metrics

| Metric | Type | Description | Alert Threshold |
|--------|------|-------------|-----------------|
| `ground_truth_mqtt_connected` | Gauge | Is MQTT connected (1=yes, 0=no) | 0 for >30s |
| `ground_truth_mqtt_messages_total` | Counter | MQTT messages received | N/A |
| `ground_truth_mqtt_connection_failures_total` | Counter | Connection attempts failed | >5/min |
| `ground_truth_mqtt_reconnects_total` | Counter | Reconnection attempts | >1/min |
| `ground_truth_mqtt_message_latency_ms` | Histogram | Message delivery latency | P95 >1s |

### Database Metrics

| Metric | Type | Description | Alert Threshold |
|--------|------|-------------|-----------------|
| `ground_truth_db_available` | Gauge | Is database available (1=yes, 0=no) | 0 for >1min |
| `ground_truth_db_size_bytes` | Gauge | Database file size | >5GB (near limit) |
| `ground_truth_db_writes_total` | Counter | Database write operations | N/A |
| `ground_truth_db_query_latency_ms` | Histogram | Database query latency | P95 >500ms |
| `ground_truth_db_lock_waits_total` | Counter | Database lock waits | >100/min |

### Cache Metrics

| Metric | Type | Description | Alert Threshold |
|--------|------|-------------|-----------------|
| `ground_truth_cache_size_bytes` | Gauge | Current cache size | >90% of max |
| `ground_truth_cache_entries` | Gauge | Number of cached items | N/A |
| `ground_truth_cache_hit_rate` | Gauge | Cache hit rate (0-1) | <0.5 (low hit rate) |
| `ground_truth_cache_hits_total` | Counter | Cache hits | N/A |
| `ground_truth_cache_misses_total` | Counter | Cache misses | N/A |
| `ground_truth_cache_evictions_total` | Counter | Items evicted | >1000/min |

### Contradiction Detection Metrics

| Metric | Type | Description | Alert Threshold |
|--------|------|-------------|-----------------|
| `ground_truth_contradictions_detected_total` | Counter | Contradictions found | >10/hour |
| `ground_truth_contradictions_by_type` | Counter | Contradictions by type (label: type) | type-specific thresholds |
| `ground_truth_contradiction_confidence_mean` | Gauge | Mean confidence of contradictions | N/A |

### System Metrics

| Metric | Type | Description | Alert Threshold |
|--------|------|-------------|-----------------|
| `ground_truth_uptime_seconds` | Gauge | Process uptime | <300s (recent restart) |
| `ground_truth_process_memory_bytes` | Gauge | Memory usage | >80% of limit |
| `ground_truth_process_cpu_seconds` | Counter | CPU time used | N/A |

---

## Dashboard: Overview (Status Page)

### Section 1: Service Health (Top)
```
┌─────────────────────────────────────────────────────────────┐
│ Status: ✓ HEALTHY                                     1243 /d│
│                                                               │
│ Nova:   ✓ Online (45ms)    │ MQTT:   ✓ Connected (12 msgs) │
│ DB:     ✓ OK (0.3s)        │ Cache:  ✓ 87% hit rate        │
└─────────────────────────────────────────────────────────────┘
```

**Panels:**
- Service status (Green=healthy, Yellow=degraded, Red=down)
- Validation throughput (validations/day)
- Component availability (Nova, MQTT, Database, Cache)
- Current SLA compliance (target 99.5%, actual 99.7%)

### Section 2: Validation Performance
```
┌──────────────────────┬──────────────────────┐
│ Validation Latency   │ Confidence Scores    │
│ P50: 45ms            │ Mean: 0.87           │
│ P95: 234ms           │ Min:  0.23           │
│ P99: 1234ms          │ Max:  1.00           │
│ Max: 4567ms          │                      │
└──────────────────────┴──────────────────────┘
```

**Panels:**
- Latency percentiles (P50, P95, P99, max)
- Confidence distribution (mean, min, max, stddev)
- Error rate (%)
- Hallucination count (per hour)

### Section 3: Resource Usage
```
┌──────────────────────┬──────────────────────┐
│ Memory               │ Cache Size           │
│ Current: 456MB       │ Current: 512MB/1GB   │
│ Limit:   2GB (22%)   │ Hit Rate: 87%        │
│ Trend:   ↑ +5MB/hr   │ Evictions: 23/hr     │
└──────────────────────┴──────────────────────┘
```

**Panels:**
- Memory usage (gauge + trend)
- CPU usage (gauge + trend)
- Disk usage (database file size)
- Cache efficiency (size, hit rate, evictions)

---

## Dashboard: Nova Integration (Advanced)

### Section 1: Nova API Performance
```
Latency Distribution (ms)
└────────────────────────────────────────────
  0-50ms:   ████████████████ (50%)
  50-100ms: ████████ (25%)
  100-500ms: ███ (10%)
  500-1000ms: ██ (10%)
  >1000ms:  █ (5%)
```

**Panels:**
- Latency histogram (P50, P95, P99)
- Success rate (%)
- Error breakdown (timeout, 429, 5xx, etc.)
- Response time trend (line graph, 24 hours)

### Section 2: Nova Health & Rate Limiting
```
Nova Availability: ✓ 99.8%
Rate Limit Status: 45/100 requests remaining
Last 429: 15 minutes ago
Next retry available: Now
```

**Panels:**
- Availability uptime (%)
- Rate limit current usage (%)
- Time since last 429 error
- Requests per second (current, peak, average)

---

## Dashboard: MQTT Integration (Advanced)

### Section 1: MQTT Broker Health
```
Connected: ✓ Yes (7 hours uptime)
Messages/sec: 23 msg/s
Queue depth: 12 (ok)
Connection attempts: 234 (1 failed)
Last reconnection: 7 hours ago
```

**Panels:**
- Connection status (connected/disconnected)
- Message throughput (msg/sec)
- Queue depth (pending messages)
- Connection reliability (success rate %)

### Section 2: Sensor Data Freshness
```
Stale sensors: 3 (GPS, Wifi, Temperature)
Avg age: 2 minutes (ok)
Oldest data: 15 minutes (warning)
Message latency: P50=50ms, P95=150ms
```

**Panels:**
- Stale sensor count
- Data age distribution
- Message latency percentiles
- Subscription count

---

## Dashboard: Database (Advanced)

### Section 1: Database Health
```
Size: 2.3GB / 10GB (23%)
Writes: 1.2k/min (ok)
Query latency: P95=234ms (ok)
Lock waits: 2/min (ok)
Last backup: 3 hours ago (ok)
```

**Panels:**
- Database size (gauge)
- Write rate (ops/sec)
- Query latency (P50, P95, P99)
- Lock wait count
- Last backup time

### Section 2: Validation Results Growth
```
Today: 45,234 claims validated
7-day rate: 6.5k/day (trending up)
30-day rate: 5.2k/day
Retention policy: 90 days (expires Mar 15)
```

**Panels:**
- Validation count (today, 7-day, 30-day average)
- Database growth rate (GB/week)
- Retention timeline
- Cleanup job status (last run, next scheduled)

---

## Alert Rules

### Critical Alerts (Page On-Call Immediately)

```yaml
# No validations for >5 minutes
- alert: NoValidations
  expr: increase(ground_truth_validations_total[5m]) == 0
  for: 5m
  annotations:
    summary: "Ground truth stopped validating"
    runbook: "TROUBLESHOOTING.md#no-validations"

# Nova unavailable for >5 minutes
- alert: NovaUnavailable
  expr: ground_truth_nova_available == 0
  for: 5m
  annotations:
    summary: "Nova service unavailable"
    runbook: "TROUBLESHOOTING.md#nova-unavailable"

# MQTT offline for >30 seconds
- alert: MQTTOffline
  expr: ground_truth_mqtt_connected == 0
  for: 30s
  annotations:
    summary: "MQTT broker offline"
    runbook: "TROUBLESHOOTING.md#mqtt-offline"

# Database unavailable
- alert: DatabaseUnavailable
  expr: ground_truth_db_available == 0
  for: 1m
  annotations:
    summary: "Database unavailable"
    runbook: "TROUBLESHOOTING.md#database-unavailable"
```

### Warning Alerts (Notify Team)

```yaml
# High validation error rate
- alert: HighErrorRate
  expr: rate(ground_truth_validation_errors_total[5m]) > 0.05
  annotations:
    summary: "Validation error rate >5%"
    runbook: "FAILURE_MODES.md#validation-failures"

# High latency (P95 >5 seconds)
- alert: HighLatency
  expr: histogram_quantile(0.95, ground_truth_validation_latency_ms) > 5000
  for: 5m
  annotations:
    summary: "Validation latency P95 >5s"
    runbook: "TROUBLESHOOTING.md#slow-validations"

# Cache hit rate low
- alert: LowCacheHitRate
  expr: ground_truth_cache_hit_rate < 0.5
  for: 10m
  annotations:
    summary: "Cache hit rate <50%"
    runbook: "TROUBLESHOOTING.md#low-cache-hit-rate"

# Disk usage high
- alert: HighDiskUsage
  expr: ground_truth_db_size_bytes / 10737418240 > 0.8  # 80% of 10GB
  for: 10m
  annotations:
    summary: "Database disk usage >80%"
    runbook: "TROUBLESHOOTING.md#disk-full"

# Memory usage high
- alert: HighMemoryUsage
  expr: ground_truth_process_memory_bytes / 2147483648 > 0.8  # 80% of 2GB
  for: 10m
  annotations:
    summary: "Memory usage >80%"
    runbook: "TROUBLESHOOTING.md#memory-full"
```

---

## SLA Targets

| Metric | Target | Alert Level |
|--------|--------|------------|
| Uptime | 99.5% | Page if <99.5% (monthly) |
| Validation latency (P95) | <1s | Warn if >5s for 5min |
| Validation latency (P99) | <2s | Warn if >10s for 5min |
| Nova availability | 99.0% | Page if <99% for 5min |
| MQTT connectivity | 99.5% | Page if offline >30s |
| Cache hit rate | >80% | Warn if <50% for 10min |
| Error rate | <1% | Page if >5% for 5min |

---

## Grafana Dashboard JSON

Create dashboard with panels:

```json
{
  "dashboard": {
    "title": "ground_truth.py Production",
    "panels": [
      {
        "title": "Service Status",
        "targets": [
          {"expr": "ground_truth_nova_available"},
          {"expr": "ground_truth_mqtt_connected"},
          {"expr": "ground_truth_db_available"},
          {"expr": "ground_truth_cache_hit_rate"}
        ]
      },
      {
        "title": "Validation Throughput",
        "targets": [
          {"expr": "rate(ground_truth_validations_total[1m])"}
        ]
      },
      {
        "title": "Validation Latency",
        "targets": [
          {"expr": "histogram_quantile(0.5, ground_truth_validation_latency_ms)"},
          {"expr": "histogram_quantile(0.95, ground_truth_validation_latency_ms)"},
          {"expr": "histogram_quantile(0.99, ground_truth_validation_latency_ms)"}
        ]
      },
      {
        "title": "Error Rate",
        "targets": [
          {"expr": "rate(ground_truth_validation_errors_total[5m])"}
        ]
      },
      {
        "title": "Resource Usage",
        "targets": [
          {"expr": "ground_truth_process_memory_bytes"},
          {"expr": "ground_truth_cache_size_bytes"}
        ]
      }
    ]
  }
}
```

---

## Integration with Monitoring Systems

### Prometheus Scrape Config
```yaml
scrape_configs:
  - job_name: 'ground_truth'
    static_configs:
      - targets: ['localhost:8080']
    metrics_path: '/metrics'
    scrape_interval: 30s
    scrape_timeout: 5s
    basic_auth:
      username: prometheus
      password: <api_key>
```

### Alertmanager Routing
```yaml
route:
  receiver: 'pagerduty'
  group_by: ['alertname', 'environment']
  group_wait: 10s
  group_interval: 10s
  repeat_interval: 4h
  routes:
    - match:
        severity: 'critical'
      receiver: 'pagerduty'
      repeat_interval: 15m
    - match:
        severity: 'warning'
      receiver: 'email'
      repeat_interval: 1h
```

---

## Related Documentation

- [HEALTH_CHECK.md](HEALTH_CHECK.md) - Health endpoint
- [FAILURE_MODES.md](FAILURE_MODES.md) - Alert root causes
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) - Runbook references
