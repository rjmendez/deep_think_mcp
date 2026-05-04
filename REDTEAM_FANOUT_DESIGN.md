# Red Team Multi-Perspective Deep-Think Design Template

## Overview

This template demonstrates the **fan-out reasoning architecture** for coordinated multi-perspective analysis with red team emphasis, Nova grounding, and budget constraints.

**Use Case:** Security vulnerability analysis, architectural review, adversarial testing, compliance evaluation

**Key Feature:** 3 parallel reasoning agents with different mandates, each running 2 sequential reasoning passes, synthesized by a heavyweight model.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                   DEEP_THINK_FAN_OUT JOB                         │
│              (Width=3, Height=2, Synthesis=1)                    │
└─────────────────────────────────────────────────────────────────┘

                    Question / Problem Statement
                              ↓
        ┌─────────────────────────────────────┐
        │  PERSPECTIVE 1: RED TEAM             │
        │  (Adversarial Reasoning)             │
        │  Provider: Abliteration              │
        │  Model: dolphin-2.9-2b               │
        │  Pass 1: Attack Surface Analysis     │
        │  Pass 2: Exploit Chain Discovery     │
        └─────────────────────────────────────┘
                         ↓
        ┌─────────────────────────────────────┐
        │  PERSPECTIVE 2: DEFENSE              │
        │  (Grounded Reasoning + Nova)         │
        │  Provider: Anthropic                 │
        │  Model: Haiku→Sonnet                 │
        │  Pass 1: Constraint Validation       │
        │  Pass 2: Fact-Checking + Nova Search │
        └─────────────────────────────────────┘
                         ↓
        ┌─────────────────────────────────────┐
        │  PERSPECTIVE 3: PRACTICAL            │
        │  (Actionable Fixes)                  │
        │  Provider: Anthropic (Fast)          │
        │  Model: Haiku→Sonnet                 │
        │  Pass 1: Risk/Effort Scoring         │
        │  Pass 2: Remediation Planning        │
        └─────────────────────────────────────┘
                         ↓
        ┌─────────────────────────────────────┐
        │  SYNTHESIS PASS                      │
        │  (Integration + Consensus)           │
        │  Provider: Anthropic                 │
        │  Model: Opus 4.7                     │
        │  Output: Convergence + Divergence    │
        └─────────────────────────────────────┘
                         ↓
                    Final Report
```

---

## API Call Template

### Basic Structure
```python
from deep_think_mcp import deep_think_fan_out

response = await deep_think_fan_out(
    question="What are the critical security vulnerabilities in [TARGET_SYSTEM]?",
    
    # Reasoning Shape
    width=3,              # 3 perspectives (Red Team, Defense, Practical)
    height=2,             # 2 sequential passes per perspective
    
    # Task Class & Constraints
    task_class="adversarial",  # Main class; can override per-perspective
    
    # Provider Configuration
    provider_config={
        "provider": "anthropic",
        "light": "claude-haiku-4-5",      # Fast passes
        "medium": "claude-sonnet-4-6",    # Medium passes
        "heavy": "claude-opus-4-7",       # Synthesis only
        # Abliteration auto-routed for adversarial perspectives
    },
    
    # Safety & Performance
    max_parallel=2,            # Run 2 perspectives at a time
    max_width=3,               # Max 3 perspectives total
    confidence_threshold=60,   # Trigger expansion if <60%
    extract_claims=True,       # Extract + verify claims
    
    # Data Policy
    data_policy="any",         # Allow cloud + local providers
)

job_id = response['job_id']
print(f"Job queued: {job_id}")
```

### Polling for Results
```python
import asyncio
from deep_think_mcp import get_thinking_result

# Poll until complete
while True:
    result = await get_thinking_result(job_id)
    if result['status'] == 'complete':
        break
    print(f"Status: {result['status']}, Elapsed: {result.get('elapsed', '?')}s")
    await asyncio.sleep(10)

# Process results
final_answer = result['final_answer']
confidence = result.get('confidence_score', 0)
convergence = result.get('converged_claims', [])
divergence = result.get('contested_areas', [])

print(f"\nFinal Answer:\n{final_answer}")
print(f"\nConfidence Score: {confidence}")
print(f"Converged Claims: {len(convergence)}")
print(f"Contested Areas: {len(divergence)}")
```

---

## Perspective Specifications

### Perspective 1: Red Team (Adversarial)

**Mandate:** Discover vulnerabilities, attack chains, and exploitation scenarios

**Reasoning Directive:**
```
Pass 1: Attack Surface Analysis
  - What entry points exist?
  - What trust boundaries can be crossed?
  - What assumptions are critical?
  - What controls are missing?
  
Pass 2: Exploit Chain Discovery
  - How can entry points be chained?
  - What's the path to critical impact?
  - What's the minimum privilege needed?
  - What's the detection/mitigation difficulty?
```

**Provider:** Abliteration (dolphin-2.9-2b-llama2)
- Uncensored, creative reasoning
- Specialized for adversarial scenarios
- Budget: ~$0.05 (2 passes × 250 tokens)
- Safety: Output filtering, ironlaw logging

**Task Class:** `adversarial`
- Blocks research tools (nova_search, web_search)
- Blocks cloud providers in adversarial contexts
- Enables Abliteration provider
- Output redacted (no credentials/PII leaks)

**Expected Output:**
- List of vulnerabilities (CVE-style descriptions)
- Attack chains (step-by-step exploitation)
- Impact assessment (CVSS-style scoring)
- Effort/Feasibility (ease of exploitation)

---

### Perspective 2: Defense (Grounded)

**Mandate:** Ground red team findings in reality, verify claims, identify constraints

**Reasoning Directive:**
```
Pass 1: Constraint Validation
  - What architectural constraints exist?
  - What assumptions does the red team make?
  - Are those assumptions valid?
  - What defenses already exist?
  
Pass 2: Fact-Checking + Nova Grounding
  - For each red team claim:
    - Is it technically accurate?
    - Can it be verified in Great Library?
    - What's the confidence level?
  - What defensive patterns apply?
```

**Provider:** Anthropic
- Light Pass: Haiku 4.5 (fast constraint checking)
- Medium Pass: Sonnet 4.6 (detailed grounding with Nova)
- Budget: ~$0.15 (2 passes × 1200 tokens)

**Task Class:** `research`
- Enables `nova_search` for claim verification
- Enables web search (if whitelisted)
- Enables DAMA sensor queries
- Full proof chains returned

**Integration Points:**
- `nova_search("CVE-XXXX", top=5)` — Verify CVE claims
- `nova_search("authentication bypass [system]")` — Search patterns
- Proof chain extraction: [source_type, source_id, confidence]

**Expected Output:**
- Verified vs. unverified claims
- Proof chains for each claim
- Architectural constraints document
- Defensive patterns applicable

---

### Perspective 3: Practical (Actionable)

**Mandate:** Translate findings into concrete, actionable remediation

**Reasoning Directive:**
```
Pass 1: Risk/Effort Scoring
  - For each vulnerability:
    - CVSS-style severity (0-10)
    - Likelihood (rare/occasional/frequent)
    - Business impact ($M per incident)
    - Effort to fix (days/weeks)
    - Risk of fix (regression probability)
    
Pass 2: Remediation Planning
  - Short-term mitigations (days)
  - Medium-term fixes (weeks)
  - Long-term architectural changes (months)
  - Detection/monitoring strategies
  - Cost/benefit analysis
```

**Provider:** Anthropic (fast/cheap)
- Light Pass: Haiku 4.5 (risk scoring)
- Medium Pass: Sonnet 4.6 (detailed planning)
- Budget: ~$0.12 (2 passes × 800 tokens)

**Task Class:** `general`
- Limited research (Nova only, no web)
- Focus on practical execution
- Emphasize actionability

**Expected Output:**
- Prioritized fix list (by risk × impact ÷ effort)
- Detailed remediation steps (with code examples if applicable)
- Cost/timeline estimates
- Success metrics / acceptance criteria

---

## Synthesis Pass Specification

**Model:** Claude Opus 4.7 (heavyweight)

**Input:** All 3 perspective outputs + claim extraction

**Mandate:**
```
1. Identify Convergence
   - What do all 3 perspectives agree on?
   - Confidence: 80%+ agreement threshold
   - Strength: Multiple perspectives validating same claim

2. Identify Divergence
   - What do perspectives disagree on?
   - Why? (Different assumptions, missing context, constraints)
   - How to resolve?

3. Generate Integrated Report
   - Executive summary (1 paragraph)
   - Critical findings (converged, high confidence)
   - Contested areas (requires further investigation)
   - Recommended actions (from practical perspective)
   - Open questions (for future deep-think jobs)

4. Confidence Scoring
   - Overall confidence (0-100)
   - Per-claim confidence (if claim extraction enabled)
   - Contested claim flags
```

**Output Format:**
```json
{
  "final_answer": "Integrated findings report...",
  "confidence_score": 78,
  "converged_claims": [
    {
      "claim": "System lacks input validation on API endpoint X",
      "sources": ["red_team_pass1", "defense_pass2"],
      "confidence": 95,
      "severity": "CRITICAL"
    },
    ...
  ],
  "contested_areas": [
    {
      "claim": "Vulnerability is exploitable in production",
      "red_team_position": "Yes (high probability)",
      "defense_position": "Uncertain (depends on deployment config)",
      "practical_position": "Yes (but high effort)",
      "recommendation": "Requires architecture review"
    },
    ...
  ],
  "recommended_actions": [
    "Immediate: Deploy WAF rule for endpoint X",
    "Week 1: Add input validation in API layer",
    "Week 2: Security audit of auth boundaries",
    ...
  ],
  "open_questions": [
    "Can input validation be bypassed via [vector]?",
    "Is there persistent storage of user input?",
    ...
  ]
}
```

---

## Cost & Budget Analysis

### Token Estimation

| Component | Passes | Model(s) | Avg Tokens/Pass | Total Tokens | Cost |
|---|---|---|---|---|---|
| **Red Team** | 2 | Abliteration dolphin | 250 | 500 | $0.05 |
| **Defense** | 2 | Haiku + Sonnet | 1,200 | 2,400 | $0.15 |
| **Practical** | 2 | Haiku + Sonnet | 800 | 1,600 | $0.12 |
| **Synthesis** | 1 | Opus 4.7 | 2,000 | 2,000 | $0.18 |
| **TOTAL** | 7 | Mixed | ~1,193 avg | 6,500 | **$0.50** |

**Budget Headroom:** $1.00 target - $0.50 actual = **$0.50 buffer**

### Abliteration Usage
- Budget: $20/month (program cap)
- This test: $0.05 (~0.25% of monthly budget)
- Keys: 3-key rotation with health checks
- Logging: Full ironlaw audit trail

### Anthropic (Copilot) Spend
- Haiku 4.5: $0.80 per 1M tokens (~$0.001 per 1250 tokens)
- Sonnet 4.6: $3 per 1M tokens (~$0.003 per 1250 tokens)
- Opus 4.7: $15 per 1M tokens (~$0.015 per 1250 tokens)

---

## Safety & Constraint Enforcement

### Task Class Controls

**Red Team (adversarial)**
```python
task_class="adversarial"
# Prevents:
# - Research tools (nova_search, web_search)
# - Cloud providers in adversarial context
# - Data leakage to external systems
#
# Enables:
# - Abliteration provider
# - Creative, unconstrained reasoning
# - Output filtering (credentials, PII redaction)
```

**Defense (research)**
```python
task_class="research"
# Enables:
# - nova_search (Great Library verification)
# - web_search (if whitelisted domains)
# - DAMA sensor queries
# - Proof chain tracking
```

**Practical (general)**
```python
task_class="general"
# Enables:
# - Nova search (limited)
# - Standard models
# - Practical reasoning
```

### Escalation Framework

**Approval Gates:**
- CRITICAL findings: Manual approval required
- HIGH findings: Owner approval or auto if reproducibility=ALWAYS
- MEDIUM/LOW findings: Automatic approval

**Budget Checks:**
- Per-provider daily limits
- Alerts at 80% budget consumption
- Blocking at 100% (with escalation)

### Abliteration Output Filtering

```python
# Automatic redaction for:
# - API keys, credentials
# - Email addresses
# - IP addresses (except internal ranges)
# - Sensitive file paths
# - Database connection strings

# Output stamped: "[INTERNAL_ONLY - IRONLAW_MONITORED]"
```

---

## Running the Red Team Test

### Prerequisites
- Deep-think MCP deployed and healthy
- Nova server accessible
- Abliteration keys configured in ~/.abliteration/credentials
- Anthropic API key set (via personal key or copilot token)

### Execution
```bash
# Start the async job
job_id=$(curl -X POST http://100.73.200.19:30830/think/fan-out \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What are critical security vulnerabilities in [TARGET_SYSTEM]?",
    "width": 3,
    "height": 2,
    "task_class": "adversarial",
    "max_parallel": 2,
    "extract_claims": true,
    "provider_config": {
      "light": "claude-haiku-4-5",
      "medium": "claude-sonnet-4-6",
      "heavy": "claude-opus-4-7"
    }
  }' | jq -r '.job_id')

echo "Job ID: $job_id"

# Poll for results (every 30 seconds)
while true; do
  status=$(curl -s http://100.73.200.19:30830/think/result/$job_id | jq -r '.status')
  if [ "$status" = "complete" ]; then
    curl -s http://100.73.200.19:30830/think/result/$job_id | jq .
    break
  fi
  echo "Status: $status, waiting..."
  sleep 30
done
```

### Monitoring

**Metrics to Track:**
- Total execution time (target: <10 min)
- Token usage per perspective
- Confidence score (target: >70%)
- Cost per job ($0.50-$0.65)
- Abliteration key usage (even distribution)
- Nova search query count

**Dashboard:**
- Prometheus: `/metrics` endpoint (if enabled)
- Logs: `kubectl logs -f deployment/deep-think-mcp -n agents`

---

## Multi-Perspective Reasoning Extensions

### Future Enhancements

1. **4+ Perspectives**
   - Add: Compliance / Legal perspective
   - Add: Performance / Scalability perspective
   - Add: Usability / UX perspective

2. **Nested Fan-Outs**
   - Red team spawns sub-perspectives (for different attack types)
   - Each converges to a finding
   - Top-level synthesis integrates sub-findings

3. **Iterative Refinement**
   - If divergence too high: spawn targeted investigation perspective
   - If low confidence: add expert perspective (human-guided)

4. **Agent Coordination**
   - Perspectives can "debate" findings
   - Evidence passing between agents
   - Consensus-building mechanisms

5. **Domain-Specific Perspectives**
   - Security: Red Team, Defense, Compliance
   - Data Science: Analysis, Validation, Business Impact
   - Software: Correctness, Performance, Maintainability

---

## References

- **Deep-Think MCP API:** `/docs/deep_think_api.md`
- **Task Class Enforcement:** `/docs/task_class_enforcement.md`
- **Abliteration Integration:** `/docs/abliteration_integration.md`
- **Nova Grounding:** `/docs/nova_grounding.md`
- **Multi-Perspective Design:** `/docs/fan_out_architecture.md`

