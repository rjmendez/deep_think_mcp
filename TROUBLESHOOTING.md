# Troubleshooting Runbook

## Overview

Quick reference guide for diagnosing and resolving common issues in production ground_truth.py deployments.

---

## Problem: "Validation always returns 0.0 confidence"

### Symptoms
```
validation_result.confidence = 0.0
validation_result.evidence = []
validation_result.ground_truth_value = None
```

User reports show: *"No ground truth data available"*

### Root Causes
1. Nova service unavailable (Great Library not reachable)
2. MQTT broker offline (sensor data not available)
3. Cache expired and no fresh data fetched
4. Nova credentials invalid (authentication failure)
5. Network connectivity broken

### Diagnosis Steps

**Step 1: Check health endpoint**
```bash
curl http://localhost:8080/health | jq .
# Look for: nova_available=false, mqtt_connected=false, or both
```

**Step 2: Check logs**
```bash
tail -50 /var/log/ground_truth.log | grep -i "nova\|mqtt\|confidence"
# Look for: "nova_available: false", "mqtt_connected: false", error messages
```

**Step 3: Verify Nova connectivity**
```bash
curl -H "Authorization: Bearer $NOVA_TOKEN" \
     -H "X-TOTP-Challenge: $(python3 -c 'import pyotp; print(pyotp.TOTP("'$NOVA_TOTP_SEED'").now())')" \
     http://[REDACTED_INTERNAL_IP]:30850/health
# Should return HTTP 200 with {status: "healthy"}
```

**Step 4: Verify MQTT connectivity**
```bash
mosquitto_sub -h $MQTT_BROKER_HOST -p $MQTT_BROKER_PORT -t "dama/sensor/#" --insecure
# Should show recent sensor messages; if none appear, broker is not receiving data
```

**Step 5: Check cache**
```bash
sqlite3 /data/ground_truth.db "SELECT COUNT(*) FROM validation_results;"
# Should return >0; if 0, cache is empty and we have no historical data
```

### Solutions

**If Nova is unavailable:**
```bash
# 1. Verify Nova service is running
kubectl get pods -n agents -l app=nova

# 2. Check Nova logs
kubectl logs -l app=nova -n agents --tail=50

# 3. Restart Nova if needed
kubectl rollout restart deployment/nova -n agents

# 4. Wait for service to stabilize
kubectl rollout status deployment/nova -n agents

# 5. Verify Nova health
curl http://[REDACTED_INTERNAL_IP]:30850/health

# 6. Check ground_truth logs for "nova_available: true"
tail -f /var/log/ground_truth.log | grep nova_available
```

**If MQTT is unavailable:**
```bash
# 1. Verify MQTT broker is running
docker ps | grep mqtt  # or: kubectl get pods -l app=mqtt

# 2. Check broker logs
docker logs mqtt-broker  # or: kubectl logs -l app=mqtt

# 3. Verify connectivity to broker
nc -zv $MQTT_BROKER_HOST $MQTT_BROKER_PORT

# 4. Check MQTT authentication
# Verify MQTT_USERNAME and MQTT_PASSWORD in .env
grep MQTT .env

# 5. Restart provider to reconnect
systemctl restart ground_truth  # or: kubectl rollout restart deployment/ground-truth

# 6. Verify connection re-established
tail -f /var/log/ground_truth.log | grep "mqtt_connected: true"
```

**If both are unavailable:**
```bash
# This is a critical failure; provide degraded service
# 1. Use cached results only (lower accuracy)
# 2. Return confidence=0.0 with note "validation data unavailable"
# 3. Alert on-call engineer immediately
# 4. Do not retry continuously (causes 429s and cascading failures)
```

---

## Problem: "MQTT connection fails with auth error"

### Symptoms
```
ERROR: MQTT connection failed: 401 Not Authorized
ERROR: MQTT_USERNAME or MQTT_PASSWORD incorrect
Service will not start
```

### Root Causes
1. MQTT_USERNAME or MQTT_PASSWORD incorrect in .env
2. MQTT broker user not created or deleted
3. Typo in environment variable
4. Credentials expired or rotated without update

### Diagnosis Steps

**Step 1: Verify .env file**
```bash
grep MQTT .env
# Output should show MQTT_USERNAME and MQTT_PASSWORD without typos
```

**Step 2: Test MQTT credentials manually**
```bash
mosquitto_pub -h $MQTT_BROKER_HOST -p $MQTT_BROKER_PORT \
  -u "$MQTT_USERNAME" -P "$MQTT_PASSWORD" \
  -t "test" -m "hello" --insecure

# If this works, MQTT credentials are correct
# If this fails, credentials are wrong
```

**Step 3: Check MQTT broker logs for auth attempts**
```bash
# For Mosquitto:
docker logs mqtt-broker | grep -i "auth\|denied"

# For MQTT broker in k3s:
kubectl logs -l app=mqtt | grep -i "auth\|denied"
```

**Step 4: Verify MQTT user exists on broker**
```bash
# For Mosquitto (if you have access):
mosquitto_passwd -c /path/to/mosquitto/passwordfile username
# (re-create user if needed)
```

### Solutions

**If credentials are wrong:**
```bash
# 1. Get correct credentials from MQTT broker admin
# 2. Update .env file
nano .env
# Update: MQTT_USERNAME=<correct_username>
# Update: MQTT_PASSWORD=<correct_password>

# 3. Restart provider
systemctl restart ground_truth

# 4. Verify connection succeeds
tail -f /var/log/ground_truth.log | grep "mqtt_connected: true"
```

**If credentials are correct but broker is not recognizing them:**
```bash
# 1. Restart MQTT broker
docker restart mqtt-broker  # or: kubectl rollout restart deployment/mqtt

# 2. Re-create MQTT user if needed
mosquitto_passwd -c /etc/mosquitto/passwd $MQTT_USERNAME

# 3. Restart broker again
docker restart mqtt-broker

# 4. Restart ground_truth provider
systemctl restart ground_truth

# 5. Verify connection
tail -f /var/log/ground_truth.log | grep mqtt_connected
```

---

## Problem: "Memory usage grows unbounded"

### Symptoms
```
Process memory: 2GB, 3GB, 4GB, ... (increasing)
Validation throughput drops to 0
Process killed by OS (OOM killer)
Log: "cache evictions too frequent" or "out of memory"
```

### Root Causes
1. Cache size limit not configured (MQTT_CACHE_SIZE_LIMIT)
2. Cache eviction policy broken (not removing old entries)
3. Memory leak in validation logic (circular references)
4. High validation throughput (more data cached than can be evicted)

### Diagnosis Steps

**Step 1: Check current memory usage**
```bash
ps aux | grep ground_truth
# Look at VSZ and RSS columns (virtual and resident set size)

# Or use Docker/K8s
docker stats ground_truth  # Container memory usage
kubectl top pod ground-truth-xyz  # Pod memory usage
```

**Step 2: Check cache status**
```bash
curl http://localhost:8080/health | jq .cache_status
# Should show: {entries, size_bytes, max_size_bytes, eviction_count}

# If size_bytes is close to max_size_bytes, cache is at limit
# If eviction_count is high (>100/min), evictions are too frequent
```

**Step 3: Check environment variables**
```bash
grep -i "cache\|limit\|max" .env
# Should show: MQTT_CACHE_SIZE_LIMIT=512000000 (500MB)
# Should show: NOVA_CACHE_SIZE_LIMIT=256000000 (250MB)
```

**Step 4: Monitor cache growth**
```bash
# Watch cache size increase in real-time
watch -n 1 'curl -s http://localhost:8080/health | jq .cache_status.size_bytes'

# If size jumps by MB per second, cache is not evicting old entries
```

**Step 5: Profile memory usage**
```bash
# Generate heap dump
python3 -m memory_profiler ground_truth.py 2>&1 | tail -30

# Or use py-spy
pip install py-spy
py-spy dump -p $(pgrep -f ground_truth) > heap.dump

# Analyze heap dump for largest objects
python3 -c "import pdb, pickle; heap = pickle.load(open('heap.dump', 'rb')); print(heap)"
```

### Solutions

**If cache limit not set:**
```bash
# 1. Update .env file
echo "MQTT_CACHE_SIZE_LIMIT=512000000" >> .env  # 500MB
echo "NOVA_CACHE_SIZE_LIMIT=256000000" >> .env  # 250MB

# 2. Restart provider
systemctl restart ground_truth

# 3. Verify limits are applied
tail -f /var/log/ground_truth.log | grep "cache size limit"
```

**If cache limit is too low for your workload:**
```bash
# 1. Increase limit (if hardware supports)
export MQTT_CACHE_SIZE_LIMIT=1000000000  # 1GB
systemctl restart ground_truth

# 2. Monitor memory usage
watch -n 5 'ps aux | grep ground_truth | grep -v grep | awk "{print \$6}"'

# 3. If still growing, investigate memory leak (see Step 5 above)
```

**If memory leak suspected:**
```bash
# 1. Check for circular references in validation loop
# Review: ground_truth.py:validate_batch() and validate()

# 2. Ensure claims, sensor_snapshots are released after validation
# Look for: unused variables keeping references alive

# 3. Add explicit garbage collection
python3 -c "import gc; gc.collect()"

# 4. Restart with memory monitoring enabled
PYTHONUNBUFFERED=1 PYTHONMALLOC=malloc python3 -u ground_truth.py 2>&1 | tee /tmp/gt.log
```

---

## Problem: "Database corrupted; cannot read validation results"

### Symptoms
```
ERROR: sqlite3.DatabaseError: database disk image is malformed
ERROR: database is locked
ERROR: disk I/O error
No historical validation data available
```

### Root Causes
1. Unclean process shutdown (incomplete writes)
2. Hardware/storage failure (bad sector)
3. Filesystem corruption
4. File permissions changed
5. Multiple processes writing simultaneously

### Diagnosis Steps

**Step 1: Check database integrity**
```bash
sqlite3 /data/ground_truth.db "PRAGMA integrity_check;"
# Output: "ok" (healthy) or error description
```

**Step 2: Check file permissions**
```bash
ls -la /data/ground_truth.db
# Should show: -rw-r--r-- (readable, writable)
```

**Step 3: Check disk health**
```bash
df -h /data
# Should show >1GB free

# For hardware issues:
sudo dmesg | grep -i "i/o\|error" | tail -10
```

**Step 4: Check file locks**
```bash
lsof /data/ground_truth.db
# Should show only ground_truth process; if multiple, there's contention
```

### Solutions

**If database is corrupted (integrity check fails):**
```bash
# 1. Attempt repair
sqlite3 /data/ground_truth.db "PRAGMA integrity_check;"
sqlite3 /data/ground_truth.db "VACUUM;"  # Might repair minor issues

# 2. If repair fails, restore from backup
cp /data/backup/ground_truth.db.latest /data/ground_truth.db
chmod 644 /data/ground_truth.db

# 3. If no backup, delete and let provider recreate
rm /data/ground_truth.db
# WARNING: This loses all historical validation data

# 4. Restart provider
systemctl restart ground_truth

# 5. Verify database is created successfully
ls -la /data/ground_truth.db
```

**If file permissions are wrong:**
```bash
# Fix permissions
sudo chown ground_truth:ground_truth /data/ground_truth.db
chmod 644 /data/ground_truth.db

# Restart provider
systemctl restart ground_truth
```

**If disk is full:**
```bash
# Check disk usage
du -sh /data/* | sort -rh | head -10

# Delete old backups if necessary
rm /data/backup/ground_truth.db.old-*

# Free up space, then restart
systemctl restart ground_truth
```

---

## Problem: "Validation latency exceeds timeout (>30 seconds)"

### Symptoms
```
validation_result.confidence = 0.0 (timeout)
ERROR: Validation request timed out after 30s
User sees: "Validation taking longer than expected"
```

### Root Causes
1. Nova is slow or unresponsive (high latency >10s)
2. MQTT broker is slow (message delivery delayed)
3. Database query slow (large table scan)
4. Network congestion or packet loss

### Diagnosis Steps

**Step 1: Check Nova latency**
```bash
# Measure Nova response time
time curl -s http://[REDACTED_INTERNAL_IP]:30850/health >/dev/null

# Check Nova metrics
curl http://[REDACTED_INTERNAL_IP]:30850/metrics | grep nova_latency
```

**Step 2: Check MQTT latency**
```bash
# Subscribe and measure message arrival time
# Send test message on one terminal, time receipt on another
time mosquitto_pub -h $MQTT_BROKER_HOST -t "test" -m "latency_test"
# Should complete in <100ms
```

**Step 3: Check database query performance**
```bash
# Analyze slow queries
sqlite3 /data/ground_truth.db
> .timer on
> SELECT COUNT(*) FROM validation_results;
> PRAGMA index_list(validation_results);
# Look for missing indices on frequently-queried columns
```

**Step 4: Check network latency**
```bash
ping [REDACTED_INTERNAL_IP]  # Nova
ping $MQTT_BROKER_HOST  # MQTT broker

# Check packet loss
ping -c 100 [REDACTED_INTERNAL_IP] | grep "% packet loss"
# Should be 0%; if >1%, network is congested
```

### Solutions

**If Nova is slow:**
```bash
# 1. Check Nova pod status
kubectl top pod <nova-pod> -n agents
# Should show <50% CPU, <500MB memory

# 2. Increase Nova pod resources if needed
kubectl set resources deployment/nova -n agents --limits=cpu=2,memory=2Gi --requests=cpu=1,memory=1Gi

# 3. Scale up Nova replicas
kubectl scale deployment/nova -n agents --replicas=3

# 4. Restart ground_truth provider
systemctl restart ground_truth

# 5. Monitor latency
watch -n 1 'curl -s http://localhost:8080/health | jq .nova_available.latency_ms'
```

**If MQTT is slow:**
```bash
# 1. Check MQTT broker logs
docker logs mqtt-broker | tail -50

# 2. Increase MQTT broker resources
docker update --memory 2g mqtt-broker  # or kubectl scale for k3s

# 3. Check subscriptions; if too many, reduce scope
# Review: ground_truth.py:mqtt_subscribe_topics()

# 4. Restart MQTT broker
docker restart mqtt-broker

# 5. Reconnect ground_truth
systemctl restart ground_truth
```

**If database queries are slow:**
```bash
# 1. Add missing indices
sqlite3 /data/ground_truth.db << EOF
CREATE INDEX IF NOT EXISTS idx_claims_timestamp ON claims(timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_validation_claim_id ON validation_results(claim_id);
EOF

# 2. Vacuum and analyze
sqlite3 /data/ground_truth.db "VACUUM; ANALYZE;"

# 3. Monitor query time
sqlite3 /data/ground_truth.db ".timer on"

# 4. Restart provider to pick up new indices
systemctl restart ground_truth
```

---

## Escalation Path

### For Critical Issues (Validation not working at all)
1. Check health endpoint: `curl http://localhost:8080/health`
2. Verify both Nova AND MQTT are available
3. If both down, escalate to infrastructure team
4. If one down, follow specific recovery steps above

### For Performance Issues (Slow validations)
1. Check latency metrics: `curl http://localhost:8080/health | jq .nova_available.latency_ms`
2. If Nova latency >5s, check Nova pod (is it OOMing?)
3. If MQTT latency >1s, check broker (is it overloaded?)
4. If database latency >500ms, add indices or increase hardware

### For Data Loss Issues (Database corrupted)
1. Restore from most recent backup
2. Document what happened
3. Review backup/restore procedures
4. Increase backup frequency

---

## Related Documentation

- [HEALTH_CHECK.md](HEALTH_CHECK.md) - Health endpoint details
- [FAILURE_MODES.md](FAILURE_MODES.md) - Root cause analysis
- [MONITORING.md](MONITORING.md) - Metrics and alerting
- [SECURITY.md](SECURITY.md) - Credential and access issues
