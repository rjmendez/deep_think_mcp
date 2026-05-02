# MQTT Novelty Verifier - Kubernetes Deployment Package

**Status: ✅ Ready for Deployment | ❌ NOT Yet Applied to Cluster**

Complete Kubernetes manifest package for deploying MQTT novelty handler as a sidecar to Nova on Oxalis k3s cluster.

## 📦 Package Contents

### Core Files
- **`k8s-mqtt-novelty.yaml`** (24KB, 799 lines)
  - Complete Kubernetes manifest with 10 objects
  - Includes Namespace, Secret template, ConfigMap, Deployment, Service, RBAC, HPA, PDB
  - Embedded Python scripts for MQTT novelty verification
  - **Status: YAML-valid, Secret template values need customization**

### Documentation Files

#### Quick Start
- **`README_K8S_MQTT.md`** (this file)
  - Overview and quick navigation

- **`QUICKREF.md`** (9.4 KB)
  - 1-page quick reference card
  - Common kubectl commands
  - Checklists and troubleshooting
  - Security features summary

#### Detailed Guides
- **`MANIFEST_SUMMARY.md`** (14.6 KB)
  - Complete manifest documentation
  - Architecture diagram
  - Data flow explanation
  - Customization points
  - Deployment checklist

- **`DEPLOYMENT_GUIDE.md`** (15.4 KB)
  - Step-by-step deployment instructions
  - Pre-deployment checklist
  - Testing and verification procedures
  - Logging and monitoring setup
  - Troubleshooting guide

- **`SECRETS_SETUP.md`** (8.1 KB)
  - 5 methods to configure MQTT credentials
  - Base64 encoding instructions
  - Sealed-secrets and external-secrets examples
  - Secret verification procedures
  - Security best practices

#### Examples
- **`DEPLOYMENT_EXAMPLE.sh`** (5.0 KB)
  - Executable bash script
  - Automated deployment workflow
  - Interactive credential configuration
  - Validation and verification steps
  - Post-deployment instructions

## 🚀 Quick Start

### 1. One-Minute Setup
```bash
cd /home/rjmendez/development/deep_think_mcp

# Make deployment script executable
chmod +x DEPLOYMENT_EXAMPLE.sh

# Run automated deployment
./DEPLOYMENT_EXAMPLE.sh
```

### 2. Manual Deployment (3 minutes)
```bash
# Step 1: Create credentials
echo -n "your-mqtt-password" | base64
# Output: base64-encoded-password

# Step 2: Edit manifest
vim k8s-mqtt-novelty.yaml
# Update Secret section with your credentials

# Step 3: Create namespace
kubectl create namespace agents

# Step 4: Apply manifest
kubectl apply -f k8s-mqtt-novelty.yaml

# Step 5: Verify
kubectl get pods -n agents
kubectl logs -f -n agents <pod-name> -c mqtt-novelty-handler
```

### 3. Command-Line Deployment
```bash
# Create secret directly
kubectl create secret generic mqtt-credentials \
  --from-literal=password='mqtt-password' \
  --from-literal=mqtt-host='mqtt-broker.svc.cluster.local' \
  --from-literal=mqtt-port='1883' \
  --from-literal=mqtt-user='nova-agent' \
  -n agents

# Apply manifest
kubectl apply -f k8s-mqtt-novelty.yaml

# Check status
kubectl rollout status deployment mqtt-novelty-verifier -n agents
```

## 📋 What Gets Deployed

### Resources Created
| Resource | Type | Count | Purpose |
|----------|------|-------|---------|
| Namespace | Namespace | 1 | `agents` namespace for isolation |
| Deployment | Deployment | 1 | Main workload with 2 replicas |
| Service | Service | 1 | ClusterIP service, ports 8000/8001/9090 |
| ConfigMap | ConfigMap | 1 | Python scripts for MQTT handler |
| Secret | Secret | 1 | MQTT credentials (template, customize!) |
| ServiceAccount | ServiceAccount | 1 | Pod identity for RBAC |
| Role | Role | 1 | Minimal read permissions |
| RoleBinding | RoleBinding | 1 | Bind Role to ServiceAccount |
| HorizontalPodAutoscaler | HPA | 1 | Auto-scale 2-5 replicas (optional) |
| PodDisruptionBudget | PDB | 1 | HA protection (optional) |

### Containers
```
Pod = nova-agent + mqtt-novelty-handler (sidecar)

nova-agent
├─ Image: nova-deep-think:latest
├─ Ports: 8000 (HTTP), 8001 (metrics)
├─ CPU: 250m request, 1000m limit
├─ Memory: 512Mi request, 2Gi limit
└─ Probes: /health, /ready

mqtt-novelty-handler
├─ Image: deep-think-mcp:mqtt-novelty-latest
├─ Port: 9090 (metrics)
├─ CPU: 100m request, 500m limit
├─ Memory: 128Mi request, 512Mi limit
└─ Probes: process check, MQTT connectivity
```

## 🔐 Security Features

✅ Non-root container (UID 1000)
✅ Read-only root filesystem
✅ Dropped Linux capabilities
✅ Pod security context enforced
✅ RBAC with minimal permissions
✅ Secrets mounted read-only
✅ Resource limits prevent DoS
✅ Health checks and auto-restart
✅ Pod disruption budget for HA

## 📊 Deployment Architecture

```
Kubernetes (k3s) - Oxalis Cluster
└─ Namespace: agents
   ├─ Deployment: mqtt-novelty-verifier
   │  ├─ Replica 1
   │  │  ├─ Container: nova-agent (primary)
   │  │  └─ Container: mqtt-novelty-handler (sidecar)
   │  └─ Replica 2 (anti-affinity: different node)
   │
   ├─ Service: mqtt-novelty-verifier
   │  ├─ Port 8000 → nova-http
   │  ├─ Port 8001 → nova-metrics
   │  └─ Port 9090 → mqtt-metrics
   │
   ├─ ConfigMap: mqtt-novelty-config
   │  ├─ run_mqtt_novelty_service.py
   │  └─ mqtt_novelty_handler.py
   │
   └─ Secret: mqtt-credentials
      ├─ password (base64)
      ├─ mqtt-host
      ├─ mqtt-port
      └─ mqtt-user

External
├─ MQTT Broker (port 1883/8883)
└─ Ollama API (optional)
```

## 🔧 Configuration

### Before Deployment
1. **MQTT Credentials** - Edit Secret in manifest
   - Base64 encode password: `echo -n "password" | base64`
   - Update: mqtt-host, mqtt-port, mqtt-user, password

2. **Image Names** - If using custom image tags
   - `nova-deep-think:latest`
   - `deep-think-mcp:mqtt-novelty-latest`

3. **Resource Limits** - If cluster capacity differs
   - Default: 100m/128Mi request, 500m/512Mi limit for sidecar
   - Adjust `resources.requests` and `resources.limits`

4. **Replica Count** - For different HA strategy
   - Default: 2 replicas
   - HPA can scale to 5

### After Deployment
1. **Monitoring** - Enable Prometheus scraping
   - Service has prometheus.io annotations
   - Endpoints: `:8001/metrics`, `:9090/metrics`

2. **Logging** - Replace emptyDir with PVC for persistent logs
   - Update volumes section in Deployment

3. **Scaling** - Adjust HPA or scale manually
   - Edit HPA replicas or: `kubectl scale deployment ... --replicas=3`

## 🧪 Verification

### Pre-Deployment
```bash
# Validate YAML syntax
kubectl apply -f k8s-mqtt-novelty.yaml --dry-run=client

# Check manifest structure
grep "^kind: " k8s-mqtt-novelty.yaml | wc -l  # Should be 10
```

### Post-Deployment
```bash
# Check deployment status
kubectl get deployment mqtt-novelty-verifier -n agents

# Check pods are running
kubectl get pods -n agents -l app=mqtt-novelty-verifier

# Check service endpoints
kubectl get svc mqtt-novelty-verifier -n agents -o wide

# Verify pod logs
kubectl logs -n agents <pod-name> -c mqtt-novelty-handler

# Check container readiness
kubectl describe pod -n agents <pod-name>

# Port-forward and test
kubectl port-forward -n agents svc/mqtt-novelty-verifier 8000:8000
curl http://localhost:8000/health  # Should return 200
```

## 📚 Documentation Guide

### For First-Time Users
1. **Start here:** `QUICKREF.md` - 2-minute overview
2. **Then read:** `DEPLOYMENT_GUIDE.md` - Detailed steps
3. **Reference:** `MANIFEST_SUMMARY.md` - Architecture details

### For Troubleshooting
1. Check: `QUICKREF.md` troubleshooting section
2. Debug: `DEPLOYMENT_GUIDE.md` troubleshooting section
3. Logs: `kubectl logs -f -n agents <pod> -c mqtt-novelty-handler`

### For Secret Configuration
1. Read: `SECRETS_SETUP.md` - 5 configuration methods
2. Choose: Best method for your environment
3. Verify: Secret created successfully

### For Automation
1. Review: `DEPLOYMENT_EXAMPLE.sh` - Automated workflow
2. Run: `./DEPLOYMENT_EXAMPLE.sh` - Interactive deployment
3. Customize: Edit script for your needs

## 🎯 Common Tasks

### Deploy
```bash
./DEPLOYMENT_EXAMPLE.sh
# or
kubectl apply -f k8s-mqtt-novelty.yaml
```

### Monitor
```bash
# Stream logs (all containers in all pods)
kubectl logs -f -n agents -l app=mqtt-novelty-verifier

# Watch pod status
kubectl get pods -n agents -l app=mqtt-novelty-verifier -w

# Check resource usage
kubectl top pods -n agents --containers
```

### Update Credentials
```bash
# Delete old secret
kubectl delete secret mqtt-credentials -n agents

# Create new secret
kubectl create secret generic mqtt-credentials \
  --from-literal=password='new-password' \
  --from-literal=mqtt-host='...' \
  --from-literal=mqtt-port='...' \
  --from-literal=mqtt-user='...' \
  -n agents

# Restart pods to pick up new credentials
kubectl rollout restart deployment mqtt-novelty-verifier -n agents
```

### Scale
```bash
# Manual scale
kubectl scale deployment mqtt-novelty-verifier --replicas=3 -n agents

# Check HPA status
kubectl get hpa mqtt-novelty-verifier -n agents

# View scaling history
kubectl describe hpa mqtt-novelty-verifier -n agents
```

### Debug
```bash
# Exec into pod
kubectl exec -it -n agents <pod-name> -c mqtt-novelty-handler -- /bin/bash

# Check environment variables
kubectl exec -n agents <pod-name> -c mqtt-novelty-handler -- env | grep MQTT

# Test MQTT connectivity
kubectl exec -n agents <pod-name> -c mqtt-novelty-handler -- nc -zv $MQTT_HOST $MQTT_PORT
```

### Delete
```bash
# Delete deployment (keeps namespace and secret)
kubectl delete deployment mqtt-novelty-verifier -n agents

# Delete entire namespace and all resources
kubectl delete namespace agents

# Delete using manifest file
kubectl delete -f k8s-mqtt-novelty.yaml
```

## ⚙️ MQTT Integration

### Subscribed Topics
```
deep-think/jobs/+/result     → Thinking results from Nova
deep-think/jobs/+/complete   → Completion notifications
```

### Published Topics
```
deep-think/jobs/{job_id}/novelty-verified  → Verification results
```

### Message Format
**Input (subscribed):**
```json
{
  "job_id": "job-12345",
  "thinking_output": "...",
  "metadata": {"model": "gpt-5", "passes": 3}
}
```

**Output (published):**
```json
{
  "job_id": "job-12345",
  "is_novel": true,
  "confidence": 0.99,
  "reasoning_hash": "abc123...",
  "timestamp": "2024-05-01T15:06:30Z",
  "novelty_score": 1.0
}
```

## 🔍 Troubleshooting Quick Links

| Issue | Check |
|-------|-------|
| Pods not starting | `kubectl describe pod -n agents <pod>` |
| MQTT connection failed | `kubectl logs -n agents <pod> -c mqtt-novelty-handler \| grep MQTT` |
| Secret not found | `kubectl get secret mqtt-credentials -n agents` |
| High CPU/Memory | `kubectl top pods -n agents --containers` |
| OOMKilled | Increase memory limit in manifest |
| Service unavailable | `kubectl get svc -n agents` and check endpoints |

## 📞 Support Resources

- **Kubernetes Docs:** https://kubernetes.io/docs/
- **k3s Docs:** https://docs.k3s.io/
- **MQTT Specification:** https://mqtt.org/
- **Nova Documentation:** (project-specific)

## ✅ Pre-Deployment Checklist

- [ ] Read this README
- [ ] Review manifest structure (MANIFEST_SUMMARY.md)
- [ ] Prepare MQTT broker credentials
- [ ] Base64 encode password
- [ ] Edit Secret section in manifest
- [ ] Verify container images exist
- [ ] Check cluster resource availability (100m CPU, 128Mi mem minimum per sidecar)
- [ ] Ensure MQTT broker accessible from k3s cluster
- [ ] Create agents namespace or verify it exists
- [ ] Run `kubectl apply --dry-run=client` first
- [ ] Deploy with `kubectl apply -f k8s-mqtt-novelty.yaml`
- [ ] Verify with `kubectl get pods -n agents`

## 📝 File Manifest

```
/home/rjmendez/development/deep_think_mcp/
├── k8s-mqtt-novelty.yaml        ← MAIN MANIFEST (apply this)
├── DEPLOYMENT_EXAMPLE.sh          ← Automated deployment script
├── README_K8S_MQTT.md             ← This file
├── QUICKREF.md                    ← 1-page quick reference
├── MANIFEST_SUMMARY.md            ← Complete documentation
├── DEPLOYMENT_GUIDE.md            ← Step-by-step guide
└── SECRETS_SETUP.md               ← Secrets configuration guide
```

## 📄 License & Attribution

This manifest package is designed for Oxalis k3s cluster deployment.
Includes embedded MQTT novelty verification scripts compatible with Nova deep-think agent.

---

**Created:** 2024
**Status:** ✅ Ready for Deployment | ❌ NOT Yet Applied
**Next Step:** Run `./DEPLOYMENT_EXAMPLE.sh` or read `DEPLOYMENT_GUIDE.md`
