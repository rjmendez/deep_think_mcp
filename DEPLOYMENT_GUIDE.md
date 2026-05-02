# MQTT Novelty Verifier - Kubernetes Deployment Guide

## Overview

This manifest deploys the MQTT novelty handler as a sidecar container alongside Nova on Oxalis k3s cluster. The deployment verifies the novelty of deep-thinking results in real-time by monitoring MQTT topics.

## Manifest Structure

The complete manifest (`k8s-mqtt-novelty.yaml`) contains the following Kubernetes resources in order:

### 1. Namespace (agents)
- Creates the `agents` namespace for all related resources
- Labels for organization and targeting

### 2. Secret: mqtt-credentials
- **Type:** Opaque
- **Keys:**
  - `password`: Base64-encoded MQTT broker password
  - `mqtt-host`: MQTT broker hostname/IP
  - `mqtt-port`: MQTT broker port number
  - `mqtt-user`: MQTT authentication username
  - `ollama-url`: Optional Ollama API endpoint (if not co-located)

### 3. ConfigMap: mqtt-novelty-config
- **Data Keys:**
  - `mqtt_novelty_handler.py`: Core library for novelty verification
    - `NoveltyVerifier` class for checking if results are novel
    - Local caching and history store support
    - Handles duplicate detection
  - `run_mqtt_novelty_service.py`: Main service entry point
    - MQTT client with connection management
    - Subscribes to `deep-think/jobs/+/result` and `deep-think/jobs/+/complete`
    - Integrates with Nova verification endpoint
    - Publishes verification results back to MQTT
    - Health checking and reconnection logic

### 4. Deployment: mqtt-novelty-verifier
The core deployment with two containers:

#### Primary Container: nova-agent
- **Image:** `nova-deep-think:latest`
- **Port:** 8000 (HTTP API), 8001 (metrics)
- **Probes:**
  - Liveness: `/health` endpoint check
  - Readiness: `/ready` endpoint check
- **Resources:**
  - Requests: 250m CPU, 512Mi memory
  - Limits: 1000m CPU, 2Gi memory
- **Security:** Non-root user, read-only filesystem, dropped capabilities

#### Sidecar Container: mqtt-novelty-handler
- **Image:** `deep-think-mcp:mqtt-novelty-latest`
- **Port:** 9090 (metrics endpoint)
- **Environment Variables:**
  - MQTT connection parameters (from secret)
  - Service URLs for Nova and Ollama
  - Logging configuration
  - Cache and keepalive settings
- **Probes:**
  - Liveness: Process check (`ps aux | grep python.*mqtt_novelty`)
  - Readiness: Process running + optional MQTT connectivity
- **Resources:**
  - Requests: 100m CPU, 128Mi memory
  - Limits: 500m CPU, 512Mi memory
- **Security:** Non-root user, read-only filesystem, dropped capabilities

#### Volumes
- **mqtt-scripts** (ConfigMap): Python scripts mounted at `/usr/local/bin/mqtt-novelty`
- **logs** (emptyDir): For runtime logs (500Mi limit)
- **runtime-data** (emptyDir): For inter-process communication (100Mi limit)
- **tmp** (emptyDir): Temporary directory (200Mi limit)
- **config** (ConfigMap): Optional additional configuration

#### Pod Configuration
- **Replicas:** 2 (high availability)
- **Strategy:** RollingUpdate with 1 max surge, 0 max unavailable
- **Security:** RunAsNonRoot, runAsUser: 1000
- **Affinity:** Pod anti-affinity preferred (spread across nodes)
- **Termination Grace:** 30 seconds

### 5. Service: mqtt-novelty-verifier
- **Type:** ClusterIP
- **Ports:**
  - `nova-http`: 8000 → 8000 (Nova API)
  - `nova-metrics`: 8001 → 8001 (Nova metrics)
  - `mqtt-metrics`: 9090 → 9090 (MQTT sidecar metrics)
- **Annotations:** Prometheus scrape configuration
- **Session Affinity:** None (can be changed to ClientIP if needed)

### 6. ServiceAccount: mqtt-novelty-verifier
- Service account for pod identity and RBAC

### 7. Role: mqtt-novelty-verifier
- Minimal RBAC permissions:
  - Get/list/watch ConfigMaps (mqtt-novelty-config only)
  - Get Secrets (mqtt-credentials only)

### 8. RoleBinding: mqtt-novelty-verifier
- Binds Role to ServiceAccount

### 9. HorizontalPodAutoscaler (Optional)
- **Min Replicas:** 2
- **Max Replicas:** 5
- **Metrics:**
  - CPU: 70% utilization target
  - Memory: 80% utilization target
- **Scale-up:** Aggressive (0s stabilization)
- **Scale-down:** Conservative (300s stabilization)

### 10. PodDisruptionBudget (Optional)
- **Min Available:** 1 pod
- Ensures availability during node maintenance/eviction

## Deployment Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Kubernetes Deployment: mqtt-novelty-verifier           │
│  Namespace: agents                                       │
│  Replicas: 2                                             │
└─────────────────────────────────────────────────────────┘
           │
           ├──────────────────────────┬────────────────────┐
           │                          │                    │
      ┌────▼─────┐            ┌───────▼──────┐      ┌─────▼─────┐
      │ Pod #1    │            │ Pod #2       │      │   ... Pod │
      └────┬─────┘            └───────┬──────┘      └─────┬─────┘
           │                          │                    │
      ┌────┴──────────────────────────┴────────────────────┴─────┐
      │                                                           │
  ┌───▼──────────────────┐              ┌──────────────────────┐ │
  │ nova-agent           │              │ mqtt-novelty-handler │ │
  │ Container            │              │ Container (Sidecar)  │ │
  ├──────────────────────┤              ├──────────────────────┤ │
  │ Image:               │              │ Image:               │ │
  │ nova-deep-think:...  │              │ deep-think-mcp:mqtt- │ │
  │                      │              │ novelty-latest       │ │
  │ Port: 8000 (HTTP)    │              │                      │ │
  │ Port: 8001 (metrics) │              │ Port: 9090 (metrics) │ │
  │                      │              │                      │ │
  │ CPU: 250m req        │              │ CPU: 100m req        │ │
  │ CPU: 1000m limit     │              │ CPU: 500m limit      │ │
  │ Mem: 512Mi req       │              │ Mem: 128Mi req       │ │
  │ Mem: 2Gi limit       │              │ Mem: 512Mi limit     │ │
  │                      │              │                      │ │
  │ Probe: /health       │              │ Probe: process check │ │
  │        /ready        │              │        + readiness   │ │
  └────┬──────────────────┘              └──────┬───────────────┘ │
       │                                        │                 │
       ├────────────────────┬───────────────────┤                 │
       │                    │                   │                 │
  ┌────▼──────┐    ┌────────▼──────┐   ┌───────▼────┐            │
  │ ConfigMap  │    │ Secret        │   │ emptyDir   │            │
  │ mqtt-      │    │ mqtt-         │   │ volumes    │            │
  │ novelty-   │    │ credentials   │   │            │            │
  │ config     │    │               │   │ /var/log   │            │
  │            │    │ Password      │   │ /var/run   │            │
  │ Scripts:   │    │ Host          │   │ /tmp       │            │
  │ - mqtt_    │    │ Port          │   │            │            │
  │   novelty_ │    │ User          │   └────────────┘            │
  │   handler  │    │ Ollama URL    │                             │
  │ - run_mqtt │    │               │                             │
  │   _novelty │    └───────────────┘                             │
  │   _service │                                                  │
  └────────────┘                                                  │
       │                                                           │
       └─────────────────────────────────────────────────────────┘
```

## Sidecar Communication Flow

```
1. Initialization:
   mqtt-novelty-handler connects to MQTT broker via MQTT_HOST:MQTT_PORT
   Authenticates with MQTT_USER and MQTT_PASSWORD from Secret

2. Subscription:
   Subscribes to: deep-think/jobs/+/result
   Subscribes to: deep-think/jobs/+/complete

3. Message Processing:
   Receives thinking result JSON from Nova on MQTT topic
   Calls NoveltyVerifier to check if result is novel
   Makes HTTP request to Nova at http://localhost:8000/verify

4. Result Publishing:
   Publishes verification result to: deep-think/jobs/{job_id}/novelty-verified
   Result includes: novelty flag, confidence, reasoning hash, timestamp

5. Health Management:
   Periodic health checks of MQTT connection
   Periodic health checks of Nova endpoint
   Automatic reconnection on disconnection
   Process monitoring via exec probes
```

## Inter-Container Communication

**Via localhost:**
- Nova runs on `localhost:8000`
- MQTT sidecar calls `http://localhost:8000/verify` for novelty verification
- Both containers share the same network namespace (Pod)

**Via Shared Volumes:**
- `/var/log/mqtt-novelty`: Sidecar logs
- `/var/run/mqtt-novelty`: Runtime data (e.g., caches, state files)
- `/tmp`: Temporary inter-process data

**Via Environment Variables:**
- Secret values injected as environment variables
- ConfigMap data available via volume mount

## Networking

- **Internal Communication:** All within cluster on `agents` namespace
- **MQTT Broker:** External connectivity from MQTT sidecar
- **Service Endpoints:**
  - Nova HTTP API: `mqtt-novelty-verifier:8000` (cluster-wide)
  - Metrics: `mqtt-novelty-verifier:9090` (for Prometheus scraping)
- **DNS:** Kubernetes CoreDNS resolves service names automatically

## Pre-Deployment Checklist

- [ ] Create `agents` namespace (or skip if exists)
- [ ] Configure `mqtt-credentials` Secret with actual values
  - [ ] MQTT broker hostname
  - [ ] MQTT broker port
  - [ ] MQTT username
  - [ ] MQTT password (base64-encoded)
- [ ] Verify images exist and are accessible
  - [ ] `nova-deep-think:latest`
  - [ ] `deep-think-mcp:mqtt-novelty-latest`
- [ ] Check MQTT broker is accessible from k3s cluster
- [ ] Verify Ollama URL is correct (if external)
- [ ] Ensure sufficient cluster resources (CPU/memory)
- [ ] Configure storage if using PVCs for logs

## Deployment Steps

### Step 1: Prepare Secrets

```bash
# Option A: Edit manifest directly
vim k8s-mqtt-novelty.yaml
# Find the mqtt-credentials Secret section and update values

# Option B: Create secret separately
kubectl create secret generic mqtt-credentials \
  --from-literal=password='your-mqtt-password' \
  --from-literal=mqtt-host='mqtt-broker.default.svc.cluster.local' \
  --from-literal=mqtt-port='1883' \
  --from-literal=mqtt-user='nova-agent' \
  -n agents
```

See `SECRETS_SETUP.md` for detailed secrets configuration.

### Step 2: Apply Manifest

```bash
# Dry-run to verify
kubectl apply -f k8s-mqtt-novelty.yaml --dry-run=client

# Apply to cluster
kubectl apply -f k8s-mqtt-novelty.yaml
```

### Step 3: Verify Deployment

```bash
# Check namespace
kubectl get namespace agents

# Check deployment
kubectl get deployment mqtt-novelty-verifier -n agents

# Check pods
kubectl get pods -n agents -l app=mqtt-novelty-verifier

# Check service
kubectl get svc mqtt-novelty-verifier -n agents

# Check ConfigMap and Secret
kubectl get configmap mqtt-novelty-config -n agents
kubectl get secret mqtt-credentials -n agents
```

### Step 4: Verify Pod Health

```bash
# Wait for pods to be ready
kubectl rollout status deployment mqtt-novelty-verifier -n agents

# Check pod details
kubectl describe pod -n agents <pod-name>

# Check container logs
kubectl logs -n agents <pod-name> -c nova-agent
kubectl logs -n agents <pod-name> -c mqtt-novelty-handler

# Watch real-time logs
kubectl logs -f -n agents <pod-name> -c mqtt-novelty-handler --tail=50
```

### Step 5: Test Functionality

```bash
# Port-forward to test locally
kubectl port-forward -n agents svc/mqtt-novelty-verifier 8000:8000

# Test Nova endpoint in another terminal
curl http://localhost:8000/health

# Test MQTT connectivity (if mosquitto-clients installed)
kubectl exec -n agents <pod-name> -c mqtt-novelty-handler -- \
  mosquitto_sub -h $MQTT_HOST -p $MQTT_PORT \
    -u $MQTT_USER -P $MQTT_PASSWORD \
    -t "deep-think/jobs/+/result" -W 3
```

## Post-Deployment Configuration

### Enable Prometheus Scraping

The service includes annotations for Prometheus:

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "9090"
  prometheus.io/path: "/metrics"
```

Configure Prometheus ServiceMonitor if using Prometheus Operator:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: mqtt-novelty-verifier
  namespace: agents
spec:
  selector:
    matchLabels:
      app: mqtt-novelty-verifier
  endpoints:
  - port: mqtt-metrics
    interval: 30s
    path: /metrics
```

### Logging

Logs are written to emptyDir volumes:
- Nova logs: `/var/log/nova`
- MQTT sidecar logs: `/var/log/mqtt-novelty`

For persistent logging, configure a sidecar logging agent (e.g., Filebeat) or replace emptyDir with a PVC.

### Scaling

The deployment includes HPA that auto-scales based on metrics. To manage manually:

```bash
# Scale replicas
kubectl scale deployment mqtt-novelty-verifier \
  --replicas=3 -n agents

# View scaling events
kubectl describe hpa mqtt-novelty-verifier -n agents

# Disable HPA
kubectl delete hpa mqtt-novelty-verifier -n agents
```

## Troubleshooting

### Pods Not Starting

```bash
# Check pod events
kubectl describe pod -n agents <pod-name>

# Check image pull errors
kubectl get events -n agents --sort-by='.lastTimestamp'

# Check resource availability
kubectl top nodes
kubectl top pods -n agents
```

### MQTT Connection Issues

```bash
# Check MQTT_PASSWORD is set
kubectl exec -n agents <pod-name> -c mqtt-novelty-handler -- \
  bash -c 'echo "Host: $MQTT_HOST Port: $MQTT_PORT User: $MQTT_USER"'

# Test MQTT directly
kubectl exec -n agents <pod-name> -c mqtt-novelty-handler -- \
  nc -zv $MQTT_HOST $MQTT_PORT

# Check service logs for connection errors
kubectl logs -n agents <pod-name> -c mqtt-novelty-handler | grep -i "mqtt\|error\|connection"
```

### High Resource Usage

```bash
# Check current resource usage
kubectl top pod -n agents <pod-name> --containers

# Check limits vs actual usage
kubectl describe pod -n agents <pod-name> | grep -A 5 "Limits\|Requests"

# Adjust limits in manifest and reapply
vim k8s-mqtt-novelty.yaml
kubectl apply -f k8s-mqtt-novelty.yaml
```

### Liveness Probe Failures

For MQTT sidecar:
```bash
# Check if process is running
kubectl exec -n agents <pod-name> -c mqtt-novelty-handler -- \
  ps aux | grep mqtt_novelty

# Check probe command directly
kubectl exec -n agents <pod-name> -c mqtt-novelty-handler -- \
  /bin/sh -c 'ps aux | grep -q "[p]ython.*mqtt_novelty" && echo "running" || echo "not running"'
```

## Cleanup

To remove the deployment:

```bash
# Delete entire namespace (removes all resources)
kubectl delete namespace agents

# Or delete just the deployment
kubectl delete deployment mqtt-novelty-verifier -n agents

# Or delete everything from manifest
kubectl delete -f k8s-mqtt-novelty.yaml
```

## Related Files

- `k8s-mqtt-novelty.yaml`: Main Kubernetes manifest
- `SECRETS_SETUP.md`: Detailed secrets configuration guide
- `DEPLOYMENT_GUIDE.md`: This file

## References

- [Kubernetes Deployment](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)
- [Kubernetes Pods](https://kubernetes.io/docs/concepts/workloads/pods/)
- [Kubernetes Volumes](https://kubernetes.io/docs/concepts/storage/volumes/)
- [Kubernetes Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)
- [k3s Documentation](https://docs.k3s.io/)
