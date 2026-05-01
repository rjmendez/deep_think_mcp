# Deployment Checklist

## Overview

Pre-deployment, deployment, and post-deployment verification steps for ground_truth.py production releases.

---

## Pre-Deployment Checklist (48 hours before)

### Code & Testing

- [ ] All tests passing: `pytest tests/ -v --cov=ground_truth`
  - [ ] Unit tests pass: `pytest tests/unit/ -v`
  - [ ] Integration tests pass: `pytest tests/integration/ -v`
  - [ ] Load tests pass: `python3 tests/load_test.py --requests=10000 --concurrency=100`
  - [ ] Coverage >80%: `pytest --cov=ground_truth --cov-report=term-missing`

- [ ] Code review completed (minimum 2 approvals)
  - [ ] All comments resolved
  - [ ] No blocking issues
  - [ ] Security review completed
  - [ ] Merge conflicts resolved

- [ ] Documentation updated
  - [ ] README.md updated with version/changes
  - [ ] API documentation current
  - [ ] Changelog entry added
  - [ ] Migration guide updated (if schema changes)

### Configuration & Secrets

- [ ] Environment validation passes: `./validate_env.sh --strict`
  - [ ] All required env vars set
  - [ ] Network connectivity verified
  - [ ] Database accessible
  - [ ] Credentials valid

- [ ] No secrets in code: `grep -r "NOVA_TOKEN\|MQTT_PASSWORD\|SECRET" . --include="*.py"`
  - [ ] Only expected matches (docs with placeholders)
  - [ ] No hardcoded values
  - [ ] No git history containing secrets

- [ ] Credentials rotated recently (within 90 days)
  - [ ] NOVA_TOKEN created/validated <90 days ago
  - [ ] MQTT_PASSWORD created/validated <90 days ago
  - [ ] Document expiration dates

- [ ] Backup verified and tested
  - [ ] Database backup created: `cp /data/ground_truth.db /backup/ground_truth.db.pre-deploy`
  - [ ] Backup restore tested: `sqlite3 /backup/ground_truth.db.pre-deploy "SELECT COUNT(*) FROM validation_results;"`
  - [ ] Backup location documented and accessible
  - [ ] Backup encryption verified

### Database & Schema

- [ ] Database integrity verified: `sqlite3 /data/ground_truth.db "PRAGMA integrity_check;"`
  - [ ] Returns "ok"
  - [ ] No corruption detected
  - [ ] File size reasonable (<20GB)

- [ ] Schema migrations prepared (if applicable)
  - [ ] Migration scripts reviewed
  - [ ] Backward compatibility verified
  - [ ] Rollback procedure documented
  - [ ] Test migration on staging environment

- [ ] Performance baseline captured
  - [ ] Current query latencies recorded
  - [ ] Cache hit rates recorded
  - [ ] Throughput metrics recorded
  - [ ] Memory/CPU baseline captured

### Infrastructure & Monitoring

- [ ] Alert rules reviewed and enabled
  - [ ] Critical alerts configured (Nova/MQTT down, DB errors)
  - [ ] Warning alerts configured (high latency, low cache hit rate)
  - [ ] PagerDuty/Slack integration verified
  - [ ] On-call rotation checked

- [ ] Monitoring dashboards prepared
  - [ ] Grafana dashboard created/updated
  - [ ] Key metrics visible (validation latency, error rate, availability)
  - [ ] Drill-down panels available
  - [ ] Runbook links present

- [ ] Logging verified
  - [ ] Log aggregation configured (ELK, Splunk, etc.)
  - [ ] Log retention policy set
  - [ ] Sensitive data not logged
  - [ ] Log search queries prepared

### Deployment Infrastructure

- [ ] Kubernetes manifests ready (if applicable)
  - [ ] Deployment YAML updated with new image
  - [ ] Service/Ingress rules configured
  - [ ] Resource requests/limits set appropriately
  - [ ] Health probe endpoints configured

- [ ] Load balancer configuration updated
  - [ ] Backend health checks configured
  - [ ] Session stickiness (if needed) set
  - [ ] Rate limiting rules updated
  - [ ] SSL certificate valid (not expiring soon)

- [ ] Rollback plan documented
  - [ ] Previous version image/binary location known
  - [ ] Database rollback procedure clear
  - [ ] Failover procedures tested
  - [ ] Communication plan for incidents

### Team Readiness

- [ ] Deployment window scheduled
  - [ ] Time and duration set
  - [ ] Stakeholders notified
  - [ ] On-call engineer assigned
  - [ ] Maintenance window scheduled

- [ ] Runbooks accessible
  - [ ] TROUBLESHOOTING.md reviewed
  - [ ] FAILURE_MODES.md reviewed
  - [ ] Team familiar with rollback procedure
  - [ ] Escalation contacts updated

- [ ] Team communication plan
  - [ ] Status page configured
  - [ ] Slack/email notification prepared
  - [ ] Incident channel ready
  - [ ] Post-deployment review scheduled

---

## Deployment Checklist (During Release)

### Pre-Deployment (15 minutes before)

- [ ] Final health check
  ```bash
  curl http://localhost:8080/health | jq .
  # Verify all systems operational
  ```

- [ ] Backup verified
  ```bash
  ls -lh /backup/ground_truth.db.pre-deploy
  # Confirm backup exists and is recent
  ```

- [ ] Team assembled
  - [ ] On-call engineer ready
  - [ ] Communications channel open (Slack, war room)
  - [ ] Incident commander assigned (if major release)
  - [ ] Stakeholders on standby

- [ ] Monitoring active
  - [ ] Grafana dashboard open
  - [ ] Alert system active
  - [ ] Log aggregation live
  - [ ] Metrics endpoint responsive

### Deployment Steps

**Step 1: Update Code/Image**
- [ ] Pull latest code: `git pull origin main`
- [ ] Update image in registry (if containerized): `docker push myregistry/ground-truth:v1.2.3`
- [ ] Verify image available: `docker pull myregistry/ground-truth:v1.2.3`

**Step 2: Update Configuration** (if needed)
- [ ] Review .env changes: `git diff HEAD~1 .env.example`
- [ ] Update .env: Add any new required variables
- [ ] Run validation: `./validate_env.sh --strict`

**Step 3: Apply Database Migrations** (if schema changed)
- [ ] Backup before migrations: `cp /data/ground_truth.db /backup/ground_truth.db.pre-migration`
- [ ] Run migrations: `python3 -m ground_truth migrate --apply-all`
- [ ] Verify migrations: `python3 -m ground_truth migrate --status`
- [ ] Spot check data: `sqlite3 /data/ground_truth.db "SELECT COUNT(*) FROM validation_results;"`

**Step 4: Update Service** (Single Instance)
```bash
# Stop service gracefully (allow in-flight requests to complete)
systemctl stop ground_truth

# Update binary
cp ground_truth.py /usr/local/bin/ground_truth.py

# Verify
python3 -m py_compile /usr/local/bin/ground_truth.py

# Start service
systemctl start ground_truth
```

**Or Step 4: Update Service** (Kubernetes)
```bash
# Update image in deployment
kubectl set image deployment/ground-truth \
  ground-truth=myregistry/ground-truth:v1.2.3 \
  -n default

# Wait for rollout (default rolling update)
kubectl rollout status deployment/ground-truth -n default
# Should see: 1 old pod, 1 new pod, then transition complete
```

**Step 5: Verify Deployment**
- [ ] Service is responsive: `curl http://localhost:8080/health`
- [ ] Validation working: POST test claim, verify response
- [ ] Database accessible: `sqlite3 /data/ground_truth.db "SELECT 1;"`
- [ ] Metrics available: `curl http://localhost:8080/metrics`

### Post-Deployment (Immediate - 5 minutes after)

- [ ] Health checks green
  ```bash
  curl http://localhost:8080/health | jq '.status'
  # Should output: "healthy"
  ```

- [ ] Validation throughput normal
  ```bash
  # Watch throughput
  watch -n 1 'curl -s http://localhost:8080/health | jq .validation_metrics.validations_last_hour'
  # Should increase by ~10-20 per minute
  ```

- [ ] No error spikes
  ```bash
  # Check error rate
  curl http://localhost:8080/health | jq '.validation_metrics.error_rate'
  # Should be <1% (similar to pre-deployment baseline)
  ```

- [ ] Database working
  ```bash
  # Test database write
  python3 -c "import sqlite3; db=sqlite3.connect('/data/ground_truth.db'); db.execute('INSERT INTO claims (id, statement) VALUES (?, ?)', ('test-deploy', 'test')); db.commit(); print('DB write OK')"
  ```

- [ ] Nova/MQTT connected
  ```bash
  curl http://localhost:8080/health | jq '{nova: .nova_available.connected, mqtt: .mqtt_connected.connected}'
  # Should output: {"nova": true, "mqtt": true}
  ```

- [ ] Logs look good (no errors)
  ```bash
  tail -50 /var/log/ground_truth.log | grep -i error
  # Should show 0-2 errors (normal), not spikes
  ```

### Post-Deployment (Ongoing - First hour)

- [ ] Monitor dashboard continuously
  - [ ] Validation latency stable (no increase)
  - [ ] Error rate stable (<1%)
  - [ ] Cache hit rate stable (>80%)
  - [ ] Memory/CPU usage normal

- [ ] Monitor logs for warnings/errors
  ```bash
  tail -f /var/log/ground_truth.log | grep -i "error\|warning"
  # Should be similar to pre-deployment levels
  ```

- [ ] Verify no incidents filed
  - [ ] Check PagerDuty/alert system
  - [ ] No unexpected alerts
  - [ ] No escalations

- [ ] Smoke test scenarios
  - [ ] Validate a real claim from model output
  - [ ] Verify contradiction detection still working
  - [ ] Verify cache is being used (hit rate >80%)
  - [ ] Verify fallback behavior (test Nova offline scenario)

### Post-Deployment (Next 24 hours)

- [ ] Monitor metrics vs baseline
  - [ ] Validation latency comparable
  - [ ] Error rate comparable
  - [ ] Cache hit rate comparable
  - [ ] No memory leaks (memory stable)

- [ ] Monitor error logs
  - [ ] No new error patterns
  - [ ] No repeated failures
  - [ ] No cascading failures

- [ ] Database health
  - [ ] Database size growing normally (not exponentially)
  - [ ] Query latencies normal
  - [ ] No lock contention

- [ ] User feedback
  - [ ] No user-reported issues
  - [ ] Performance acceptable
  - [ ] No data loss/corruption reports

---

## Rollback Checklist

### When to Rollback
- Service unavailable (health check returns error)
- Validation error rate >10% (up from <1%)
- Database corruption detected
- Critical security issue found
- Data loss confirmed
- **Decision:** Make within 5 minutes; do not wait

### Rollback Steps

**Step 1: Stop Current Version**
```bash
# Graceful shutdown (wait for in-flight requests)
systemctl stop ground_truth  # or: kubectl rollout undo deployment/ground-truth -n default

# Or force stop if necessary (may lose in-flight requests)
systemctl kill -s KILL ground_truth
```

**Step 2: Restore Database** (if schema changed)
```bash
cp /backup/ground_truth.db.pre-deploy /data/ground_truth.db
chmod 644 /data/ground_truth.db
```

**Step 3: Revert Code**
```bash
git checkout v1.1.0  # previous working version

# Or restore previous image (if containerized)
kubectl set image deployment/ground-truth \
  ground-truth=myregistry/ground-truth:v1.1.0 \
  -n default
```

**Step 4: Restart Service**
```bash
systemctl start ground_truth
# Or: kubectl rollout status deployment/ground-truth -n default
```

**Step 5: Verify Rollback**
- [ ] Service is responsive
- [ ] Health checks pass
- [ ] Validation working
- [ ] Database accessible
- [ ] Error rate normal

### Post-Rollback

- [ ] Document what went wrong
- [ ] Schedule post-incident review
- [ ] Update runbooks based on findings
- [ ] Notify stakeholders of incident
- [ ] Plan re-deployment with fixes

---

## Deployment Record Template

```
╔═══════════════════════════════════════════════════════════════════════╗
║                      DEPLOYMENT RECORD                                 ║
╠═══════════════════════════════════════════════════════════════════════╣
║ Version:           v1.2.3                                             ║
║ Date:              2024-05-01 14:30 UTC                               ║
║ Deployed by:       alice@example.com                                  ║
║ Deployment time:   12 minutes                                         ║
║ Database migrated: Yes (v2 → v3 schema)                              ║
║ Rollback tested:   Yes (successful rollback to v1.1.0)               ║
╠═══════════════════════════════════════════════════════════════════════╣
║ Pre-Deployment Status:                                                ║
║ - Validation latency P95: 234ms                                       ║
║ - Validation error rate: 0.8%                                         ║
║ - Cache hit rate: 87%                                                 ║
║ - Database size: 2.3GB                                                ║
╠═══════════════════════════════════════════════════════════════════════╣
║ Post-Deployment Status:                                               ║
║ - Validation latency P95: 241ms (✓ acceptable)                       ║
║ - Validation error rate: 0.9% (✓ acceptable)                         ║
║ - Cache hit rate: 85% (✓ acceptable)                                 ║
║ - Database size: 2.4GB (✓ normal growth)                             ║
╠═══════════════════════════════════════════════════════════════════════╣
║ Incidents:  None                                                       ║
║ Rollbacks:  None                                                       ║
║ Status:     ✓ SUCCESSFUL                                              ║
╚═══════════════════════════════════════════════════════════════════════╝
```

---

## Related Documentation

- [HEALTH_CHECK.md](HEALTH_CHECK.md) - Health verification
- [FAILURE_MODES.md](FAILURE_MODES.md) - Incident recovery
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) - Issue diagnosis
- [UPGRADE.md](UPGRADE.md) - Migration procedures
