#!/bin/bash
# MQTT Novelty Verifier - Kubernetes Deployment Example Script
# This script demonstrates the full deployment workflow

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  MQTT Novelty Verifier - Kubernetes Deployment Example     ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"

# ============================================================================
# STEP 1: Verify prerequisites
# ============================================================================

echo -e "\n${YELLOW}[Step 1]${NC} Checking prerequisites..."

# Check kubectl
if ! command -v kubectl &> /dev/null; then
    echo -e "${RED}✗ kubectl not found. Install kubectl first.${NC}"
    exit 1
fi
echo -e "${GREEN}✓ kubectl found${NC}"

# Check manifest file
if [ ! -f "k8s-mqtt-novelty.yaml" ]; then
    echo -e "${RED}✗ k8s-mqtt-novelty.yaml not found in current directory${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Manifest file found${NC}"

# Check kubeconfig
if [ ! -f ~/.kube/config ] && [ ! -f /etc/rancher/k3s/k3s.yaml ]; then
    echo -e "${RED}✗ No kubeconfig found${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Kubeconfig found${NC}"

# ============================================================================
# STEP 2: Configure MQTT credentials
# ============================================================================

echo -e "\n${YELLOW}[Step 2]${NC} MQTT Credential Configuration"
echo -e "You need to provide MQTT broker credentials.\n"

# Option to use defaults or custom values
read -p "Use default test values? (yes/no) [yes]: " USE_DEFAULTS
USE_DEFAULTS=${USE_DEFAULTS:-yes}

if [ "$USE_DEFAULTS" = "yes" ] || [ "$USE_DEFAULTS" = "y" ]; then
    MQTT_PASSWORD="changeme"
    MQTT_HOST="mosquitto.default.svc.cluster.local"
    MQTT_PORT="1883"
    MQTT_USER="nova-agent"
    echo -e "${GREEN}Using default values${NC}"
else
    echo -e "Enter your MQTT broker details:"
    read -p "MQTT Host [mosquitto.default.svc.cluster.local]: " MQTT_HOST
    MQTT_HOST=${MQTT_HOST:-mosquitto.default.svc.cluster.local}
    
    read -p "MQTT Port [1883]: " MQTT_PORT
    MQTT_PORT=${MQTT_PORT:-1883}
    
    read -p "MQTT Username [nova-agent]: " MQTT_USER
    MQTT_USER=${MQTT_USER:-nova-agent}
    
    read -sp "MQTT Password: " MQTT_PASSWORD
    echo
fi

echo -e "${GREEN}Configuration:${NC}"
echo "  Host: $MQTT_HOST"
echo "  Port: $MQTT_PORT"
echo "  User: $MQTT_USER"
echo "  Password: $(printf '%*s\n' ${#MQTT_PASSWORD} | tr ' ' '*')"

# ============================================================================
# STEP 3: Create namespace if needed
# ============================================================================

echo -e "\n${YELLOW}[Step 3]${NC} Setting up namespace..."

if kubectl get namespace agents &> /dev/null; then
    echo -e "${GREEN}✓ Namespace 'agents' already exists${NC}"
else
    echo "Creating namespace 'agents'..."
    kubectl create namespace agents
    echo -e "${GREEN}✓ Namespace 'agents' created${NC}"
fi

# ============================================================================
# STEP 4: Create or update the secret
# ============================================================================

echo -e "\n${YELLOW}[Step 4]${NC} Setting up Kubernetes secret..."

# Delete existing secret if it exists
if kubectl get secret mqtt-credentials -n agents &> /dev/null; then
    echo "Deleting existing secret..."
    kubectl delete secret mqtt-credentials -n agents
fi

# Create new secret
echo "Creating new secret with credentials..."
kubectl create secret generic mqtt-credentials \
  --from-literal=password="$MQTT_PASSWORD" \
  --from-literal=mqtt-host="$MQTT_HOST" \
  --from-literal=mqtt-port="$MQTT_PORT" \
  --from-literal=mqtt-user="$MQTT_USER" \
  -n agents

echo -e "${GREEN}✓ Secret created${NC}"

# Verify secret was created
if kubectl get secret mqtt-credentials -n agents &> /dev/null; then
    echo -e "${GREEN}✓ Secret verified${NC}"
else
    echo -e "${RED}✗ Secret creation failed${NC}"
    exit 1
fi

# ============================================================================
# STEP 5: Dry-run to validate manifest
# ============================================================================

echo -e "\n${YELLOW}[Step 5]${NC} Validating manifest (dry-run)..."

if kubectl apply -f k8s-mqtt-novelty.yaml --dry-run=client &> /dev/null; then
    echo -e "${GREEN}✓ Manifest validation passed${NC}"
else
    echo -e "${RED}✗ Manifest validation failed${NC}"
    echo "Run this for details:"
    echo "  kubectl apply -f k8s-mqtt-novelty.yaml --dry-run=client"
    exit 1
fi

# ============================================================================
# STEP 6: Apply manifest to cluster
# ============================================================================

echo -e "\n${YELLOW}[Step 6]${NC} Applying manifest to cluster..."

read -p "Ready to apply manifest to cluster? (yes/no) [yes]: " PROCEED
PROCEED=${PROCEED:-yes}

if [ "$PROCEED" != "yes" ] && [ "$PROCEED" != "y" ]; then
    echo "Deployment cancelled."
    exit 0
fi

kubectl apply -f k8s-mqtt-novelty.yaml
echo -e "${GREEN}✓ Manifest applied${NC}"

# ============================================================================
# STEP 7: Wait for deployment to be ready
# ============================================================================

echo -e "\n${YELLOW}[Step 7]${NC} Waiting for deployment to be ready..."
echo "(This may take 1-2 minutes...)"

kubectl rollout status deployment mqtt-novelty-verifier -n agents --timeout=5m
echo -e "${GREEN}✓ Deployment ready${NC}"

# ============================================================================
# STEP 8: Verify resources
# ============================================================================

echo -e "\n${YELLOW}[Step 8]${NC} Verifying resources..."

echo -e "\nDeployment Status:"
kubectl get deployment mqtt-novelty-verifier -n agents

echo -e "\nPods:"
kubectl get pods -n agents -l app=mqtt-novelty-verifier

echo -e "\nService:"
kubectl get svc mqtt-novelty-verifier -n agents

echo -e "\nConfigMap:"
kubectl get configmap mqtt-novelty-config -n agents

echo -e "\nSecret:"
kubectl get secret mqtt-credentials -n agents

# ============================================================================
# STEP 9: Display logs
# ============================================================================

echo -e "\n${YELLOW}[Step 9]${NC} Checking pod logs..."

PODS=$(kubectl get pods -n agents -l app=mqtt-novelty-verifier -o jsonpath='{.items[*].metadata.name}')

for POD in $PODS; do
    echo -e "\n${BLUE}Pod: $POD${NC}"
    echo "--- Nova Agent Logs ---"
    kubectl logs -n agents $POD -c nova-agent --tail=20 2>/dev/null || echo "No logs yet"
    
    echo "--- MQTT Handler Logs ---"
    kubectl logs -n agents $POD -c mqtt-novelty-handler --tail=20 2>/dev/null || echo "No logs yet"
done

# ============================================================================
# STEP 10: Post-deployment instructions
# ============================================================================

echo -e "\n${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║           Deployment Complete!                             ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"

echo -e "\n${BLUE}Next steps:${NC}"
echo "1. Monitor logs:"
echo "   kubectl logs -f -n agents <pod-name> -c mqtt-novelty-handler"
echo ""
echo "2. Port-forward to test locally:"
echo "   kubectl port-forward -n agents svc/mqtt-novelty-verifier 8000:8000"
echo ""
echo "3. Test Nova endpoint:"
echo "   curl http://localhost:8000/health"
echo ""
echo "4. Test metrics endpoint:"
echo "   curl http://localhost:9090/metrics"
echo ""
echo "5. View deployment details:"
echo "   kubectl describe deployment mqtt-novelty-verifier -n agents"
echo ""
echo "6. Check resource usage:"
echo "   kubectl top pods -n agents --containers"
echo ""
echo "7. To scale manually:"
echo "   kubectl scale deployment mqtt-novelty-verifier --replicas=3 -n agents"
echo ""
echo "8. To delete the deployment:"
echo "   kubectl delete -f k8s-mqtt-novelty.yaml"
echo ""

echo -e "${BLUE}Documentation:${NC}"
echo "  - SECRETS_SETUP.md     - Secret configuration details"
echo "  - DEPLOYMENT_GUIDE.md  - Detailed deployment instructions"
echo "  - QUICKREF.md          - Quick reference and common commands"
echo "  - MANIFEST_SUMMARY.md  - Complete manifest documentation"

