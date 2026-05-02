# MQTT Novelty Verifier - Quick Reference Card

## Manifest Overview

**File:** `k8s-mqtt-novelty.yaml`
**Size:** ~24KB (799 lines)
**Resources:** 10 Kubernetes objects

## Quick Deploy Checklist

```bash
# 1. Set environment variables or edit manifest
export MQTT_PASSWORD="your-password"
export MQTT_HOST="mqtt-broker.default.svc.cluster.local"
export MQTT_PORT="1883"
export MQTT_USER="nova-agent"

# 2. Create secret (Option A: manual)
kubectl create secret generic mqtt-credentials \
  --from-literal=password="$MQTT_PASSWORD" \
  --from-literal=mqtt-host="$MQTT_HOST" \
  --from-literal=mqtt-port="$MQTT_PORT" \
  --from-literal=mqtt-user="$MQTT_USER" \
  -n agents

# 3. Apply manifest
kubectl apply -f k8s-mqtt-novelty.yaml

# 4. Verify
kubectl get pods -n agents -l app=mqtt-novelty-verifier
kubectl logs -f -n agents <pod-name> -c mqtt-novelty-handler

# 5. Test (Optional)
kubectl port-forward -n agents svc/mqtt-novelty-verifier 8000:8000
curl http://localhost:8000/health
```

## Manifest Components

| Component | Type | Name | Namespace | Purpose |
|-----------|------|------|-----------|---------|
| Namespace | Namespace | agents | - | Logical isolation |
| Secret | Secret | mqtt-credentials | agents | MQTT credentials & URLs |
| ConfigMap | ConfigMap | mqtt-novelty-config | agents | Python scripts |
| Deployment | Deployment | mqtt-novelty-verifier | agents | Main workload |
| Service | Service | mqtt-novelty-verifier | agents | Network exposure |
| ServiceAccount | ServiceAccount | mqtt-novelty-verifier | agents | Pod identity |
| Role | Role | mqtt-novelty-verifier | agents | Access control |
| RoleBinding | RoleBinding | mqtt-novelty-verifier | agents | Bind role to SA |
| HPA | HorizontalPodAutoscaler | mqtt-novelty-verifier | agents | Auto-scaling |
| PDB | PodDisruptionBudget | mqtt-novelty-verifier | agents | High availability |

## Container Specifications

### Primary: nova-agent
```
Image:        nova-deep-think:latest
Ports:        8000 (http), 8001 (metrics)
CPU Req:      250m
CPU Limit:    1000m
Memory Req:   512Mi
Memory Limit: 2Gi
Probes:       Liveness (/health), Readiness (/ready)
```

### Sidecar: mqtt-novelty-handler
```
Image:        deep-think-mcp:mqtt-novelty-latest
Port:         9090 (metrics)
CPU Req:      100m
CPU Limit:    500m
Memory Req:   128Mi
Memory Limit: 512Mi
Probes:       Liveness (process check), Readiness (process + mqtt)
```

## Secret Keys

| Key | Usage | Source | Example |
|-----|-------|--------|---------|
| `password` | MQTT authentication | Base64-encoded password | `dGVzdA==` → "test" |
| `mqtt-host` | MQTT broker address | Kubernetes DNS or IP | `mosquitto.default.svc.cluster.local` |
| `mqtt-port` | MQTT broker port | Integer as string | `"1883"` |
| `mqtt-user` | MQTT username | Application-specific | `"nova-agent"` |
| `ollama-url` | Ollama API endpoint | Optional URL | `http://ollama.agents:11434` |

## Environment Variables (Sidecar)

### From Secrets
- `MQTT_PASSWORD` ← secret.data.password
- `MQTT_HOST` ← secret.data.mqtt-host
- `MQTT_PORT` ← secret.data.mqtt-port
- `MQTT_USER` ← secret.data.mqtt-user
- `OLLAMA_URL` ← secret.data.ollama-url

### Hardcoded
- `NOVA_URL` = `http://localhost:8000` (same pod)
- `LOG_LEVEL` = `INFO`
- `MQTT_KEEPALIVE` = `60`
- `MQTT_QOS` = `1`
- `NOVELTY_CACHE_SIZE` = `10000`

## Common Commands

```bash
# View deployment status
kubectl get deployment mqtt-novelty-verifier -n agents -o wide

# Watch pod rollout
kubectl rollout status deployment mqtt-novelty-verifier -n agents

# Check pod resources
kubectl top pods -n agents -l app=mqtt-novelty-verifier --containers

# Stream logs (all pods)
kubectl logs -f -n agents -l app=mqtt-novelty-verifier -c mqtt-novelty-handler

# Exec into pod
kubectl exec -it -n agents <pod-name> -c mqtt-novelty-handler -- /bin/bash

# Port-forward to service
kubectl port-forward -n agents svc/mqtt-novelty-verifier 8000:8000 9090:9090

# Delete deployment
kubectl delete deployment mqtt-novelty-verifier -n agents

# View all resources in namespace
kubectl get all -n agents

# Describe pod for events
kubectl describe pod -n agents <pod-name>

# Check HPA status
kubectl get hpa mqtt-novelty-verifier -n agents
kubectl describe hpa mqtt-novelty-verifier -n agents

# Manually scale
kubectl scale deployment mqtt-novelty-verifier --replicas=3 -n agents

# Check RBAC
kubectl get role,rolebinding,serviceaccount -n agents
```

## Probes Explained

### Nova Liveness Probe
```yaml
httpGet:
  path: /health
  port: 8000
initialDelaySeconds: 30  # Wait 30s after start
periodSeconds: 10        # Check every 10s
timeoutSeconds: 5        # Request timeout
failureThreshold: 3      # Kill after 3 failures
```
→ Container restarts if Nova becomes unresponsive

### Nova Readiness Probe
```yaml
httpGet:
  path: /ready
  port: 8000
initialDelaySeconds: 15  # Shorter wait for readiness
periodSeconds: 5         # More frequent checks
timeoutSeconds: 3
failureThreshold: 2
```
→ Pod removed from Service until Nova is ready

### MQTT Liveness Probe
```yaml
exec:
  command:
  - /bin/sh
  - -c
  - ps aux | grep -q "[p]ython.*mqtt_novelty" && exit 0 || exit 1
initialDelaySeconds: 30
periodSeconds: 15
timeoutSeconds: 5
failureThreshold: 3
```
→ Container restarts if process dies

### MQTT Readiness Probe
```yaml
exec:
  command:
  - /bin/sh
  - -c
  - ps aux | grep -q "[p]ython.*mqtt_novelty" && exit 0 || exit 1
initialDelaySeconds: 20
periodSeconds: 10
timeoutSeconds: 5
failureThreshold: 2
```
→ Pod removed from Service if MQTT handler dies

## Volumes

| Name | Type | Size | Mounted By | Path |
|------|------|------|-----------|------|
| mqtt-scripts | ConfigMap | ∞ | mqtt-novelty-handler | /usr/local/bin/mqtt-novelty |
| logs | emptyDir | 500Mi | both | /var/log/* |
| runtime-data | emptyDir | 100Mi | both | /var/run/* |
| tmp | emptyDir | 200Mi | mqtt-novelty-handler | /tmp |
| config | ConfigMap | ∞ | nova-agent | /etc/nova |

## Networking

| Component | Protocol | Port | Purpose |
|-----------|----------|------|---------|
| Service nova-http | TCP | 8000 | Nova HTTP API |
| Service nova-metrics | TCP | 8001 | Nova Prometheus metrics |
| Service mqtt-metrics | TCP | 9090 | MQTT sidecar metrics |
| MQTT Broker | TCP/TLS | 1883/8883 | External MQTT connectivity |

## MQTT Topics

### Subscribed (Input)
- `deep-think/jobs/+/result` - Thinking results
- `deep-think/jobs/+/complete` - Completion notifications

### Published (Output)
- `deep-think/jobs/{job_id}/novelty-verified` - Verification results

## Auto-Scaling Configuration

```yaml
minReplicas: 2          # Minimum pods
maxReplicas: 5          # Maximum pods
cpuUtilization: 70%     # Scale-up target
memoryUtilization: 80%  # Scale-up target
```

Scaling rules:
- **Scale Up:** Fast (0s stabilization) when crossing thresholds
- **Scale Down:** Slow (300s stabilization) to prevent thrashing
- **Policies:** Both percentage and pod-based

## Example: Secrets Configuration

### Base64 Encode Password
```bash
echo -n "mypassword123" | base64
# Output: bXlwYXNzd29yZDEyMw==
```

### Edit Manifest
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: mqtt-credentials
  namespace: agents
type: Opaque
data:
  password: bXlwYXNzd29yZDEyMw==  # ← Base64 encoded
stringData:
  mqtt-host: "mosquitto.agents.svc.cluster.local"
  mqtt-port: "1883"
  mqtt-user: "nova-agent"
  ollama-url: "http://ollama.agents.svc.cluster.local:11434"
```

## Security Features

✓ Non-root container (UID 1000)
✓ Read-only root filesystem
✓ Dropped Linux capabilities
✓ No privilege escalation
✓ Pod security context
✓ RBAC with minimal permissions
✓ Secrets mounted as volumes (optional security enhancement)
✓ Resource limits prevent DOS

## Monitoring

### Prometheus Scrape Targets
- `mqtt-novelty-verifier:8001/metrics` (Nova metrics)
- `mqtt-novelty-verifier:9090/metrics` (MQTT metrics)

### Key Metrics
- `container_cpu_usage_seconds_total`
- `container_memory_usage_bytes`
- `mqtt_messages_processed_total`
- `mqtt_connection_state`
- `novelty_verification_duration_seconds`

## Troubleshooting Quick Links

| Issue | Command |
|-------|---------|
| Pods stuck in Pending | `kubectl describe pod -n agents <pod>` |
| CrashLoopBackOff | `kubectl logs -n agents <pod> --previous` |
| MQTT not connecting | `kubectl logs -n agents <pod> -c mqtt-novelty-handler \| grep -i mqtt` |
| High CPU usage | `kubectl top pods -n agents --containers` |
| OOMKilled | Check Memory Limit vs actual usage |
| Probe failures | `kubectl get events -n agents --sort-by=.lastTimestamp` |

## Documentation Files

- **k8s-mqtt-novelty.yaml** - Complete manifest (this file)
- **SECRETS_SETUP.md** - Secret configuration guide
- **DEPLOYMENT_GUIDE.md** - Detailed deployment instructions
- **QUICKREF.md** - This quick reference

## Key Points

1. **Two containers in one pod** - Nova + MQTT handler share network/storage
2. **Local communication** - MQTT handler calls Nova via localhost:8000
3. **Secret-based config** - All credentials from k8s Secret
4. **Scripts in ConfigMap** - Python scripts mounted at runtime
5. **High availability** - 2 replicas, anti-affinity, HPA enabled
6. **Resource constrained** - Sidecar limited to 500m CPU / 512Mi mem
7. **Health monitored** - Liveness and readiness probes for both containers
8. **Auto-scaling** - HPA scales 2-5 replicas based on CPU/memory
9. **Production-ready** - PDB, security context, RBAC configured
10. **Not yet applied** - Manifest created but not deployed to cluster
