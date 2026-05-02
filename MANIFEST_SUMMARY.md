# Kubernetes Manifest Summary: MQTT Novelty Verifier

**Status:** ✅ Complete and Ready for Deployment (NOT YET APPLIED)

## File Information

| Property | Value |
|----------|-------|
| **File Path** | `/home/rjmendez/development/deep_think_mcp/k8s-mqtt-novelty.yaml` |
| **File Size** | ~24 KB |
| **Total Lines** | 799 |
| **YAML Documents** | 10 |
| **Created** | 2024 |
| **Namespace** | `agents` |
| **Deployment Name** | `mqtt-novelty-verifier` |

## What's Inside

### 1. **Namespace: agents**
- Logical isolation for all resources
- Labels for organization

### 2. **Secret: mqtt-credentials** 
- MQTT broker connection details
- Credentials for authentication
- Template with placeholder values (must customize before deploy)

**Keys to configure:**
```
password      → Base64-encoded MQTT password (default: "changeme")
mqtt-host     → MQTT broker hostname (default: "mqtt-broker.default.svc.cluster.local")
mqtt-port     → MQTT broker port (default: "1883")
mqtt-user     → MQTT username (default: "nova-agent")
ollama-url    → Optional Ollama API URL
```

### 3. **ConfigMap: mqtt-novelty-config**
Contains embedded Python scripts:

#### run_mqtt_novelty_service.py
- Main service entry point
- Connects to MQTT broker
- Subscribes to deep-think job results
- Calls Nova verification endpoint
- Publishes novelty verification results back to MQTT
- Includes health checking and reconnection logic

**Subscribes to:**
- `deep-think/jobs/+/result` - Thinking results from Nova
- `deep-think/jobs/+/complete` - Completion notifications

**Publishes to:**
- `deep-think/jobs/{job_id}/novelty-verified` - Verification outcomes

#### mqtt_novelty_handler.py
- Core novelty verification library
- `NoveltyVerifier` class for duplicate detection
- `NoveltyCheckResult` dataclass for results
- Local caching and history store support
- SHA256 reasoning hash computation

### 4. **Deployment: mqtt-novelty-verifier**
Main workload with two containers in one pod:

#### Container 1: nova-agent (Primary)
```yaml
Image:              nova-deep-think:latest
Ports:              8000 (HTTP), 8001 (metrics)
CPU Request:        250m
CPU Limit:          1000m
Memory Request:     512Mi
Memory Limit:       2Gi
Liveness Probe:     HTTP GET /health (30s initial, 10s period)
Readiness Probe:    HTTP GET /ready (15s initial, 5s period)
Security Context:   Non-root (UID 1000), read-only FS, no capabilities
```

#### Container 2: mqtt-novelty-handler (Sidecar)
```yaml
Image:              deep-think-mcp:mqtt-novelty-latest
Port:               9090 (Prometheus metrics)
CPU Request:        100m
CPU Limit:          500m
Memory Request:     128Mi
Memory Limit:       512Mi
Liveness Probe:     Process check via exec (30s initial, 15s period)
Readiness Probe:    Process check + MQTT connectivity (20s initial, 10s period)
Security Context:   Non-root (UID 1000), read-only FS, no capabilities
```

#### Deployment Configuration
```yaml
Namespace:          agents
Replicas:           2 (for high availability)
Strategy:           RollingUpdate (1 max surge, 0 max unavailable)
Termination Grace:  30 seconds
Service Account:    mqtt-novelty-verifier
```

#### Shared Volumes
- `mqtt-scripts` (ConfigMap) → `/usr/local/bin/mqtt-novelty`
- `logs` (emptyDir, 500Mi) → `/var/log/`
- `runtime-data` (emptyDir, 100Mi) → `/var/run/`
- `tmp` (emptyDir, 200Mi) → `/tmp`

#### Pod Affinity
```yaml
Pod Anti-Affinity:  Prefer to spread replicas across different nodes
Topology Key:       kubernetes.io/hostname
```

### 5. **Service: mqtt-novelty-verifier**
ClusterIP service exposing three ports:

```yaml
Type:               ClusterIP
Port Mapping:
  nova-http:        8000 → 8000 (Nova HTTP API)
  nova-metrics:     8001 → 8001 (Nova Prometheus metrics)
  mqtt-metrics:     9090 → 9090 (MQTT sidecar metrics)
```

**Prometheus Annotations:**
```yaml
prometheus.io/scrape: "true"
prometheus.io/port:   "9090"
prometheus.io/path:   "/metrics"
```

### 6. **ServiceAccount: mqtt-novelty-verifier**
Provides pod identity for RBAC

### 7. **Role: mqtt-novelty-verifier**
Minimal RBAC permissions:
- `get, list, watch` on ConfigMap `mqtt-novelty-config`
- `get` on Secret `mqtt-credentials`

### 8. **RoleBinding: mqtt-novelty-verifier**
Binds Role to ServiceAccount

### 9. **HorizontalPodAutoscaler: mqtt-novelty-verifier** (Optional)
Automatic scaling configuration:

```yaml
Min Replicas:           2
Max Replicas:           5
Scale-up Policy:        
  - 100% increase in 30s OR
  - +2 pods in 60s
Scale-down Policy:      
  - 50% decrease in 60s
  - Stabilization: 300s
Metrics:
  - CPU Utilization: 70%
  - Memory Utilization: 80%
```

### 10. **PodDisruptionBudget: mqtt-novelty-verifier** (Optional)
High-availability protection:
```yaml
Min Available:   1 pod
Effect:          Prevents all pods from being evicted simultaneously
```

## Architecture Diagram

```
┌──────────────────────────────────────────────────────────────┐
│ Kubernetes Cluster (Oxalis k3s)                              │
│                                                               │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ Namespace: agents                                      │  │
│  │                                                         │  │
│  │  ┌──────────────────────────────────────────────────┐ │  │
│  │  │ Deployment: mqtt-novelty-verifier (2 replicas)  │ │  │
│  │  │                                                   │ │  │
│  │  │  ┌──────────────────────────────────────────┐   │ │  │
│  │  │  │ Pod #1                                   │   │ │  │
│  │  │  ├──────────────────────────────────────────┤   │ │  │
│  │  │  │ Container: nova-agent                    │   │ │  │
│  │  │  │  ├─ Image: nova-deep-think:latest       │   │ │  │
│  │  │  │  ├─ Port: 8000, 8001                     │   │ │  │
│  │  │  │  └─ Probes: /health, /ready              │   │ │  │
│  │  │  │                                           │   │ │  │
│  │  │  │ Container: mqtt-novelty-handler (sidecar)│   │ │  │
│  │  │  │  ├─ Image: deep-think-mcp:mqtt-novelty-...│   │ │  │
│  │  │  │  ├─ Port: 9090                           │   │ │  │
│  │  │  │  └─ Probes: process, mqtt connectivity   │   │ │  │
│  │  │  │                                           │   │ │  │
│  │  │  │ Shared:                                  │   │ │  │
│  │  │  │  ├─ Volumes: logs, runtime-data, tmp    │   │ │  │
│  │  │  │  └─ Network namespace (localhost comms) │   │ │  │
│  │  │  └──────────────────────────────────────────┘   │ │  │
│  │  │                                                   │ │  │
│  │  │  ┌──────────────────────────────────────────┐   │ │  │
│  │  │  │ Pod #2 (similar to Pod #1)               │   │ │  │
│  │  │  │ (Anti-affinity: different node if avail.)│   │ │  │
│  │  │  └──────────────────────────────────────────┘   │ │  │
│  │  └──────────────────────────────────────────────────┘ │  │
│  │                          │                            │  │
│  │  ┌──────────────────────▼────────────────────────┐  │  │
│  │  │ Service: mqtt-novelty-verifier (ClusterIP)    │  │  │
│  │  │  ├─ 8000/tcp → nova-http                      │  │  │
│  │  │  ├─ 8001/tcp → nova-metrics                   │  │  │
│  │  │  └─ 9090/tcp → mqtt-metrics                   │  │  │
│  │  └──────────────────────────────────────────────┘  │  │
│  │                          │                            │  │
│  │  ┌──────────────────────▼────────────────────────┐  │  │
│  │  │ ConfigMap: mqtt-novelty-config                │  │  │
│  │  │  ├─ run_mqtt_novelty_service.py               │  │  │
│  │  │  └─ mqtt_novelty_handler.py                   │  │  │
│  │  └──────────────────────────────────────────────┘  │  │
│  │                                                      │  │
│  │  ┌──────────────────────────────────────────────┐  │  │
│  │  │ Secret: mqtt-credentials                      │  │  │
│  │  │  ├─ password (base64-encoded)                 │  │  │
│  │  │  ├─ mqtt-host                                 │  │  │
│  │  │  ├─ mqtt-port                                 │  │  │
│  │  │  ├─ mqtt-user                                 │  │  │
│  │  │  └─ ollama-url (optional)                     │  │  │
│  │  └──────────────────────────────────────────────┘  │  │
│  │                                                      │  │
│  │  ┌──────────────────────────────────────────────┐  │  │
│  │  │ Optional: HPA (2-5 replicas, CPU/Mem target) │  │  │
│  │  │ Optional: PDB (min 1 available)               │  │  │
│  │  └──────────────────────────────────────────────┘  │  │
│  │                                                      │  │
│  │  ┌──────────────────────────────────────────────┐  │  │
│  │  │ RBAC:                                         │  │  │
│  │  │  ├─ ServiceAccount: mqtt-novelty-verifier    │  │  │
│  │  │  ├─ Role: read ConfigMap, Secret             │  │  │
│  │  │  └─ RoleBinding: bind Role to SA             │  │  │
│  │  └──────────────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────────────┘  │
│                                                           │
└──────────────────────────────────────────────────────────┘
         │                                  │
         │                                  │
         ▼                                  ▼
    ┌─────────────────────┐    ┌──────────────────────┐
    │ MQTT Broker         │    │ External Services    │
    │ (mosquitto,etc)     │    │ ├─ Ollama API        │
    │ Port: 1883/8883     │    │ └─ Other APIs        │
    └─────────────────────┘    └──────────────────────┘
```

## Data Flow

```
1. THINKING RESULT GENERATION
   Nova generates deep-thinking result
   └─→ Publishes to MQTT: deep-think/jobs/{job_id}/result

2. MQTT MONITORING
   MQTT sidecar subscribes to: deep-think/jobs/+/result
   Receives JSON: {job_id, thinking_output, metadata}

3. NOVELTY VERIFICATION
   sidecar calls NoveltyVerifier.check_novelty()
   ├─ Check local cache
   ├─ Check history store (if configured)
   └─ Return: is_novel, confidence, reasoning_hash

4. NOVA INTEGRATION (Optional)
   sidecar makes HTTP POST to localhost:8000/verify
   Request: {job_id, result, thinking_output}
   Response: {verification_result}

5. RESULT PUBLICATION
   sidecar publishes to MQTT: deep-think/jobs/{job_id}/novelty-verified
   Payload: {job_id, is_novel, confidence, reasoning_hash, timestamp}

6. HEALTH CHECKS
   Every 30 seconds:
   ├─ Check MQTT connection status
   ├─ Verify Nova /health endpoint
   └─ Perform reconnection if needed
```

## Environment Variables Reference

### MQTT Sidecar Container
| Variable | Source | Default/Required | Purpose |
|----------|--------|------------------|---------|
| `MQTT_HOST` | Secret | Required | MQTT broker hostname |
| `MQTT_PORT` | Secret | "1883" | MQTT broker port |
| `MQTT_USER` | Secret | "nova-agent" | MQTT username |
| `MQTT_PASSWORD` | Secret | Required | MQTT password |
| `NOVA_URL` | Hardcoded | `http://localhost:8000` | Nova API endpoint |
| `OLLAMA_URL` | Secret | Optional | Ollama API endpoint |
| `LOG_LEVEL` | Hardcoded | "INFO" | Logging verbosity |
| `MQTT_KEEPALIVE` | Hardcoded | "60" | MQTT keepalive (seconds) |
| `MQTT_QOS` | Hardcoded | "1" | MQTT QoS level |
| `NOVELTY_CACHE_SIZE` | Hardcoded | "10000" | Local cache size |

### Nova Container
| Variable | Value | Purpose |
|----------|-------|---------|
| `NOVA_LISTEN_ADDR` | `0.0.0.0:8000` | HTTP server binding |
| `NOVA_METRICS_ADDR` | `0.0.0.0:8001` | Metrics server binding |
| `OLLAMA_URL` | From Secret | Ollama endpoint |
| `LOG_LEVEL` | "INFO" | Logging verbosity |
| `ENVIRONMENT` | "production" | Deployment environment |

## Customization Points

### Before Deployment

1. **Update Secret Values**
   - Edit `mqtt-credentials` Secret section
   - Provide actual MQTT broker credentials
   - Base64-encode the password

2. **Image Names**
   - Update `nova-deep-think:latest` if using different tag
   - Update `deep-think-mcp:mqtt-novelty-latest` if using different tag

3. **Resource Limits**
   - Adjust CPU/memory requests based on cluster capacity
   - Sidecar: currently 100m/128Mi request, 500m/512Mi limit
   - Nova: currently 250m/512Mi request, 1000m/2Gi limit

4. **Replica Count**
   - Default: 2 replicas (for HA)
   - HPA can scale 2-5
   - Change `spec.replicas` for different default

5. **MQTT Topics**
   - Edit subscription topics in ConfigMap script if needed
   - Currently subscribes to: `deep-think/jobs/+/result`

6. **Log Locations**
   - Logs in emptyDir (volatile)
   - For persistent logs, replace emptyDir with PVC

7. **Health Check Intervals**
   - Liveness probes: 30s initial, 15s/10s period
   - Readiness probes: 15-20s initial, 5-10s period
   - Adjust `initialDelaySeconds` and `periodSeconds` if needed

## Deployment Checklist

- [ ] Read SECRETS_SETUP.md
- [ ] Prepare MQTT credentials (host, port, username, password)
- [ ] Base64 encode MQTT password
- [ ] Edit Secret section in manifest with actual values
- [ ] Verify images exist in registry
- [ ] Check cluster has sufficient CPU/memory resources
- [ ] Verify MQTT broker is accessible from k3s cluster
- [ ] Create agents namespace: `kubectl create namespace agents`
- [ ] Apply manifest: `kubectl apply -f k8s-mqtt-novelty.yaml`
- [ ] Verify deployment: `kubectl get deployment -n agents`
- [ ] Check pods: `kubectl get pods -n agents`
- [ ] View logs: `kubectl logs -f -n agents <pod-name> -c mqtt-novelty-handler`
- [ ] Test connectivity: `kubectl port-forward svc/mqtt-novelty-verifier 8000:8000`
- [ ] Verify metrics endpoint: `curl http://localhost:9090/metrics`

## Important Notes

✅ **Manifest is YAML-valid and ready for deployment**
❌ **Manifest is NOT yet applied to cluster**
⚠️ **Requires secret configuration before deployment**
📝 **Template values (password: "changeme") must be replaced**

## Related Documentation

1. **k8s-mqtt-novelty.yaml** - Complete manifest (799 lines, 10 documents)
2. **SECRETS_SETUP.md** - Secret configuration guide with 5 options
3. **DEPLOYMENT_GUIDE.md** - Detailed step-by-step deployment
4. **QUICKREF.md** - Quick reference card and common commands
5. **MANIFEST_SUMMARY.md** - This file

## Additional Resources

- [Kubernetes Deployment Documentation](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)
- [Kubernetes Pod Documentation](https://kubernetes.io/docs/concepts/workloads/pods/)
- [Kubernetes Service Documentation](https://kubernetes.io/docs/concepts/services-networking/service/)
- [k3s Documentation](https://docs.k3s.io/)
- [MQTT Protocol Specification](https://mqtt.org/)
