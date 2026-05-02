# 🚀 MQTT Novelty Verifier - Kubernetes Deployment Package

## START HERE ➡️

Welcome! This package contains a complete, production-ready Kubernetes manifest for deploying the MQTT novelty handler as a sidecar to Nova on Oxalis k3s.

### ⏱️ Quick Timeline
- **2 minutes:** Read this file
- **5 minutes:** Read QUICKREF.md
- **10 minutes:** Run deployment script OR manual deployment
- **Total:** ~17 minutes from start to running

---

## 📦 What You're Deploying

A **two-container pod** that runs:
1. **nova-agent** (primary) - Deep-thinking agent at `localhost:8000`
2. **mqtt-novelty-handler** (sidecar) - Monitors MQTT for results and verifies novelty

**Deployed as:** Kubernetes Deployment with 2 replicas in `agents` namespace

---

## 📁 Files Created

| File | Size | Purpose | Read This For |
|------|------|---------|---------------|
| **k8s-mqtt-novelty.yaml** | 24 KB | Complete manifest (799 lines, 10 objects) | The actual deployment |
| **DEPLOYMENT_EXAMPLE.sh** | 9 KB | Automated deployment script | Easy deployment |
| **README_K8S_MQTT.md** | 13 KB | Navigation & overview | Package guide |
| **QUICKREF.md** | 9 KB | 1-page quick reference | Fast lookups |
| **MANIFEST_SUMMARY.md** | 18 KB | Complete documentation | Detailed understanding |
| **DEPLOYMENT_GUIDE.md** | 17 KB | Step-by-step instructions | Detailed deployment |
| **SECRETS_SETUP.md** | 8 KB | Secret configuration (5 methods) | Secret management |
| **00_START_HERE.md** | This file | Quick navigation | Where to start |

**Total:** ~2,900 lines of manifest + documentation

---

## 🎯 Three Ways to Deploy

### ✨ Option 1: Automated (Recommended)
```bash
cd /home/USER/development/deep_think_mcp
./DEPLOYMENT_EXAMPLE.sh
```
**Time:** 5 minutes | **Effort:** Minimal | **Best for:** First-time deployments

### 🛠️ Option 2: Manual (Step-by-Step)
```bash
# See DEPLOYMENT_GUIDE.md for detailed instructions
# Takes ~10 minutes with explanations
```

### ⚡ Option 3: One-Command (If credentials ready)
```bash
# Configure secret first, then:
kubectl apply -f k8s-mqtt-novelty.yaml
```

---

## 🔐 Before You Deploy

### ⚠️ IMPORTANT: Secret Configuration

The manifest includes a **template Secret** with placeholder values:
```yaml
password: Y2hhbmdlbWU=  # ← "changeme" in base64
mqtt-host: "mqtt-broker.default.svc.cluster.local"
mqtt-port: "1883"
mqtt-user: "nova-agent"
```

**You must customize these** before deployment!

### What You Need
- [ ] MQTT broker hostname (or IP)
- [ ] MQTT broker port (usually 1883)
- [ ] MQTT username
- [ ] MQTT password (base64-encoded)

### Encode Your Password
```bash
echo -n "your-password" | base64
# Output: eW91ci1wYXNzd29yZA==
```

---

## 📖 Documentation Roadmap

### New to This? Read This Order
1. **This file** (you're reading it) - 2 min
2. **QUICKREF.md** - 5 min
3. **DEPLOYMENT_GUIDE.md** - Deployment steps
4. **Run deployment script** - 5 min

### Want to Understand Everything?
1. **MANIFEST_SUMMARY.md** - Architecture (10 min)
2. **DEPLOYMENT_GUIDE.md** - Details (15 min)
3. **SECRETS_SETUP.md** - Secret options (10 min)
4. **k8s-mqtt-novelty.yaml** - Source (read as needed)

### Just Need Quick Answers?
→ **QUICKREF.md** - Has commands, examples, troubleshooting

### Just Deploy It?
→ **DEPLOYMENT_EXAMPLE.sh** - Interactive deployment

---

## 🚀 30-Second Deployment

```bash
# 1. Prepare credentials
export MQTT_PASSWORD="your-password"
export MQTT_HOST="mqtt-broker.example.com"
export MQTT_PORT="1883"
export MQTT_USER="nova-agent"

# 2. Run automated script
cd /home/USER/development/deep_think_mcp
./DEPLOYMENT_EXAMPLE.sh

# ✅ Done! Pods should be running
```

---

## ✅ Manifest Structure (10 Objects)

```
┌─ Namespace (agents)
├─ Secret (mqtt-credentials) ← CUSTOMIZE THIS
├─ ConfigMap (mqtt-novelty-config)
├─ Deployment (mqtt-novelty-verifier)
│  ├─ Container: nova-agent
│  └─ Container: mqtt-novelty-handler (sidecar)
├─ Service (mqtt-novelty-verifier)
├─ ServiceAccount
├─ Role
├─ RoleBinding
├─ HorizontalPodAutoscaler (auto-scaling)
└─ PodDisruptionBudget (HA protection)
```

---

## 🎓 Understanding the Deployment

### What Runs?
- **2 Pods** (replicas for high availability)
- **2 Containers per pod** (nova + mqtt handler)
- **4 Total containers** across cluster

### What Does the Sidecar Do?
1. Connects to MQTT broker
2. Subscribes to: `deep-think/jobs/+/result`
3. Receives thinking results from Nova
4. Verifies if result is novel (duplicate detection)
5. Publishes result back to MQTT: `deep-think/jobs/{id}/novelty-verified`
6. Periodically health-checks both MQTT and Nova

### How Do They Communicate?
- **Within Pod:** `localhost:8000` (same network namespace)
- **With MQTT:** TCP to external broker (1883/8883)
- **With Ollama:** TCP to optional external service

---

## 📊 Resource Requirements

### Sidecar Container (mqtt-novelty-handler)
```
CPU Request:    100m  (0.1 vCPU)
CPU Limit:      500m  (0.5 vCPU)
Memory Request: 128Mi
Memory Limit:   512Mi
```

### Primary Container (nova-agent)
```
CPU Request:    250m  (0.25 vCPU)
CPU Limit:      1000m (1 vCPU)
Memory Request: 512Mi
Memory Limit:   2Gi
```

### Total per Pod: ~500m CPU, 640Mi Memory

---

## 🔒 Security Features

✅ Non-root containers (UID 1000)
✅ Read-only root filesystems
✅ All Linux capabilities dropped
✅ RBAC with minimal permissions
✅ Secret-based credential management
✅ Health checks and auto-restart
✅ Pod disruption budget (HA)
✅ Resource limits (DoS prevention)

---

## 📈 Auto-Scaling (Optional)

The manifest includes HPA (HorizontalPodAutoscaler):
- **Minimum:** 2 replicas
- **Maximum:** 5 replicas
- **Scale-up trigger:** 70% CPU or 80% memory
- **Scale-down:** Conservative (300s stabilization)

Can be disabled by deleting the HPA resource.

---

## 🧪 Verify It's Working

After deployment:

```bash
# Check pods are running
kubectl get pods -n agents

# Check logs
kubectl logs -f -n agents <pod-name> -c mqtt-novelty-handler

# Test connection
kubectl port-forward -n agents svc/mqtt-novelty-verifier 8000:8000
curl http://localhost:8000/health

# Check metrics
curl http://localhost:9090/metrics
```

Expected output:
- ✅ 2 pods in `agents` namespace
- ✅ Both containers "Running"
- ✅ `/health` returns 200 OK
- ✅ No error messages in logs
- ✅ MQTT connections established

---

## 🎯 Next Steps

### Recommended Path
1. **Right now:** Choose deployment method (automated recommended)
2. **Before deploy:** Prepare MQTT credentials
3. **Do it:** Run deployment script
4. **Verify:** Check `kubectl get pods -n agents`
5. **Test:** Review logs and test endpoints
6. **Monitor:** Watch `kubectl logs -f ...`

### Deployment Commands by Method

**Automated:**
```bash
./DEPLOYMENT_EXAMPLE.sh
```

**Manual:**
```bash
# Edit manifest
vim k8s-mqtt-novelty.yaml

# Create namespace
kubectl create namespace agents

# Create secret
kubectl create secret generic mqtt-credentials \
  --from-literal=password='...' \
  --from-literal=mqtt-host='...' \
  --from-literal=mqtt-port='...' \
  --from-literal=mqtt-user='...' \
  -n agents

# Apply manifest
kubectl apply -f k8s-mqtt-novelty.yaml

# Verify
kubectl rollout status deployment mqtt-novelty-verifier -n agents
```

**Simple (if all done):**
```bash
kubectl apply -f k8s-mqtt-novelty.yaml
```

---

## ❓ FAQ

**Q: Is this production-ready?**
A: Yes! Includes RBAC, security context, health checks, auto-scaling, high availability.

**Q: Do I need to customize anything?**
A: Only the Secret section with MQTT credentials. Everything else has sensible defaults.

**Q: How long does deployment take?**
A: 1-2 minutes for pods to be ready after applying manifest.

**Q: Can I scale it?**
A: Yes! HPA auto-scales 2-5 replicas. Or manually: `kubectl scale deployment ... --replicas=3`

**Q: What if MQTT broker is down?**
A: Sidecar will retry connection with exponential backoff. Pod remains running.

**Q: How do I update credentials?**
A: See SECRETS_SETUP.md. You can update secret and restart pods.

**Q: How do I remove it?**
A: `kubectl delete namespace agents` removes everything in one command.

**Q: Does it need persistent storage?**
A: No. All state is ephemeral. Logs are in-memory (emptyDir).

**Q: How do I monitor it?**
A: Service has Prometheus annotations. Metrics at `:8001` and `:9090/metrics`

**Q: Can I customize resource limits?**
A: Yes! Edit `resources.requests` and `resources.limits` in manifest.

---

## 📞 Troubleshooting Quick Links

| Problem | Solution |
|---------|----------|
| "Pods stuck in Pending" | `kubectl describe pod -n agents <pod-name>` |
| "CrashLoopBackOff" | Check logs: `kubectl logs -n agents <pod> --previous` |
| "MQTT connection failed" | Verify credentials and broker is accessible |
| "Secret not found" | See SECRETS_SETUP.md |
| "OOMKilled" | Increase memory limit in manifest |
| "High CPU usage" | Check `kubectl top pods -n agents` |

Full troubleshooting: See **QUICKREF.md** or **DEPLOYMENT_GUIDE.md**

---

## 🎓 Learn More

**Kubernetes Concepts:**
- [Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)
- [Services](https://kubernetes.io/docs/concepts/services-networking/service/)
- [ConfigMaps & Secrets](https://kubernetes.io/docs/concepts/configuration/)
- [Health Checks](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)

**This Package:**
- Manifest details → **MANIFEST_SUMMARY.md**
- Step-by-step → **DEPLOYMENT_GUIDE.md**
- Quick answers → **QUICKREF.md**
- Secret options → **SECRETS_SETUP.md**

---

## 📋 Pre-Deployment Checklist

- [ ] Read this file
- [ ] Prepare MQTT broker hostname/port/user/password
- [ ] Decide deployment method (automated recommended)
- [ ] Have `kubectl` configured for k3s cluster
- [ ] Have `chmod +x` run on DEPLOYMENT_EXAMPLE.sh
- [ ] Ready to proceed!

---

## ✨ Your Journey

```
📖 Reading this file (now)
    ↓
🔐 Prepare MQTT credentials
    ↓
🚀 Run deployment (script or manual)
    ↓
✅ Verify pods are running
    ↓
📊 Monitor logs and metrics
    ↓
🎉 Deployment complete!
```

---

## 🎯 Ready to Deploy?

### Pick Your Path:

**🟢 Easiest (Recommended)**
```bash
cd /home/USER/development/deep_think_mcp
./DEPLOYMENT_EXAMPLE.sh
```

**🟡 Step-by-Step**
```
Read: DEPLOYMENT_GUIDE.md
Follow: Instructions section
```

**🔴 Advanced**
```
Edit: k8s-mqtt-novelty.yaml
Run: kubectl apply -f k8s-mqtt-novelty.yaml
```

---

## 📄 File Locations

All files are in: `/home/USER/development/deep_think_mcp/`

- `k8s-mqtt-novelty.yaml` ← Main manifest
- `DEPLOYMENT_EXAMPLE.sh` ← Run this for easy deployment
- `*.md` ← Documentation files

---

**Status:** ✅ Ready for Deployment | ❌ NOT Yet Applied to Cluster

**Next Action:** Read QUICKREF.md (5 min) or run DEPLOYMENT_EXAMPLE.sh (10 min)

**Questions?** Check QUICKREF.md FAQ or DEPLOYMENT_GUIDE.md Troubleshooting section.

---

*Kubernetes manifest package for MQTT novelty handler on Oxalis k3s | Created 2024*
