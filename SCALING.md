# Scaling Considerations

## Overview

Guidelines and recommendations for scaling ground_truth.py from single-instance to multi-instance, high-throughput production deployments.

---

## Current Architecture Limits

### Single-Instance Deployment

**Current State:**
- In-memory cache (non-distributed)
- Single MQTT connection
- Single Nova connection
- SQLite database (single writer)
- Max throughput: ~100 validations/second

**Bottlenecks:**
1. **Cache is in-process only** — not shared between instances
2. **MQTT single connection** — connection limits apply
3. **Nova rate limits** — 100 req/sec per token
4. **Database contention** — SQLite single-writer limit
5. **Memory limits** — Cache size bounded by available RAM

---

## Scaling Strategy

### Phase 1: Single Instance with Better Resources (0-1000 req/sec)

**Recommendations:**
- Increase cache size: `MQTT_CACHE_SIZE_LIMIT=2GB` (if 8GB+ available)
- Increase database page size: `PRAGMA page_size=4096`
- Enable WAL mode: `PRAGMA journal_mode=WAL` (better concurrency)
- Add read replicas for reporting queries
- Increase Nova rate limit (request new token with higher limit)

**Implementation:**
```bash
# Update cache sizes
export MQTT_CACHE_SIZE_LIMIT=2147483648  # 2GB
export NOVA_CACHE_SIZE_LIMIT=1073741824  # 1GB

# Enable database optimizations
sqlite3 /data/ground_truth.db << EOF
PRAGMA page_size=4096;
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=10000;
EOF

# Increase Nova rate limit (contact Nova admin)
# Request: NOVA_RATE_LIMIT_RPS=500 (was 100)
```

**Cost:** Minimal (more RAM, maybe larger instance)  
**Throughput:** 100 → 500 req/sec  
**Implementation Time:** 1-2 hours

---

### Phase 2: Distributed Cache with Redis (1000-10000 req/sec)

**Problem:** Single instance cache fills up; multi-instance validation is incoherent

**Solution:** Use Redis for distributed cache

**Architecture:**
```
Ground_truth instance 1 ←→ Redis cluster
Ground_truth instance 2 ←→ Redis cluster  (shared cache)
Ground_truth instance 3 ←→ Redis cluster
```

**Implementation:**

1. **Deploy Redis cluster:**
   ```bash
   # Option A: Docker
   docker run -d --name redis-cache \
     -p 6379:6379 \
     -v redis-data:/data \
     redis:7-alpine

   # Option B: Kubernetes
   kubectl apply -f redis-cluster.yaml -n agents
   ```

2. **Install Redis client:**
   ```bash
   pip install redis
   ```

3. **Update ground_truth.py:**
   ```python
   import redis
   
   CACHE_BACKEND = os.getenv("CACHE_BACKEND", "memory")  # or "redis"
   REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
   REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
   REDIS_DB = int(os.getenv("REDIS_DB", "0"))
   
   if CACHE_BACKEND == "redis":
       redis_client = redis.Redis(
           host=REDIS_HOST,
           port=REDIS_PORT,
           db=REDIS_DB,
           decode_responses=True
       )
   ```

4. **Configure .env:**
   ```bash
   export CACHE_BACKEND=redis
   export REDIS_HOST=redis-cache.default.svc.cluster.local
   export REDIS_PORT=6379
   export REDIS_DB=0
   ```

5. **Test cache coherence:**
   ```bash
   # Instance 1: cache a claim
   curl -X POST http://instance1:8080/validate -d '...'
   
   # Instance 2: should retrieve from Redis
   curl -X GET http://instance2:8080/cache/claim-123
   # Should return cached result from instance 1
   ```

**Benefits:**
- Cache shared between all instances
- Instances can be added/removed without cache loss
- Redis provides TTL, eviction policies
- Redis is horizontally scalable

**Cost:** Redis cluster (managed service ~$100-500/mo)  
**Throughput:** 500 → 5000 req/sec  
**Implementation Time:** 3-5 days (including testing)

---

### Phase 3: Database Connection Pooling (5000-50000 req/sec)

**Problem:** SQLite single-writer limit; database contention increases

**Solution 1: PostgreSQL with Connection Pooling**

```
Ground_truth instances (n=10) ←→ PgBouncer (connection pool) ←→ PostgreSQL
```

**Implementation:**

1. **Migrate database:**
   ```bash
   # Dump from SQLite
   sqlite3 /data/ground_truth.db .dump > schema.sql
   
   # Restore to PostgreSQL
   psql -h postgres-host -U ground_truth ground_truth < schema.sql
   ```

2. **Install PostgreSQL driver:**
   ```bash
   pip install psycopg2-binary
   ```

3. **Update connection string:**
   ```python
   DB_ENGINE = os.getenv("DB_ENGINE", "sqlite")  # or "postgres"
   
   if DB_ENGINE == "postgres":
       import psycopg2
       conn = psycopg2.connect(
           host=os.getenv("DB_HOST"),
           port=int(os.getenv("DB_PORT", "5432")),
           database=os.getenv("DB_NAME", "ground_truth"),
           user=os.getenv("DB_USER"),
           password=os.getenv("DB_PASSWORD")
       )
   ```

4. **Deploy PgBouncer connection pool:**
   ```ini
   # pgbouncer.ini
   [databases]
   ground_truth = host=postgresql port=5432 dbname=ground_truth

   [pgbouncer]
   pool_mode = transaction
   max_client_conn = 1000
   default_pool_size = 25
   ```

**Benefits:**
- Supports hundreds of concurrent connections
- Multi-writer capability (true ACID)
- Connection pooling reduces latency
- Better query optimization

**Cost:** PostgreSQL managed service (~$500-2000/mo)  
**Throughput:** 5000 → 50000 req/sec  
**Implementation Time:** 1-2 weeks (including testing, migration)

---

### Phase 4: Load Balancing (50000+ req/sec)

**Problem:** Single Nova endpoint rate limit; need to distribute requests

**Solution:** Multiple Nova tokens + round-robin load balancing

```
Ground_truth instances ←→ Load Balancer ←→ Nova endpoints (n=3)
                                      ↓
                                  Nova backend
```

**Implementation:**

1. **Request additional Nova tokens from admin:**
   ```bash
   NOVA_TOKEN_1=token_xxxx
   NOVA_TOKEN_2=token_yyyy
   NOVA_TOKEN_3=token_zzzz
   ```

2. **Implement token rotation in ground_truth.py:**
   ```python
   NOVA_TOKENS = [
       os.getenv("NOVA_TOKEN_1"),
       os.getenv("NOVA_TOKEN_2"),
       os.getenv("NOVA_TOKEN_3"),
   ]
   
   def _get_nova_headers_round_robin():
       """Rotate through Nova tokens for load distribution."""
       token = NOVA_TOKENS[_rotation_index % len(NOVA_TOKENS)]
       _rotation_index += 1
       return {"Authorization": f"Bearer {token}", ...}
   ```

3. **Enable circuit breaker per token:**
   ```python
   # If token hits 429 rate limit, skip to next token
   if response.status_code == 429:
       _token_circuit_breaker[token] = time.time() + 60  # 60s cooldown
       return _get_nova_headers_round_robin()  # try next token
   ```

**Benefits:**
- Distributes load across multiple Nova tokens
- Automatic failover if one token is rate-limited
- Linear scaling with number of tokens

**Cost:** Minimal (just additional Nova tokens)  
**Throughput:** 50000 → 500000 req/sec  
**Implementation Time:** 2-3 days

---

## Horizontal Scaling (Adding Instances)

### Single Instance → N Instances

**Prerequisites:**
- Shared cache (Redis)
- Shared database (PostgreSQL)
- Load balancer in front

**Deployment:**

```bash
# Deploy 3 instances of ground_truth.py
kubectl scale deployment/ground-truth --replicas=3

# Verify all instances are running
kubectl get pods -l app=ground-truth
# Should show 3 pods in Running state

# Test load distribution
for i in {1..100}; do
  curl http://ground-truth-load-balancer/health | jq .validation_metrics.total_validations
done
# Metrics should be balanced across instances
```

**Monitoring:**

```bash
# Watch instance count
watch -n 2 'kubectl get pods -l app=ground-truth | wc -l'

# Watch validation throughput per instance
kubectl exec -it <pod> -- curl http://localhost:8080/health | jq .validation_metrics.validations_last_hour

# Watch cache hit rate (should be >80%)
kubectl exec -it <pod> -- curl http://localhost:8080/health | jq .cache_status.hit_rate
```

**Auto-Scaling Rules:**

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: ground-truth-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: ground-truth
  minReplicas: 3
  maxReplicas: 20
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
  - type: Resource
    resource:
      name: memory
      target:
        type: Utilization
        averageUtilization: 80
```

---

## Performance Tuning

### CPU Optimization

**Problem:** CPU usage high due to validation logic  
**Solution:**
- Profile with `py-spy`; identify hot spots
- Use compiled extensions for CPU-bound code (Cython, C extensions)
- Cache expensive computations (contradictions, semantic similarity)
- Batch Nova requests (validate 10 claims per API call, not 10 separate calls)

### Memory Optimization

**Problem:** Memory usage grows with instance count  
**Solution:**
- Tune cache sizes based on hit rate: target >85%
- Use TTL-based cache eviction (not just LRU)
- Profile with `memory_profiler`; identify memory leaks
- Use generators for large result sets (not lists)

### Network Optimization

**Problem:** Latency high due to round-trips to Nova/MQTT  
**Solution:**
- Batch MQTT subscriptions (fewer connections)
- Connection pooling for database
- HTTP/2 multiplexing with Nova (if supported)
- Local caching of Nova responses (1-hour TTL)

---

## Capacity Planning

### Resource Estimates

| Throughput | Instances | CPU/Instance | RAM/Instance | Cache (Redis) | Database |
|------------|-----------|--------------|--------------|---------------|----------|
| 100 req/s | 1 | 1 core | 4GB | N/A | SQLite 10GB |
| 500 req/s | 2 | 2 cores | 8GB | 10GB | PostgreSQL 20GB |
| 5k req/s | 5 | 4 cores | 16GB | 50GB | PostgreSQL 100GB |
| 50k req/s | 10 | 8 cores | 32GB | 200GB | PostgreSQL 500GB |

### Cost Estimates

| Throughput | Compute | Cache | Database | Total |
|------------|---------|-------|----------|-------|
| 100 req/s | $100/mo | — | — | $100/mo |
| 500 req/s | $500/mo | $100/mo | $200/mo | $800/mo |
| 5k req/s | $2k/mo | $300/mo | $1k/mo | $3.3k/mo |
| 50k req/s | $10k/mo | $1k/mo | $5k/mo | $16k/mo |

---

## Migration Path

### Recommended Progression

1. **Week 1:** Phase 1 (better resources) — low risk, quick wins
2. **Week 2-3:** Phase 2 (Redis) — medium risk, significant gain
3. **Week 4-6:** Phase 3 (PostgreSQL) — higher risk, major scaling
4. **Week 7-8:** Phase 4 (Nova load balancing) — low risk, approach limit

### Rollback at Each Phase

```bash
# Phase 1 rollback: Just reduce resource sizes
export MQTT_CACHE_SIZE_LIMIT=512000000
systemctl restart ground_truth

# Phase 2 rollback: Switch back to in-memory cache
export CACHE_BACKEND=memory
systemctl restart ground_truth

# Phase 3 rollback: Restore from SQLite backup, switch connection string
cp /backup/ground_truth.db.backup /data/ground_truth.db
export DB_ENGINE=sqlite
systemctl restart ground_truth

# Phase 4 rollback: Use single Nova token
unset NOVA_TOKEN_2 NOVA_TOKEN_3
systemctl restart ground_truth
```

---

## Related Documentation

- [FAILURE_MODES.md](FAILURE_MODES.md) - Scaling failure scenarios
- [MONITORING.md](MONITORING.md) - Metrics for capacity planning
- [HEALTH_CHECK.md](HEALTH_CHECK.md) - Health checks under load
