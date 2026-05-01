# Ground Truth Provider Configuration

This document provides setup instructions for deploying the Deep Think MCP ground truth providers (Nova and MQTT) in local, Docker, and Kubernetes environments.

## Local Development

### Prerequisites
- Python 3.9+
- MQTT broker (e.g., Mosquitto)
- Optional: GitHub Copilot OAuth token or Anthropic API key
- Optional: Nova service for ground truth validation

### Setup Checklist

- [ ] **Clone repository and navigate to project directory**
  ```bash
  cd /home/rjmendez/development/deep_think_mcp
  ```

- [ ] **Copy environment template**
  ```bash
  cp .env.example .env
  ```

- [ ] **Configure MQTT connection** (edit `.env`)
  ```bash
  MQTT_HOST=botnet.floppydicks.net
  MQTT_PORT=1883
  MQTT_PASSWORD=your_password_here
  ```

- [ ] **Configure Nova/Great Library** (edit `.env`)
  ```bash
  # For k3s deployments (optional):
  NOVA_TOKEN=your_nova_token
  NOVA_TOTP_SEED=your_totp_seed
  ```

- [ ] **Configure LLM provider** (edit `.env`, choose one):
  ```bash
  # Option 1: GitHub Copilot
  GITHUB_COPILOT_OAUTH_TOKEN=your_token_here
  
  # Option 2: Anthropic
  ANTHROPIC_API_KEY=your_api_key_here
  ```

- [ ] **Install Python dependencies**
  ```bash
  pip install -r requirements.txt
  ```

- [ ] **Run unit tests**
  ```bash
  python -m pytest -m "unit" -v
  ```

- [ ] **Run integration tests** (requires MQTT and Nova)
  ```bash
  python -m pytest -m "integration" -v
  ```

- [ ] **Start the MCP server** (for testing with Copilot CLI)
  ```bash
  python -m deep_think_mcp
  ```

## Docker / Container Deployment

### Build Docker Image

```bash
docker build -t deep-think-mcp:latest .
```

### Environment File

Create `docker.env`:

```env
MQTT_HOST=mqtt-broker
MQTT_PORT=1883
MQTT_PASSWORD=password
NOVA_TOKEN=token
NOVA_TOTP_SEED=seed
ANTHROPIC_API_KEY=key
```

### Run Container

```bash
docker run -d \
  --name deep-think-mcp \
  --env-file docker.env \
  -p 8000:8000 \
  -v deep-think-cache:/app/cache \
  --network mqtt-network \
  deep-think-mcp:latest
```

### Volume Mounts

- `/app/cache` - Sensor data cache and validation results
- `/app/logs` - Application logs (optional, for debugging)

### Port Mappings

- `8000` - HTTP server (MCP protocol)
- `8001` - Metrics/health endpoint (if enabled)

### Networking

Connect to existing MQTT bridge network:

```bash
docker network create mqtt-network
docker run -d \
  --name mqtt-broker \
  --network mqtt-network \
  -p 1883:1883 \
  eclipse-mosquitto:latest

docker run -d \
  --name deep-think-mcp \
  --network mqtt-network \
  --env MQTT_HOST=mqtt-broker \
  -p 8000:8000 \
  deep-think-mcp:latest
```

## Kubernetes / k3s Deployment

### Prerequisites

- k3s cluster running with kubectl access
- Namespace: `agents`
- MQTT broker accessible from cluster

### Create Secrets

```bash
kubectl create namespace agents 2>/dev/null || true

# MQTT credentials
kubectl create secret generic mqtt-credentials \
  --from-literal=host=botnet.floppydicks.net \
  --from-literal=port=1883 \
  --from-literal=password=your_password \
  -n agents

# Nova credentials
kubectl create secret generic nova-secrets \
  --from-literal=token=your_nova_token \
  --from-literal=totp_seed=your_totp_seed \
  -n agents
```

### Create ConfigMap

```bash
kubectl create configmap deep-think-config \
  --from-literal=MQTT_BROKER_HOST=botnet.floppydicks.net \
  --from-literal=MQTT_BROKER_PORT=1883 \
  --from-literal=CACHE_TTL_SECONDS=30 \
  --from-literal=LOG_LEVEL=INFO \
  -n agents
```

### Deploy Pod

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: deep-think-mcp
  namespace: agents
  labels:
    app: deep-think-mcp
spec:
  containers:
  - name: mcp
    image: deep-think-mcp:latest
    imagePullPolicy: IfNotPresent
    
    # Port configuration
    ports:
    - name: http
      containerPort: 8000
      protocol: TCP
    
    # Environment from ConfigMap
    envFrom:
    - configMapRef:
        name: deep-think-config
    
    # Environment from Secrets
    env:
    - name: MQTT_PASSWORD
      valueFrom:
        secretKeyRef:
          name: mqtt-credentials
          key: password
    - name: NOVA_TOKEN
      valueFrom:
        secretKeyRef:
          name: nova-secrets
          key: token
    - name: NOVA_TOTP_SEED
      valueFrom:
        secretKeyRef:
          name: nova-secrets
          key: totp_seed
    
    # Resource limits
    resources:
      requests:
        memory: "256Mi"
        cpu: "250m"
      limits:
        memory: "512Mi"
        cpu: "500m"
    
    # Readiness probe
    readinessProbe:
      httpGet:
        path: /health
        port: 8000
      initialDelaySeconds: 5
      periodSeconds: 10
      timeoutSeconds: 3
      failureThreshold: 3
    
    # Liveness probe
    livenessProbe:
      httpGet:
        path: /health
        port: 8000
      initialDelaySeconds: 15
      periodSeconds: 30
      timeoutSeconds: 5
      failureThreshold: 3
    
    # Volume mounts
    volumeMounts:
    - name: cache
      mountPath: /app/cache
    - name: logs
      mountPath: /app/logs
  
  # Restart policy
  restartPolicy: Always
  
  # Volumes
  volumes:
  - name: cache
    emptyDir:
      sizeLimit: 1Gi
  - name: logs
    emptyDir:
      sizeLimit: 500Mi
```

Save as `deep-think-mcp-pod.yaml` and deploy:

```bash
kubectl apply -f deep-think-mcp-pod.yaml
```

### Verify Deployment

```bash
# Check pod status
kubectl get pods -n agents -l app=deep-think-mcp

# View logs
kubectl logs -n agents deep-think-mcp -f

# Port forward for testing
kubectl port-forward -n agents deep-think-mcp 8000:8000
```

### Pod Resource Limits

- **Memory Request**: 256 MiB (typical operation)
- **Memory Limit**: 512 MiB (burst operations)
- **CPU Request**: 250m (baseline)
- **CPU Limit**: 500m (max concurrent validation)

### Readiness Probe Configuration

The pod reports ready when:
1. MQTT connection established (if MQTT provider enabled)
2. Nova service reachable (if Nova provider enabled)
3. HTTP server responding to `/health`

Tuning:
- `initialDelaySeconds: 5` - Wait 5s before first probe
- `periodSeconds: 10` - Check every 10s
- `failureThreshold: 3` - Mark unhealthy after 3 failures

### Environment Variables Reference

| Variable | Example | Required | Description |
|----------|---------|----------|-------------|
| `MQTT_HOST` | `botnet.floppydicks.net` | No | MQTT broker hostname |
| `MQTT_PORT` | `1883` | No | MQTT broker port |
| `MQTT_PASSWORD` | `password` | No | MQTT authentication |
| `NOVA_TOKEN` | `nova-xxx` | No | Nova service authentication |
| `NOVA_TOTP_SEED` | `JBSWY3D...` | No | TOTP seed for Nova |
| `ANTHROPIC_API_KEY` | `sk-ant-xxx` | No | Anthropic API key |
| `GITHUB_COPILOT_OAUTH_TOKEN` | `ghu_xxx` | No | GitHub Copilot token |
| `CACHE_TTL_SECONDS` | `30` | No | Sensor cache expiration (default: 30) |
| `LOG_LEVEL` | `INFO` | No | Logging level (default: INFO) |

## Troubleshooting

### MQTT Connection Issues

```bash
# Test MQTT broker connectivity
mosquitto_sub -h botnet.floppydicks.net -p 1883 -P password -t "dama/+/telemetry"
```

### Nova Service Timeouts

The provider implements automatic exponential backoff (1s, 2s, 4s) for Nova timeouts.

If retries are exhausted:
- Check Nova service health: `curl http://nova-service:8000/health`
- Verify network connectivity to Nova pod
- Check Nova pod logs for rate limiting

### Cache Issues

To clear sensor cache:

```python
# In code
provider = MQTTGroundTruthProvider()
provider._sensor_cache.clear()

# Via CLI
python -c "from ground_truth import MQTTGroundTruthProvider; \
  p = MQTTGroundTruthProvider(); \
  p._sensor_cache.clear(); \
  print('Cache cleared')"
```

### Validation Failures

Enable debug logging:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

Common causes:
- Malformed MQTT payload (missing required fields)
- Stale sensor data (age_ms > cache_ttl_seconds)
- Invalid GPS/WiFi/Bluetooth section format
