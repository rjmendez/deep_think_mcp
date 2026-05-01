# Production Security Checklist

## Overview

Security hardening checklist for ground_truth.py deployment. Complete all items before production release.

---

## Category 1: Secrets Management

### ☐ Credentials Not in Logs
- [ ] Verify NOVA_TOKEN is never logged (check ground_truth.py for print/log statements)
- [ ] Verify NOVA_TOTP_SEED is never logged
- [ ] Verify MQTT_PASSWORD is never logged
- [ ] Check configured logging format does not include env vars
- [ ] Test with sample data: `grep -r "NOVA_TOKEN\|TOTP_SEED\|MQTT_PASSWORD" /var/log/`
- **Fix:** Use `logging.getLogger(__name__)` with proper formatters; never `print()` secrets

### ☐ Credentials Not in Code
- [ ] No hardcoded tokens in ground_truth.py
- [ ] No default credentials in .env.example (use placeholder text: `NOVA_TOKEN=<your-token-here>`)
- [ ] No git history containing credentials: `git log -p | grep -i "token\|secret\|password"`
- [ ] Credentials in environment or secret vault only
- **Fix:** Use `git filter-branch` or `git filter-repo` to scrub history if needed

### ☐ Credentials Rotation
- [ ] Establish credential rotation schedule (30-90 days)
- [ ] Document rotation process (who, how often, where)
- [ ] Plan for zero-downtime rotation (new credentials deployed, then old revoked)
- [ ] NOVA_TOKEN rotated regularly; note expiration date
- [ ] MQTT_PASSWORD rotated regularly; update broker user
- **Fix:** Schedule calendar reminder; document procedure in runbook

### ☐ Secrets Storage Secure
- [ ] .env file not committed to git: `git status | grep .env` (should be empty)
- [ ] .env file permissions are 600 (readable only by owner): `ls -la .env`
- [ ] Secrets stored in vault, not on filesystem if possible
- [ ] Database backups encrypted at rest
- **Fix:** `chmod 600 .env`; use Vault/Secrets Manager in production

---

## Category 2: API Authentication & Authorization

### ☐ Nova Authentication Validated
- [ ] NOVA_TOKEN is valid and not expired: Test with `curl -H "Authorization: Bearer $NOVA_TOKEN" http://[REDACTED_INTERNAL_IP]:30850/health`
- [ ] NOVA_TOTP_SEED generates valid tokens: `python3 -c "import pyotp; print(pyotp.TOTP('$NOVA_TOTP_SEED').now())"`
- [ ] X-TOTP-Challenge header present on all Nova requests (verify in ground_truth.py:_get_nova_headers_with_cached_totp)
- [ ] TLS certificate verified (no `verify=False` in requests)
- **Fix:** Regenerate tokens with Nova admin; enable certificate verification

### ☐ MQTT Authentication Validated
- [ ] MQTT_USERNAME and MQTT_PASSWORD are correct and not default
- [ ] Test with: `mosquitto_pub -h $MQTT_BROKER_HOST -u "$MQTT_USERNAME" -P "$MQTT_PASSWORD" -t test -m hello`
- [ ] MQTT broker requires TLS (port 8883, not 1883)
- [ ] TLS certificate verified (MQTT client uses --insecure only for testing)
- **Fix:** Rotate MQTT credentials; enable TLS on broker

### ☐ API Rate Limiting Configured
- [ ] Ground_truth.py implements rate limiting or circuit breaker for Nova calls
- [ ] Nova rate limits honored (check X-RateLimit-* headers, backoff on 429)
- [ ] Request throttling in place if high-throughput environment
- [ ] Health check endpoint has rate limit protection
- **Fix:** Implement token bucket rate limiter; see metrics.py for example

---

## Category 3: Input Validation

### ☐ Claim Structure Validated
- [ ] Claim.id is sanitized (no path traversal: no `../`, `//`)
- [ ] Claim.statement is length-limited (max 10KB)
- [ ] Claim.expected_value is validated (not arbitrary nested object)
- [ ] Claim.claim_type is from allowed list (not free-form string)
- **Fix:** Add input validation in validate() method

### ☐ API Input Validated
- [ ] /health endpoint parameters validated (if any)
- [ ] /validate endpoint requires authentication
- [ ] Claim payload size limited (<10MB)
- [ ] Request timeout set (30s max)
- **Fix:** Add request size checks and timeout middleware

---

## Category 4: Data Protection

### ☐ Database Encryption at Rest
- [ ] SQLite database encrypted (use SQLCipher extension)
- [ ] Database file permissions: 600 (readable only by owner)
- [ ] Database backups encrypted during transfer
- [ ] Database backups encrypted in storage
- **Fix:** Install SQLCipher; rebuild database with encryption

### ☐ Sensitive Data Masking
- [ ] Health endpoint masks MQTT password: `MQTT_PASSWORD=***MASKED***`
- [ ] Health endpoint masks Nova credentials: `NOVA_TOKEN=***MASKED***`
- [ ] Logs do not contain sensor values from PHI (Personal Health Information)
- [ ] Database exports sanitized before sharing
- **Fix:** Add data masking in health endpoint and logging

### ☐ Data Retention Policies
- [ ] Validation results expired after 90 days (if no compliance requirement)
- [ ] Sensor snapshots expired after 30 days
- [ ] Contradiction detection logs retained for 1 year
- [ ] Automatic cleanup script configured and tested
- **Fix:** Add TTL indices to database; implement cleanup job

### ☐ Database Backups Secured
- [ ] Backups stored off-site (not on same server)
- [ ] Backup restoration tested (verify you can actually restore)
- [ ] Backup access restricted (not world-readable)
- [ ] Backup retention policy documented (keep last 7 days, then weekly)
- **Fix:** Configure daily backup to secure storage; test restore monthly

---

## Category 5: Network Security

### ☐ TLS/SSL Enabled
- [ ] Nova communication over HTTPS (not HTTP)
- [ ] MQTT broker over TLS (port 8883, not 1883)
- [ ] Health endpoint over HTTPS in production
- [ ] TLS certificates valid (not expired, not self-signed)
- **Fix:** Configure TLS; install proper certificates; update NOVA_BASE_URL to https://

### ☐ Network Isolation
- [ ] Ground_truth.py runs in private network segment (not exposed to internet)
- [ ] Only authorized services can reach MQTT broker
- [ ] Only authorized services can reach Nova endpoint
- [ ] Firewall rules restrict access to database port
- **Fix:** Configure VPC/security groups; limit access by IP/service

### ☐ Secrets Not in URLs
- [ ] Nova authentication uses header, not URL parameter
- [ ] MQTT authentication uses username/password, not connection string
- [ ] Health endpoint URLs do not contain tokens
- **Fix:** Audit all HTTP requests; ensure secrets in headers, not query params

---

## Category 6: Access Control

### ☐ Database Access Restricted
- [ ] Database file readable only by ground_truth user (600 permissions)
- [ ] No world-readable database backups
- [ ] Database user account has minimal privileges (no admin)
- [ ] Connection pooling with credential isolation
- **Fix:** Set correct file permissions; use least-privilege DB user

### ☐ Code Repository Access
- [ ] Ground_truth.py repository has access controls (not public)
- [ ] Production branch protected (requires reviews before merge)
- [ ] Secrets scanning enabled (pre-commit hooks, CI/CD scans)
- [ ] Admin access limited to on-call engineers
- **Fix:** Enable branch protection; add pre-commit secret scanning

### ☐ Logging Access Restricted
- [ ] Log files not world-readable: `ls -la /var/log/ground_truth.log`
- [ ] Log shipping encrypted in transit
- [ ] Log storage access restricted to authorized users
- [ ] Audit logging enabled for log access
- **Fix:** Set log file permissions to 640 or 600

---

## Category 7: Dependency Security

### ☐ Dependencies Pinned
- [ ] requirements.txt uses pinned versions (not `pkg>=1.0`)
- [ ] No transitive dependency vulnerabilities: `pip-audit`
- [ ] Dependencies scanned for known CVEs before production
- [ ] Security updates tested before deployment
- **Fix:** Pin all versions; run `pip-audit` regularly

### ☐ Vulnerable Dependencies Remediated
- [ ] `pip audit` shows no high/critical vulnerabilities
- [ ] All CVEs in dependencies have a fix available
- [ ] Timeline to update critical CVEs: <1 week
- [ ] Test updates in staging before production deployment
- **Fix:** Update vulnerable packages; document rationale if delayed

---

## Category 8: Audit & Monitoring

### ☐ Audit Logging Enabled
- [ ] All validation requests logged (claim_id, timestamp, result)
- [ ] All Nova calls logged (method, latency, success/failure)
- [ ] All database writes logged (timestamp, operation)
- [ ] Audit logs retained for 1 year
- [ ] Audit logs tamper-evident (immutable storage)
- **Fix:** Add validation_log table; implement immutable logging

### ☐ Security Event Detection
- [ ] Unusual validation failure rate alerts (>5% failed validations)
- [ ] Repeated authentication failures alert (>5 Nova 401 errors)
- [ ] Unusual database access patterns detected
- [ ] Cache eviction rate anomaly detected (>1000 evictions/min)
- **Fix:** Configure alerting thresholds in monitoring system

### ☐ Metrics & Observability
- [ ] Prometheus metrics exposed (with authentication)
- [ ] Metrics do not leak sensitive data (no claim text, sensor values)
- [ ] Metrics endpoint requires API key or bearer token
- [ ] Metrics exported to secure monitoring backend
- **Fix:** Add metrics endpoint auth; sanitize metric labels

---

## Category 9: Error Handling

### ☐ Errors Handled Gracefully
- [ ] No stack traces leaked to users (catch Exception, log to server)
- [ ] Database errors do not expose schema details
- [ ] Nova errors do not expose API keys or URLs
- [ ] MQTT errors do not expose broker credentials
- **Fix:** Add error handling middleware; return generic errors to users

### ☐ Error Logging Secure
- [ ] Errors logged with context (request_id, timestamp)
- [ ] Sensitive data redacted from error messages
- [ ] Exception stack traces logged on server, not sent to client
- [ ] Error logging does not create infinite loops
- **Fix:** Add request ID to logs; sanitize error context

---

## Category 10: Deployment Security

### ☐ Secure Configuration Management
- [ ] Configuration validated before startup: `./validate_env.sh`
- [ ] No plaintext secrets in configuration files
- [ ] Configuration changes require approval (code review)
- [ ] Configuration changes logged and auditable
- **Fix:** Use config validation script; implement audit logging

### ☐ Container/Image Security
- [ ] Docker image scanned for vulnerabilities: `docker scan myimage`
- [ ] Image uses minimal base image (python:3.10-slim, not latest)
- [ ] Image layers use non-root user (UID >1000)
- [ ] Image digests pinned (not floating tags like `latest`)
- **Fix:** Scan image; use specific base image version; add non-root user

### ☐ Supply Chain Security
- [ ] Dependencies sourced from trusted registries
- [ ] PyPI packages verified (have version, not `*`)
- [ ] Git commits signed (GPG) before production merge
- [ ] Dependency manifests locked (requirements.txt, not setup.py)
- **Fix:** Enable commit signing; lock dependency manifests

---

## Pre-Deployment Security Checklist

Run this before every production deployment:

```bash
# 1. Verify no secrets in code
grep -r "NOVA_TOKEN\|MQTT_PASSWORD\|SECRET" . --include="*.py" --include="*.md"
# Should return only expected matches (docs, examples with placeholders)

# 2. Verify dependencies
pip audit

# 3. Verify environment validation
./validate_env.sh --strict

# 4. Verify TLS connectivity
curl -vI https://[REDACTED_INTERNAL_IP]:30850/health

# 5. Verify database integrity
sqlite3 /data/ground_truth.db "PRAGMA integrity_check;"

# 6. Verify file permissions
ls -la .env ground_truth.py /data/ground_truth.db
# Should show: .env (600), ground_truth.py (644), db (600)

# 7. Sign off
echo "Security checklist passed on $(date)" >> DEPLOY_CHECKLIST.log
```

---

## Incident Response

### If Credentials Are Compromised
1. **Immediately revoke:** NOVA_TOKEN, MQTT_PASSWORD
2. **Notify:** Infrastructure team; Incident Commander
3. **Audit:** Check logs for unauthorized access (last 30 days)
4. **Regenerate:** New credentials from vault/admin
5. **Deploy:** Restart service with new credentials within 1 hour
6. **Verify:** Health endpoint shows new credentials working
7. **Document:** Post-incident review within 24 hours

### If Data Breach Suspected
1. **Isolate:** Disconnect from network if necessary
2. **Preserve:** Do not modify database; preserve logs
3. **Notify:** Incident Commander; Legal/Compliance
4. **Investigate:** Determine what data was accessed
5. **Notify:** Affected users (if PII disclosed)
6. **Remediate:** Update credentials; patch vulnerability
7. **Communicate:** Post-incident review with timeline

---

## Related Documentation

- [FAILURE_MODES.md](FAILURE_MODES.md) - Security failure scenarios
- [HEALTH_CHECK.md](HEALTH_CHECK.md) - Monitoring security metrics
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) - Security issue diagnosis
