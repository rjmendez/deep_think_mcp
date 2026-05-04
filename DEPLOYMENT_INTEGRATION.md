# Deployment Pipeline Integration Guide

## Overview

The deployment pipeline has been integrated into the deep_think_mcp k3s production system as a new HTTP endpoint: **POST /self-improvement/deploy**

This endpoint orchestrates the Layer 5 Self-Improvement System's automated deployment pipeline, implementing a safe canary rollout strategy with automatic rollback capabilities.

## Architecture

### Components

1. **DeploymentPipeline** (`adversarial_testing/deployment_pipeline.py`)
   - Manages canary deployments with automatic rollback
   - Monitors error rate, timeout rate, and latency p99
   - Executes gradual rollout: 5% → 25% → 100%

2. **Deployment Endpoint** (`server.py`)
   - HTTP POST /self-improvement/deploy
   - Validates inputs and pre-flight checks
   - Invokes DeploymentPipeline for execution
   - Returns deployment status and metrics

3. **AdversarialStore** (`adversarial_testing/store.py`)
   - Wrapper for SQLite operations
   - Manages deployment_events table
   - Tracks deployment history and status

## Canary Deployment Stages

### Stage 1: 5% Canary (30 seconds)
- Routes 5% of traffic to new code
- Monitors for 30 seconds
- Strict error thresholds (4% increase allowed)
- Quick feedback on basic functionality

### Stage 2: 25% Gradual (2 minutes)
- Routes 25% of traffic
- Monitors for 120 seconds
- Standard error thresholds (2% increase allowed)
- Tests with moderate load

### Stage 3: 100% Full Rollout (5 minutes)
- Routes 100% of traffic
- Monitors for 300 seconds
- Standard thresholds maintained
- Final validation before completion

## Metric Thresholds

Rollback is triggered automatically if ANY threshold is exceeded:

| Metric | Threshold | Description |
|--------|-----------|-------------|
| Error Rate | 2.0% increase | Absolute increase from baseline |
| Timeout Rate | 5.0% increase | Absolute increase from baseline |
| Latency P99 | 20% increase | Percentage increase from baseline |

## API Specification

### Request

```http
POST /self-improvement/deploy HTTP/1.1
Content-Type: application/json

{
  "validation_id": "uuid",  # validation_results.id from validation_suite
  "plan_id": "uuid",        # self_improvement_plans.id
  "commit_sha": "abc123..."  # git commit SHA to deploy
}
```

### Response (Success)

```json
{
  "success": true,
  "error": null,
  "deployment_id": "uuid",
  "status": "completed",
  "details": {
    "deployment_id": "uuid",
    "status": "completed",
    "commit_sha": "abc123...",
    "stages_completed": 3,
    "metrics": {
      "baseline": {
        "error_rate": 1.0,
        "timeout_rate": 0.5,
        "p95_latency_ms": 100
      },
      "final": {
        "error_rate": 0.9,
        "timeout_rate": 0.4,
        "p95_latency_ms": 102
      }
    }
  }
}
```

### Response (Rollback)

```json
{
  "success": false,
  "error": "Deployment rolled back: Error rate spike: 3.5% > 2.0%",
  "deployment_id": "uuid",
  "status": "rolled_back",
  "details": {
    "deployment_id": "uuid",
    "status": "rolled_back",
    "commit_sha": "abc123...",
    "stages_completed": 2
  }
}
```

### Response (Invalid Input)

```json
{
  "success": false,
  "error": "Missing required fields: validation_id, plan_id, commit_sha",
  "status_code": 400
}
```

## Pre-Flight Checks

Before deployment begins, the endpoint performs:

1. **Validation Check**
   - Verifies validation_results record exists
   - Confirms status is "passed"
   - Returns 400 if validation failed

2. **Plan Existence Check**
   - Verifies self_improvement_plans record exists
   - Checks deployment_sha is present
   - Returns 404 if plan not found

## Database Schema

### deployment_events Table

```sql
CREATE TABLE deployment_events (
  id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (plan_id) REFERENCES self_improvement_plans(id)
);
```

### Audit Trail

Deployment lifecycle is logged in `layer5_audit_log`:

```json
{
  "event": "deployment_completed",
  "plan_id": "uuid",
  "finding_id": "uuid",
  "details": {
    "deployment_id": "uuid",
    "commit_sha": "abc123...",
    "status": "completed",
    "stages_completed": 3
  },
  "timestamp": "2024-05-03T12:34:56Z"
}
```

## Usage Example

### Step 1: Run Validation
```python
from adversarial_testing.validation_suite import ValidationSuite

suite = ValidationSuite(store, metrics)
passed, error, details = await suite.validate_implementation(plan_id, commit_sha)

if passed:
    validation_id = details["validation_id"]
```

### Step 2: Deploy
```bash
curl -X POST http://localhost:8080/self-improvement/deploy \
  -H "Content-Type: application/json" \
  -d '{
    "validation_id": "validation-uuid",
    "plan_id": "plan-uuid",
    "commit_sha": "abc123..."
  }'
```

### Step 3: Check Status
```python
deployment = await pipeline.get_deployment_status(deployment_id)
print(f"Status: {deployment['status']}")
print(f"Commit: {deployment['commit_sha']}")
```

## Environment Variables

Configure deployment behavior with environment variables:

```bash
# Prometheus metrics endpoint
export PROMETHEUS_ENDPOINT="http://prometheus:9090"

# k3s namespace for deployment
export K3S_NAMESPACE="agents"

# Deployment resource name
export DEPLOYMENT_NAME="deep-think"

# Adversarial testing database path
export ADVERSARIAL_DB="/home/user/.deep_think/adversarial.db"
```

## Monitoring and Debugging

### View Deployment History
```python
history = await pipeline.get_deployment_history(plan_id, limit=10)
for deployment in history:
    print(f"{deployment['deployment_id']}: {deployment['status']}")
```

### Check Deployment Status
```python
status = await pipeline.get_deployment_status(deployment_id)
print(json.dumps(status, indent=2))
```

### Query Deployment Events
```sql
SELECT * FROM deployment_events 
WHERE plan_id = 'plan-uuid'
ORDER BY created_at DESC
LIMIT 10;
```

### Query Audit Log
```sql
SELECT * FROM layer5_audit_log
WHERE event IN ('deployment_completed', 'deployment_rolled_back')
ORDER BY timestamp DESC
LIMIT 20;
```

## Rollback Process

### Automatic Triggers

Rollback occurs automatically when:

1. **Error Rate Spike**
   - Current error rate > baseline + 2%
   - Example: baseline 1.0% → current 3.5% → rollback

2. **Timeout Rate Spike**
   - Current timeout rate > baseline + 5%
   - Example: baseline 0.1% → current 5.5% → rollback

3. **Latency Increase**
   - p99 latency > baseline * 1.2 (20% increase)
   - Example: baseline 100ms → current 125ms → rollback

### Recovery Steps

When rollback is triggered:

1. Identify last successful deployment tag
2. Extract stable commit SHA from git tag
3. Update k3s deployment to stable image
4. Verify rollout status
5. Create rollback audit tag: `layer5-deploy-TIMESTAMP-rollback`
6. Log event in deployment_events table

### Manual Rollback

If automatic rollback fails:

```bash
# Find last successful deployment
git tag -l 'layer5-deploy-*-completed' | sort | tail -5

# Extract commit SHA
STABLE_SHA=$(git rev-list -n 1 layer5-deploy-20240503-completed)

# Rollback pod image
kubectl set image deployment/deep-think \
  deep-think=deep-think:${STABLE_SHA} \
  -n agents

# Verify rollout
kubectl rollout status deployment/deep-think -n agents
```

## Testing

### Run All Tests
```bash
cd /home/USER/development/deep_think_mcp
python3 -m pytest adversarial_testing/tests/test_deployment_integration.py -v
```

### Run Specific Test Categories

```bash
# Canary weight calculation tests
python3 -m pytest adversarial_testing/tests/test_deployment_integration.py::TestCanaryWeightCalculation -v

# Rollback trigger logic tests
python3 -m pytest adversarial_testing/tests/test_deployment_integration.py::TestRollbackTriggerLogic -v

# Integration tests
python3 -m pytest adversarial_testing/tests/test_deployment_integration.py::TestDeploymentPipelineIntegration -v

# Edge case tests
python3 -m pytest adversarial_testing/tests/test_deployment_integration.py::TestDeploymentEdgeCases -v

# Metric threshold tests
python3 -m pytest adversarial_testing/tests/test_deployment_integration.py::TestMetricThresholds -v
```

### Test Coverage

The integration includes 24 comprehensive tests:

**Unit Tests (6 tests)**
- Canary weight calculations (5% → 25% → 100%)
- Pod replica count calculations
- Weight-to-replica mapping

**Rollback Logic Tests (6 tests)**
- Error rate spike detection
- Timeout rate spike detection
- Latency spike detection
- Green metrics (no rollback scenarios)
- Stricter canary thresholds
- Below-threshold scenarios

**Integration Tests (5 tests)**
- Deployment stages execute in order
- Rollback during stage 1
- Rollback during stage 3
- Git tag creation on success
- Database updates on completion

**Edge Case Tests (3 tests)**
- Pod weight update failure handling
- Prometheus metrics unavailability
- Git tag creation failure (non-critical)

**Configuration Tests (4 tests)**
- Error rate threshold validation
- Timeout rate threshold validation
- Latency threshold validation
- Monitoring duration validation

## Integration with Layer 5 Self-Improvement

The deployment pipeline is the final step in the Layer 5 workflow:

```
┌─────────────────────────────────────────────────────────┐
│ Layer 5 Self-Improvement Workflow                       │
├─────────────────────────────────────────────────────────┤
│ 1. Adversarial Testing → Findings                       │
│ 2. Planning Engine → Plans (AI-generated fixes)         │
│ 3. Implementation Pipeline → Commits                    │
│ 4. Validation Suite → Validation Results (passed/fail)  │
│ 5. Deployment Pipeline → Live Production Rollout        │ ← NEW
│    - Canary: 5%                                         │
│    - Gradual: 25%                                       │
│    - Full: 100%                                         │
│    - Monitor & Rollback if needed                       │
└─────────────────────────────────────────────────────────┘
```

## Performance Characteristics

### Deployment Timeline

- **Stage 1 (5% Canary)**: ~30 seconds
- **Stage 2 (25% Gradual)**: ~120 seconds
- **Stage 3 (100% Full)**: ~300 seconds
- **Total if successful**: ~8 minutes
- **Total if rollback at stage 1**: ~2 minutes

### Resource Requirements

- **CPU**: Minimal (metrics polling only)
- **Memory**: ~50MB for pipeline instance
- **Prometheus**: 5m metric window queries
- **k3s**: Standard Istio/Envoy for traffic routing

### Database Impact

- **deployment_events inserts**: 1-3 per deployment
- **audit_log inserts**: 1 per deployment
- **Query overhead**: <100ms per query

## Troubleshooting

### Deployment Stuck in Stage 1

**Symptom**: Deployment progresses very slowly or not at all

**Possible Causes**:
- Prometheus endpoint unreachable
- k3s cluster networking issues
- Pod readiness probes failing

**Solution**:
```bash
# Check Prometheus connectivity
curl http://localhost:9090/api/v1/query?query=up

# Check pod status
kubectl get pods -n agents -l app=deep-think

# Check rollout status
kubectl rollout status deployment/deep-think -n agents
```

### Rollback Not Triggered

**Symptom**: Metrics show spike but rollback didn't occur

**Possible Causes**:
- Metrics not available from Prometheus
- Threshold calculations incorrect
- Git tag creation failed

**Solution**:
```bash
# Check Prometheus metrics manually
curl 'http://localhost:9090/api/v1/query?query=error_rate'

# Verify git can create tags
git tag test-tag && git tag -d test-tag

# Check logs for errors
kubectl logs -l app=deep-think -n agents -f
```

### Validation Pre-Flight Check Failing

**Symptom**: Endpoint returns 400 "Validation did not pass"

**Possible Causes**:
- Wrong validation_id
- Validation status not "passed"
- Validation record doesn't exist

**Solution**:
```sql
-- Find validation records
SELECT id, status FROM validation_results 
WHERE plan_id = 'plan-uuid'
ORDER BY created_at DESC;

-- Check validation status
SELECT * FROM validation_results 
WHERE id = 'validation-uuid';
```

## Future Enhancements

### Planned Features

1. **Canary Traffic Split Improvements**
   - Per-user rollout (dark traffic)
   - Geographic-based canary
   - Header-based routing

2. **Enhanced Monitoring**
   - Custom metric thresholds per plan
   - Correlation-based rollback detection
   - Machine learning anomaly detection

3. **Gradual Rollout Control**
   - Manual stage progression
   - Custom stage durations
   - Webhook notifications

4. **A/B Testing Integration**
   - Compare old vs new in parallel
   - Statistical significance testing
   - User feedback collection

## Support

For issues or questions about the deployment integration:

1. Check test files: `adversarial_testing/tests/test_deployment_integration.py`
2. Review deployment_pipeline.py source code
3. Check MQTT novelty handling: `adversarial_testing/mqtt_novelty_handler.py`
4. Review Layer 5 documentation: `LAYER5_IMPLEMENTATION_SUMMARY.md`
