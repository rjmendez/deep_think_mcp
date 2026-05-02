# MQTT Novelty Verifier - Secrets Setup Guide

## Overview
This document explains how to configure the `mqtt-credentials` secret for the MQTT novelty verifier deployment.

## Secret Template

The manifest includes a basic secret template. Before deploying, you must configure it with your actual values.

### Current Template (in k8s-mqtt-novelty.yaml)
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: mqtt-credentials
  namespace: agents
type: Opaque
data:
  password: Y2hhbmdlbWU=
stringData:
  mqtt-host: "mqtt-broker.default.svc.cluster.local"
  mqtt-port: "1883"
  mqtt-user: "nova-agent"
```

## Configuration Steps

### 1. Prepare Your Values

Gather the following information:
- **MQTT_PASSWORD**: Your MQTT broker password
- **MQTT_HOST**: Hostname of your MQTT broker (e.g., `mosquitto.default.svc.cluster.local`)
- **MQTT_PORT**: Port number (default: 1883 for unencrypted, 8883 for TLS)
- **MQTT_USER**: Username for MQTT authentication (e.g., `nova-agent`)
- **NOVA_URL** (optional): If Nova runs elsewhere, customize the sidecar connection
- **OLLAMA_URL** (optional): If Ollama runs on a different host

### 2. Base64 Encode the Password

```bash
# Encode your actual MQTT password
echo -n "your-actual-mqtt-password" | base64
# Output: eW91ci1hY3R1YWwtbXF0dC1wYXNzd29yZA==
```

### 3. Create/Update the Secret

#### Option A: Direct YAML Edit
Edit `k8s-mqtt-novelty.yaml` and update the Secret section:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: mqtt-credentials
  namespace: agents
type: Opaque
data:
  # Replace with your base64-encoded password
  password: eW91ci1hY3R1YWwtbXF0dC1wYXNzd29yZA==
stringData:
  mqtt-host: "mosquitto.default.svc.cluster.local"  # Update
  mqtt-port: "1883"                                   # Update if needed
  mqtt-user: "nova-agent"                             # Update if needed
  ollama-url: "http://ollama.agents.svc.cluster.local:11434"  # Add if needed
```

Then deploy:
```bash
kubectl apply -f k8s-mqtt-novelty.yaml
```

#### Option B: Create Secret from Command Line
```bash
# Create the secret with your values
kubectl create secret generic mqtt-credentials \
  --from-literal=password='your-actual-mqtt-password' \
  --from-literal=mqtt-host='mosquitto.default.svc.cluster.local' \
  --from-literal=mqtt-port='1883' \
  --from-literal=mqtt-user='nova-agent' \
  -n agents
```

#### Option C: Create Secret from File
Create a `.env` file:
```bash
# mqtt-credentials.env
password=your-actual-mqtt-password
mqtt-host=mosquitto.default.svc.cluster.local
mqtt-port=1883
mqtt-user=nova-agent
ollama-url=http://ollama.agents.svc.cluster.local:11434
```

Then create the secret:
```bash
kubectl create secret generic mqtt-credentials \
  --from-env-file=mqtt-credentials.env \
  -n agents
```

#### Option D: Using sealed-secrets (Recommended for Production)
For production environments, use sealed-secrets to encrypt credentials:

```bash
# Install sealed-secrets if not already installed
kubectl apply -f https://github.com/bitnami-labs/sealed-secrets/releases/download/v0.18.0/controller.yaml

# Create secret
kubectl create secret generic mqtt-credentials \
  --from-literal=password='your-actual-mqtt-password' \
  --from-literal=mqtt-host='mosquitto.default.svc.cluster.local' \
  --from-literal=mqtt-port='1883' \
  --from-literal=mqtt-user='nova-agent' \
  -n agents \
  --dry-run=client \
  -o yaml | kubeseal -f - > mqtt-credentials-sealed.yaml

# Deploy sealed secret
kubectl apply -f mqtt-credentials-sealed.yaml
```

#### Option E: Using External Secrets Operator (For Secret Management Systems)
```yaml
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: mqtt-secret-store
  namespace: agents
spec:
  provider:
    vault:  # Or AWS Secrets Manager, HashiCorp Vault, etc.
      server: "https://vault.example.com"
      path: "secret/mqtt"
      auth:
        kubernetes:
          mountPath: "kubernetes"
          role: "mqtt-novelty-verifier"
---
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: mqtt-credentials
  namespace: agents
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: mqtt-secret-store
    kind: SecretStore
  target:
    name: mqtt-credentials
    creationPolicy: Owner
  data:
  - secretKey: password
    remoteRef:
      key: mqtt_password
  - secretKey: mqtt-host
    remoteRef:
      key: mqtt_host
  - secretKey: mqtt-port
    remoteRef:
      key: mqtt_port
  - secretKey: mqtt-user
    remoteRef:
      key: mqtt_user
```

## Verification

After creating the secret, verify it was created correctly:

```bash
# Check secret exists
kubectl get secret mqtt-credentials -n agents

# View secret metadata (not values)
kubectl describe secret mqtt-credentials -n agents

# Verify pod can access it
kubectl exec -n agents <pod-name> -c mqtt-novelty-handler -- \
  env | grep MQTT
```

## Environment Variable Mapping

The secret keys map to these environment variables in the MQTT sidecar:

| Secret Key | Environment Variable | Container | Usage |
|------------|---------------------|-----------|-------|
| `password` | `MQTT_PASSWORD` | mqtt-novelty-handler | MQTT broker authentication |
| `mqtt-host` | `MQTT_HOST` | mqtt-novelty-handler | MQTT broker hostname/IP |
| `mqtt-port` | `MQTT_PORT` | mqtt-novelty-handler | MQTT broker port |
| `mqtt-user` | `MQTT_USER` | mqtt-novelty-handler | MQTT broker username |
| `ollama-url` | `OLLAMA_URL` | both containers | Ollama API endpoint |

## Security Best Practices

1. **Never commit secrets to version control**
   - Keep the actual password out of git
   - Use template files with placeholder values

2. **Use least privilege**
   - MQTT user should have minimal permissions needed
   - Restrict to only required topic subscriptions

3. **Encrypt at rest**
   - Enable encryption for secrets in etcd
   - Use sealed-secrets or external secret management for production

4. **Rotate credentials regularly**
   - Change MQTT password periodically
   - Update secret and restart pods

5. **Audit access**
   - Monitor RBAC policies
   - Log who accesses the secret

## Troubleshooting

### Secret Not Found
```bash
# Check if secret exists in correct namespace
kubectl get secrets -n agents | grep mqtt-credentials

# Check secret keys
kubectl get secret mqtt-credentials -n agents -o json | \
  jq '.data | keys'
```

### Pod Cannot Connect to MQTT
```bash
# Check environment variables in pod
kubectl exec -n agents <pod-name> -c mqtt-novelty-handler -- env | grep MQTT

# Check logs
kubectl logs -n agents <pod-name> -c mqtt-novelty-handler

# Test MQTT connectivity from pod
kubectl exec -n agents <pod-name> -c mqtt-novelty-handler -- \
  mosquitto_sub -h $MQTT_HOST -p $MQTT_PORT -u $MQTT_USER -P $MQTT_PASSWORD -t "test" -W 3
```

### MQTT_PASSWORD Environment Variable Not Set
```bash
# Verify secret has password key
kubectl get secret mqtt-credentials -n agents -o jsonpath='{.data}' | jq .

# Check pod volume mounts
kubectl describe pod -n agents <pod-name> | grep -A 20 "Mounts:"

# Verify secret reference in pod spec
kubectl get pod -n agents <pod-name> -o yaml | grep -A 10 "secretKeyRef"
```

## Updating Credentials

To update the password without recreating the deployment:

```bash
# Get current secret values (except password)
kubectl get secret mqtt-credentials -n agents -o json > mqtt-secret.json

# Edit or recreate with new password
kubectl delete secret mqtt-credentials -n agents
kubectl create secret generic mqtt-credentials \
  --from-literal=password='new-password' \
  --from-literal=mqtt-host='...' \
  --from-literal=mqtt-port='...' \
  --from-literal=mqtt-user='...' \
  -n agents

# Force pod restart to pick up new credentials
kubectl rollout restart deployment mqtt-novelty-verifier -n agents
```

## Related Documentation

- [Kubernetes Secrets Documentation](https://kubernetes.io/docs/concepts/configuration/secret/)
- [Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets)
- [External Secrets Operator](https://external-secrets.io/)
- MQTT Sidecar Deployment: `k8s-mqtt-novelty.yaml`
