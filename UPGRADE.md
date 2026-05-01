# Upgrade and Migration Guide

## Overview

This guide covers upgrading ground_truth.py between versions, managing database schema changes, and migrating credentials and configuration to new environments.

---

## Backward Compatibility Policy

### Compatibility Guarantee
- Minor versions (e.g., 1.0 → 1.1): **Backward compatible** — no breaking changes
- Major versions (e.g., 1.0 → 2.0): May include breaking changes (see migration guide)
- Patch versions (e.g., 1.0.0 → 1.0.1): Bug fixes only, fully compatible

### Data Compatibility
- Database schema: Migrations are applied automatically on startup (see Migrations section)
- Cache format: JSON-compatible; changes are version-specific (see Cache Migration)
- API responses: Added fields are backward compatible (new optional fields don't break clients)

---

## Version-Specific Migrations

### Upgrading from 0.x to 1.0

#### Breaking Changes
- `get_sensor_data()` now returns structured dict instead of raw JSON
- `validate()` method signature changed (added `context` parameter)
- MQTT topic format changed from `dama/sensors/#` to `dama/sensor/#`

#### Migration Steps
1. **Backup database:** `cp /data/ground_truth.db /data/ground_truth.db.backup-v0`
2. **Update code:** `git fetch origin && git checkout v1.0.0`
3. **Update dependencies:** `pip install -r requirements.txt`
4. **Run migrations:** `python3 -m ground_truth migrate --from-version=0.9 --to-version=1.0`
5. **Verify:** `python3 -m ground_truth validate_env` (should pass all checks)
6. **Test:** Run integration tests: `pytest tests/test_ground_truth.py -v`
7. **Deploy:** Replace old image/binary with new version

#### Rollback
If issues occur, rollback to previous version:
```bash
git checkout v0.9.0
systemctl restart ground_truth
# Data is compatible (no schema changes in 0.9→1.0 migration)
```

### Upgrading from 1.x to 2.0

#### Breaking Changes
- Nova authentication changed to use OAuth2 (was bearer token only)
- Database schema: `validation_results` table restructured (new indexes)
- Cache eviction changed from LRU to LRU+TTL hybrid

#### Migration Steps
1. **Backup database:** `cp /data/ground_truth.db /data/ground_truth.db.backup-v1`
2. **Update environment variables:** See Credential Migration section below
3. **Update code:** `git fetch origin && git checkout v2.0.0`
4. **Update dependencies:** `pip install -r requirements.txt && pip install --upgrade nova_mcp`
5. **Run schema migrations:**
   ```bash
   python3 -m ground_truth migrate --from-version=1.9 --to-version=2.0
   # This will:
   # - Add new indices to validation_results table
   # - Create validation_log table for audit trail
   # - Migrate cache format (automatic)
   ```
6. **Verify database integrity:** `sqlite3 /data/ground_truth.db "PRAGMA integrity_check;"`
7. **Test:** Run integration tests: `pytest tests/ -v`
8. **Deploy:** Replace old image/binary with new version

#### Rollback
```bash
git checkout v1.9.0
# Restore database backup (schema changes are one-way):
cp /data/ground_truth.db.backup-v1 /data/ground_truth.db
systemctl restart ground_truth
```

---

## Database Schema Migrations

### Migration Strategy
- Migrations are applied **automatically** on service startup (see `ground_truth.py:_apply_schema_migrations()`)
- Each migration is idempotent (safe to run multiple times)
- Migration history is stored in `schema_migrations` table

### Schema Versions
| Version | Date | Changes | Migration Script |
|---------|------|---------|------------------|
| 1 | 2024-01-01 | Initial: claims, validation_results, sensor_snapshots | v1_initial.sql |
| 2 | 2024-02-15 | Add indices on (claim_id, timestamp_utc) | v2_add_indices.sql |
| 3 | 2024-03-20 | Add validation_log table for audit trail | v3_add_audit_log.sql |
| 4 | 2024-04-10 | Add cache_metadata table | v4_add_cache_metadata.sql |

### Running Migrations Manually
```bash
# List pending migrations
python3 -m ground_truth migrate --list

# Apply specific migration
python3 -m ground_truth migrate --apply-version=3

# Verify current schema version
python3 -m ground_truth migrate --status
```

### Migration Monitoring
Migrations are logged at INFO level:
```
2024-05-01 12:34:56 INFO: Applying migration v2_add_indices.sql
2024-05-01 12:35:01 INFO: Migration v2_add_indices.sql applied successfully (5.3s)
```

If migration fails, error is logged at ERROR level with details:
```
2024-05-01 12:34:56 ERROR: Migration v2_add_indices.sql failed: 
  "table validation_results already has column timestamp_utc"
  (safe to retry; migration is idempotent)
```

---

## Credential Migration

### Environment Variables Changed Across Versions

| Version | Variable | Old Value | New Value | Migration |
|---------|----------|-----------|-----------|-----------|
| 1.0 | NOVA_TOKEN | `token_v1_xxxx` | `token_v2_xxxx` | Obtain new token from Nova admin |
| 1.0 | NOVA_BASE_URL | `http://nova.internal` | `http://100.73.200.19:30850` | Update to public endpoint |
| 2.0 | NOVA_AUTH_TYPE | N/A | `oauth2` | New in 2.0; use OAuth2 instead of bearer |
| 2.0 | NOVA_CLIENT_ID | N/A | `<uuid>` | New in 2.0; obtain from Nova admin |
| 2.0 | NOVA_CLIENT_SECRET | N/A | `<secret>` | New in 2.0; store securely in vault |

### Migration Checklist
- [ ] Obtain new credentials from Nova admin (NOVA_CLIENT_ID, NOVA_CLIENT_SECRET)
- [ ] Update .env file with new variables
- [ ] Verify old credentials are removed from .env
- [ ] Test connectivity: `curl -X POST -d "client_id=$NOVA_CLIENT_ID&client_secret=$NOVA_CLIENT_SECRET" $NOVA_BASE_URL/oauth/token`
- [ ] Restart service: `systemctl restart ground_truth`
- [ ] Monitor logs for "OAuth2 token acquired successfully"

### Securing Credentials
**Do not commit credentials to git:**
```bash
# Use environment variables or secret vaults:
export NOVA_TOKEN=$(vault kv get -field=token secret/nova)
export NOVA_TOTP_SEED=$(vault kv get -field=totp_seed secret/nova)

# Or use .env file (do not commit):
echo "NOVA_TOKEN=xxx" >> .env
echo ".env" >> .gitignore
```

---

## Cache Format Migration

### Cache Location Changes
| Version | Cache Backend | Location | Migration |
|---------|---------------|----------|-----------|
| 1.0 | In-memory dict | RAM | N/A |
| 1.5 | SQLite cache table | `/data/ground_truth.db:cache` | Auto-populated on first load |
| 2.0 | Redis (optional) | `redis://localhost:6379/0` | Manual; use `migrate_cache_to_redis.py` |

### Migrating Cache to Redis
Redis provides better performance and distributed caching for multi-instance deployments.

**Prerequisites:**
- Redis server running at `REDIS_HOST:REDIS_PORT`
- `redis-py` installed: `pip install redis`
- Redis credentials in environment (if auth required)

**Migration:**
```bash
# Dry run (shows what would be migrated)
python3 scripts/migrate_cache_to_redis.py --dry-run

# Actual migration
python3 scripts/migrate_cache_to_redis.py --mode=copy
# (or --mode=move to delete from SQLite after copying)

# Verify migration
redis-cli DBSIZE  # should show cache entries
redis-cli TTL ground_truth:claim:abc123  # check TTL

# Update configuration
export CACHE_BACKEND=redis
export REDIS_HOST=redis.example.com
export REDIS_PORT=6379
export REDIS_DB=0
systemctl restart ground_truth
```

---

## Deployment Environment Migration

### Moving to New Kubernetes Cluster

1. **Backup:** `kubectl exec -it pod/ground-truth-xyz -- sh -c "cp /data/ground_truth.db /data/backup/"`
2. **Export data:** `kubectl cp pod/ground-truth-xyz:/data/ground_truth.db ./ground_truth.db`
3. **Create new PV/PVC** in target cluster with same storage class
4. **Copy data:** Use `kubectl cp` or rsync to restore `ground_truth.db`
5. **Update image:** `kubectl set image deployment/ground-truth ground-truth=myregistry/ground-truth:v2.0`
6. **Apply migrations:** Pod startup runs migrations automatically
7. **Verify:** `kubectl logs -f deployment/ground-truth` (watch for "migration applied" messages)

### Moving to New Physical Server

1. **Backup database:** `rsync -av /data/ground_truth.db backup@new-server:/data/`
2. **Stop on old server:** `systemctl stop ground_truth`
3. **Restore on new server:** `rsync -av backup@old-server:/data/ground_truth.db /data/`
4. **Update DNS/IP:** Point client to new server
5. **Start service:** `systemctl start ground_truth`
6. **Monitor:** `tail -f /var/log/ground_truth.log` (watch for startup messages)

---

## Rollback Procedures

### Rollback to Previous Version
```bash
# 1. Stop service
systemctl stop ground_truth

# 2. Restore database backup (if schema changed)
cp /data/ground_truth.db.backup /data/ground_truth.db

# 3. Revert code
git checkout v1.9.0

# 4. Reinstall dependencies (if needed)
pip install -r requirements.txt

# 5. Restart service
systemctl start ground_truth

# 6. Verify
curl http://localhost:8080/health | jq .status
```

### Rollback with Data Preservation
If you want to keep data from the newer version:
```bash
# 1. Create backup of new schema
cp /data/ground_truth.db /data/ground_truth.db.new-schema

# 2. Restore old schema backup
cp /data/ground_truth.db.old-schema /data/ground_truth.db

# 3. Revert code and restart
git checkout v1.9.0
systemctl restart ground_truth

# 4. Later, investigate why upgrade failed and re-attempt
# (the new-schema backup is available for analysis)
```

---

## Testing Upgrades Locally

### Test Upgrade in Development Environment
```bash
# 1. Create test database with old schema
sqlite3 test_v1.db < schemas/v1_initial.sql

# 2. Check current version
python3 -m ground_truth migrate --status --db=test_v1.db

# 3. Run upgrade
python3 -m ground_truth migrate --apply-all --db=test_v1.db

# 4. Verify final version
python3 -m ground_truth migrate --status --db=test_v1.db

# 5. Run tests against upgraded DB
pytest tests/ --db=test_v1.db -v
```

### Load Testing After Upgrade
```bash
# Run validation load test
python3 tests/load_test.py \
  --host=localhost \
  --port=8080 \
  --requests=1000 \
  --concurrency=10 \
  --duration=60s

# Monitor metrics
watch -n 1 'curl -s http://localhost:8080/health | jq .validation_metrics'
```

---

## Related Documentation

- [HEALTH_CHECK.md](HEALTH_CHECK.md) - Health monitoring during upgrades
- [DEPLOYMENT_CHECKLIST.md](DEPLOY.md) - Pre/post-deployment verification
- [FAILURE_MODES.md](FAILURE_MODES.md) - Troubleshooting migration issues
