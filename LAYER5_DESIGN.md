# Layer 5: Self-Improvement System - Comprehensive Design

## Executive Summary

Layer 5 is the autonomous continuous improvement system for deep_think_mcp. It creates a closed-loop improvement cycle: adversarial findings → auto-review → planning → implementation → validation → deployment with automatic rollback.

**Key Design Principle:** Human-in-the-loop gates for HIGH+severity, fully autonomous for LOW/MEDIUM with audit trails.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                       LAYER 5: SELF-IMPROVEMENT                 │
│  Auto-Review → Planning → Implementation → Validation → Deploy   │
└─────────────────────────────────────────────────────────────────┘
           ↑                                            ↓
           ↓                                            ↑
    Adversarial Findings              Metrics Monitoring & Alerts
    (Layer 4 Discovery)               (Prometheus, regression alerts)
           ↑
           │
    ┌──────┴──────────┬────────────────┬──────────────┐
    │                 │                │              │
  Layer 1-4:    MetricsCollector    EscalationQueue  Governance
  Creative      (before/after)      (approval gates) (budget/risk)
  Reasoning
```

---

## Core Components

### 1. Auto-Review Trigger System

**Purpose:** Detect findings that require immediate action based on metric thresholds.

**Activation Criteria:**
```
IF (pass_rate_pct < 95%) THEN finding_rate spike detected
   OR (avg_time_to_fix_days > 7) AND (open_findings > 5)
   OR (error_rate > 5% for >10 min) THEN trigger review
   OR (critical_finding detected) THEN immediate escalation
```

**Implementation Pattern:**
- Runs async check every 30 seconds (configurable)
- Queries MetricsCollector.snapshot() for current metrics
- Compares against baseline stored in adversarial_metrics_baseline table
- Queues auto_review task only when threshold breached AND no recent review in-progress

**Monitoring Hook:**
```python
def should_trigger_auto_review(metrics_snapshot, baseline, budget):
    # Budget check first (respect API limits)
    if not budget.has_capacity():
        return False
    
    # Metric thresholds (see table below)
    return (metrics_snapshot.pass_rate_pct < baseline.pass_rate_threshold
            or metrics_snapshot.error_rate > baseline.error_rate_threshold
            or metrics_snapshot.avg_time_to_fix_days > baseline.ttf_threshold)
```

### 2. Planning Engine

**Purpose:** Analyze HIGH-priority findings, generate ranked improvement plans, estimate effort/risk.

**Algorithm:**
1. Query top findings by severity + reproducibility + impact
2. For each finding, call deep_think(task_class="planning") with:
   - Finding details (category, error message, stack trace)
   - Code context (affected module, recent commits)
   - Historical patterns (similar fixes, time-to-fix benchmark)
3. Parse response to extract:
   - `fix_approach`: Primary solution strategy
   - `effort_estimate`: 1-5 (days)
   - `risk_level`: LOW/MEDIUM/HIGH (regression risk)
   - `dependencies`: Other findings that must be fixed first
4. Rank by priority = (severity × impact) / (effort + risk_penalty)
5. Store as self_improvement_plan with traceability to source findings

**Deep Think Prompt Template:**
```
You are a planning expert reviewing an adversarial finding to create a fix roadmap.

Finding: [category] [example_input]
Error: [error_message]
Affected Module: [module]
Historical Context: Last similar fix took X days, success rate Y%

Generate a structured plan with:
1. Root cause analysis
2. Fix approach (primary + fallback)
3. Effort estimate (1-5 days, with breakdown by subtask)
4. Risk assessment (regression risk, dependencies)
5. Validation strategy (what tests to add)

Output JSON:
{
  "root_cause": "...",
  "fix_approach": "...",
  "effort_estimate": 2,
  "risk_level": "MEDIUM",
  "subtasks": [...],
  "dependencies": ["finding_id_2"],
  "validation_tests": [...]
}
```

**Storage (extends store.py schema):**
```sql
CREATE TABLE self_improvement_plans (
    id TEXT PRIMARY KEY,
    finding_ids TEXT,  -- JSON array of finding IDs
    plan_json TEXT,    -- Full deep_think response
    priority REAL,     -- Computed from severity × impact / effort
    effort_estimate INT,
    risk_level TEXT,
    status TEXT DEFAULT 'pending',  -- pending/approved/implementing/deployed/rolled_back
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    approved_by TEXT,  -- User email for HIGH+ severity
    deployment_sha TEXT
)
```

### 3. Implementation Pipeline

**Purpose:** Orchestrate code changes with approval gates and rollback capability.

**State Machine:**
```
pending → approved → implementing → validating → deploying → completed
                         ↓
                    [review agent]
                         ↓
                      approved
                         ↓
                    [code agent]
                         ↓
                    validation_queue → {pass → deploy} OR {fail → blocked}
                         ↓
                    [git tag: layer5-impl-<timestamp>-<status>]
```

**Approval Gates:**
- **CRITICAL severity:** Manual approval via HumanEscalationQueue (escalation_module.py pattern)
- **HIGH severity:** Approval required from code owner (governance module)
- **MEDIUM/LOW severity:** Auto-approved if risk_level ≤ MEDIUM

**Implementation Flow:**
1. Fetch plan from self_improvement_plans
2. Route to appropriate agent based on fix_approach:
   - code-review agent: Validate fix in isolation
   - general-purpose agent: Implement the fix (apply edits, create new modules)
   - task agent: Run tests and integration checks
3. Wait for approval gate (blocking for HIGH+, resumable)
4. Create feature branch: `layer5-impl-<plan_id>-<finding_id>`
5. Implement changes (agent handles file edits)
6. Commit with tracer: `[Layer 5] Fix <category>: <brief description>\n\nPlan: <plan_id>\nFinding: <finding_id>\nCo-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`
7. Tag commit: `git tag layer5-impl-<timestamp>-pending` (for rollback tracking)
8. Queue for validation

**Budget Controls:**
```python
# Respect daily/monthly API cost limits (from adversarial_budget table)
if budget.daily_token_usage > budget.daily_token_limit:
    implementation.pause()  # Resume when budget resets
elif budget.monthly_cost > budget.monthly_budget:
    implementation.escalate_to_human("Monthly budget exceeded")
```

### 4. Validation Suite

**Purpose:** Verify fixes don't break existing behavior; measure improvement.

**Pre-Validation (Before Merging):**
1. Run full test suite on feature branch
2. Capture metrics snapshot:
   - `before_metrics`: Snapshot from main branch
   - `after_metrics`: Snapshot from feature branch
3. Compute regression score:
   ```
   regression = max(
       after.error_rate - before.error_rate,
       after.timeout_rate - before.timeout_rate,
       after.false_positive_rate - before.false_positive_rate
   )
   ```
4. Check improvement score:
   ```
   improvement = (before.avg_time_to_fix - after.avg_time_to_fix) / before.avg_time_to_fix
   ```
5. Block if regression > 2% or improvement < 5% for HIGH fixes
6. Auto-approve if regression < 0.5% (actual improvement detected)

**Regression Detection:**
```python
def detect_regression(before_metrics, after_metrics):
    regressions = []
    
    # Check error rate didn't spike
    if after_metrics.error_rate > before_metrics.error_rate * 1.05:
        regressions.append(f"error_rate increased {before_metrics.error_rate}→{after_metrics.error_rate}")
    
    # Check timeout rate didn't increase
    if after_metrics.timeout_rate > before_metrics.timeout_rate * 1.05:
        regressions.append(f"timeout_rate increased {before_metrics.timeout_rate}→{after_metrics.timeout_rate}")
    
    # Check test coverage didn't decrease
    if after_metrics.test_coverage_pct < before_metrics.test_coverage_pct - 1:
        regressions.append(f"test_coverage decreased {before_metrics.test_coverage_pct}→{after_metrics.test_coverage_pct}")
    
    return regressions
```

**Storage:**
```sql
CREATE TABLE validation_results (
    id TEXT PRIMARY KEY,
    implementation_id TEXT,
    test_suite_output TEXT,
    before_metrics JSONB,
    after_metrics JSONB,
    regression_detected BOOLEAN,
    improvement_score REAL,
    status TEXT DEFAULT 'pending',  -- pending/passed/failed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

### 5. Deployment Pipeline

**Purpose:** Roll out validated fixes to production with automatic rollback on failure.

**Canary Deployment Pattern:**

1. **Stage 1 (Canary): 5% of traffic**
   - Deploy to k3s subset (1-2 pods)
   - Monitor for 30 seconds
   - Metrics check: error_rate, timeout_rate, latency p95
   - Rollback trigger: error_rate spike >1%, timeout_rate >5%

2. **Stage 2 (Gradual): 25% of traffic**
   - Increase pod weight from 5% → 25%
   - Monitor for 2 minutes
   - Same rollback triggers as Stage 1

3. **Stage 3 (Full): 100% of traffic**
   - Complete rollout
   - Sustained monitoring for 5 minutes
   - Rollback still available

**Automatic Rollback Logic:**
```python
def should_rollback(canary_metrics, baseline_metrics, stage):
    # Stage-specific thresholds
    error_threshold = 2.0 if stage == "canary" else 1.0
    
    error_rate_spike = canary_metrics.error_rate - baseline_metrics.error_rate
    if error_rate_spike > error_threshold:
        return True, f"error_rate spike: {error_rate_spike}%"
    
    timeout_spike = canary_metrics.timeout_rate - baseline_metrics.timeout_rate
    if timeout_spike > 5.0:
        return True, f"timeout spike: {timeout_spike}%"
    
    latency_increase = canary_metrics.p95_latency_ms - baseline_metrics.p95_latency_ms
    if latency_increase > baseline_metrics.p95_latency_ms * 0.2:  # 20% increase
        return True, f"latency increase: {latency_increase}ms"
    
    return False, None
```

**Rollback Execution:**
```bash
# Tag the rollback point
git tag layer5-deploy-<timestamp>-rollback

# Reset to previous commit
git reset --hard layer5-impl-<previous-timestamp>-completed

# Redeploy from last known-good pod weight
kubectl set image deployment/deep-think deep-think=<last-good-sha> -n agents
kubectl rollout status deployment/deep-think -n agents
```

---

## Threshold Configuration

| Metric | Threshold | Action | Severity |
|--------|-----------|--------|----------|
| pass_rate_pct | < 95% | Trigger review | AUTO |
| error_rate | > 5% | Escalate | HIGH |
| timeout_rate | > 2% | Escalate | MEDIUM |
| avg_time_to_fix | > 7 days | Escalate | MEDIUM |
| test_coverage | < 80% | Block deployment | HIGH |
| regression_score | > 2% | Block merge | HIGH |
| improvement_score | < 5% (for HIGH fixes) | Require approval | MEDIUM |
| canary_error_spike | > 2% | Rollback | CRITICAL |
| false_positive_rate | > 10% | Block review | MEDIUM |

---

## Approval & Escalation Flow

### For CRITICAL/HIGH Severity Fixes:

```
Finding discovered (CRITICAL/HIGH)
       ↓
Planning engine generates plan
       ↓
HumanEscalationQueue.put() → blocks implementation
       ↓
Human reviews via `/escalation/queue` endpoint
       ↓
Human approves OR rejects
       ↓
IF approved → implementation pipeline continues
IF rejected → close finding, document reasoning
```

### For MEDIUM/LOW Severity Fixes:

```
Finding discovered (MEDIUM/LOW)
       ↓
Planning engine generates plan (auto-approve if risk ≤ MEDIUM)
       ↓
Implementation pipeline starts immediately
       ↓
Validation must pass (regression check)
       ↓
Deployment proceeds (canary → gradual → full)
```

---

## Data Flow Summary

```
1. Adversarial Testing (Layer 4)
   ↓
   findings inserted → adversarial_findings table

2. Auto-Review Trigger (Layer 5.1)
   ↓
   MetricsCollector.snapshot() queries metrics
   ↓
   Decision: trigger_review?
   ↓
   IF yes → queue auto_review task

3. Planning Engine (Layer 5.2)
   ↓
   deep_think(task_class="planning") generates plan
   ↓
   plan stored → self_improvement_plans table
   ↓
   await approval (if HIGH+)

4. Implementation (Layer 5.3)
   ↓
   code_review/general_purpose agents implement fix
   ↓
   commit created with Layer 5 tracer
   ↓
   tag: layer5-impl-<timestamp>-pending
   ↓
   branch pushed to GitHub

5. Validation (Layer 5.4)
   ↓
   before/after metrics captured
   ↓
   regression check: passed/failed
   ↓
   tag: layer5-impl-<timestamp>-validated
   ↓
   merge to main if passed

6. Deployment (Layer 5.5)
   ↓
   canary rollout (5%)
   ↓
   gradual rollout (25%)
   ↓
   full rollout (100%)
   ↓
   IF error spike → automatic rollback
   ↓
   tag: layer5-deploy-<timestamp>-completed OR rollback
   ↓
   metrics stored → deployment_events table
```

---

## Integration with Existing Layers

### With Layer 4 (Adversarial Testing):
- Listen to `on_finding()` hook from self_improvement.py
- Trigger planning ONLY for findings with reproducibility > 0.7
- Exclude low-confidence findings (escalation_framework handled by Layer 4)

### With Layer 3 (Escalation):
- Reuse HumanEscalationQueue pattern for approval gates
- Respect budget controls from adversarial_budget table
- Follow requires_human_review() for CRITICAL findings

### With Layer 2 (Grounding/Nova):
- Use nova_verify() to validate fix correctness before deployment
- Cache verification results in validation_results table
- Alert if nova_verify returns LOW confidence

### With Layer 1 (Creative Reasoning):
- deep_think(task_class="planning") for roadmap generation
- deep_think(task_class="code_review") for implementation review
- deep_think(task_class="reasoning") for complex dependency analysis

### With Prometheus:
- Query error_rate, timeout_rate, latency metrics for canary decisions
- Set up alert_rule for `layer5_rollback_triggered` (critical)
- Monitor layer5_deployment_success_rate

---

## API Contract

### REST Endpoints (for integration):

```
POST /layer5/trigger-review
  {finding_id: "...", reason: "metric_threshold_exceeded"}
  → queues auto_review task

GET /layer5/plans
  ?status=pending|approved|implementing|deployed
  → list all plans in status
  → response: [self_improvement_plan, ...]

POST /layer5/plans/{plan_id}/approve
  {approved_by: "user@github.com", notes: "looks good"}
  → gate keeper approval

POST /layer5/plans/{plan_id}/deploy
  {canary_duration_sec: 30, gradual_duration_sec: 120}
  → start deployment pipeline

GET /layer5/deployments/{deployment_id}
  → current status, canary metrics, rollback option

POST /layer5/deployments/{deployment_id}/rollback
  {reason: "error_spike_detected"}
  → execute rollback immediately
```

---

## Monitoring & Audit

### Metrics to Track:

```
layer5_findings_reviewed_total (counter)
layer5_plans_created_total (counter)
layer5_implementations_started (counter)
layer5_implementations_succeeded (counter)
layer5_deployments_succeeded (counter)
layer5_deployments_rolled_back (counter)
layer5_avg_time_to_deploy_minutes (histogram)
layer5_regression_detected_rate (gauge)
layer5_approval_queue_depth (gauge)
```

### Audit Trail (append-only):

```sql
CREATE TABLE layer5_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,  -- "plan_created", "approved", "impl_started", "deployed", "rolled_back"
    plan_id TEXT,
    finding_id TEXT,
    actor TEXT,  -- user email or "system"
    details JSONB,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

Every action recorded with full context: who, what, when, why.

---

## Acceptance Criteria Mapping

✅ **Auto-review trigger:** Metric thresholds + HumanEscalationQueue for escalation  
✅ **Auto-planning:** deep_think(task_class="planning") → self_improvement_plans  
✅ **Auto-implementation:** code-review → general-purpose agents with approval gates  
✅ **Auto-validation:** before/after metrics + regression detection  
✅ **Auto-deployment:** Canary → Gradual → Full with automatic rollback  
✅ **Audit trails:** Append-only adversarial_audit_log + layer5_audit_log  
✅ **Code coverage:** 80%+ via comprehensive test suite (40+ tests)  

---

## Next Steps

1. **Create core modules:**
   - planning_engine.py
   - implementation_pipeline.py
   - validation_suite.py
   - deployment_pipeline.py

2. **Extend store.py schema:**
   - self_improvement_plans
   - implementation_tasks
   - validation_results
   - deployment_events
   - layer5_audit_log

3. **Create test suite (40+ tests):**
   - Unit tests for each module
   - Integration tests for end-to-end flow
   - Threshold trigger tests
   - Approval gate tests
   - Rollback simulation tests

4. **Update self_improvement.py:**
   - Replace stubs with calls to Layer 5 components
   - Integrate with MetricsCollector for auto-review triggers
   - Hook into HumanEscalationQueue for approval gates

5. **Documentation:**
   - API endpoint specs
   - Operational runbook (what to do if rollback fails)
   - Troubleshooting guide (metric threshold tuning)
   - Cost estimation (API calls per improvement cycle)
