# Critical Bug Fixes - Deep Think MCP

## Summary
Fixed 2 critical bugs that could cause job processing failures in production:
1. Race condition in job claiming (multiple workers could claim the same job)
2. Status mismatch causing requeued jobs to be stuck forever

## Bug 1: Race Condition in Job Claiming (CRITICAL)

### Problem
**Location**: `store.py` lines 374-397 (claim_next_job function)

The original implementation had a SELECT followed by UPDATE pattern:
```python
row = conn.execute("SELECT * FROM thinking_jobs WHERE status='queued' ...").fetchone()
# <-- Race window here: another worker could claim the same job
conn.execute("UPDATE thinking_jobs SET status='running' ... WHERE job_id=?", (row["job_id"],))
```

**Race scenario**:
1. Worker A: SELECT finds job X (status='queued')
2. Worker B: SELECT finds job X (status='queued') ← both see same job!
3. Worker A: UPDATE job X to running, claimed_by='worker-A'
4. Worker B: UPDATE job X to running, claimed_by='worker-B' ← overwrites A's claim!
5. Both workers think they own job X and execute it simultaneously

**Impact**: Duplicate job execution, wasted resources, potential data corruption

### Solution
Added `WHERE status='queued'` condition to the UPDATE statement and check `rowcount`:

```python
cur = conn.execute(
    "UPDATE thinking_jobs SET status='running', started_at=?, claimed_by=?, claimed_at=? "
    "WHERE job_id=? AND status='queued'",  # ← Added status='queued' check
    (now, worker_id, now, row["job_id"]),
)
# If no rows were updated, another worker claimed this job first
if cur.rowcount == 0:
    conn.execute("ROLLBACK")
    return None
```

**How it prevents the race**:
- Worker A's UPDATE succeeds (1 row updated), commits, changes status to 'running'
- Worker B's UPDATE finds status='running' (not 'queued'), so 0 rows updated
- Worker B sees rowcount=0, rolls back, returns None → knows it didn't get the job
- Only one worker successfully claims the job

### Test Coverage
Added comprehensive tests in `tests/test_race_condition.py`:
- `test_concurrent_claim_only_one_succeeds`: 10 workers race to claim 1 job → exactly 1 succeeds
- `test_sequential_claims_after_requeue`: Verify claim → requeue → claim cycle works
- `test_claim_only_works_on_queued_jobs`: Verify running jobs can't be re-claimed

## Bug 2: Status Mismatch - Pending vs Queued (CRITICAL)

### Problem
**Location**: `store.py` line 595 (requeue_orphaned_job function)

Status value inconsistency across the codebase:
- `create_job()` → sets status='queued'
- `claim_next_job()` → filters for status='queued'
- `requeue_stale()` → sets status='queued'
- `requeue_orphaned_job()` → **sets status='pending'** ← BUG!

**Impact**: 
When a job times out and gets requeued via `requeue_orphaned_job()`:
1. Job status changed to 'pending'
2. Workers only claim jobs with status='queued'
3. Job stuck in 'pending' forever, never processed
4. Silent data loss - jobs disappear into the void

This is a **silent failure mode** - no errors, just jobs that never complete.

### Solution
Changed `requeue_orphaned_job()` to use 'queued' instead of 'pending':

```python
def requeue_orphaned_job(job_id: str, reason: str = "orphan_timeout") -> bool:
    """Requeue an orphaned job by resetting its status to 'queued'."""
    # Changed from status='pending' to status='queued'
    cur = conn.execute(
        "UPDATE thinking_jobs SET status='queued', started_at=NULL, claimed_by=NULL, claimed_at=NULL "
        "WHERE job_id=? AND status='running'",
        (job_id,),
    )
```

### State Machine
The correct state machine for thinking_jobs:

```
'queued' ──claim──> 'running' ──complete──> 'complete'
   ↑                    │
   └────requeue─────────┘
                        │
                        └──fail──> 'failed'
```

**Valid status values**: 'queued', 'running', 'complete', 'failed'
**Removed**: 'pending' (was never part of the design, just a typo/mistake)

### Related Changes
Updated all references to 'pending' → 'queued' for consistency:
- `tests/test_orphan_detection.py`: Updated assertions
- `worker.py`: Updated docstring
- `ORPHAN_JOB_DETECTION.md`: Updated documentation

### Test Coverage
Added tests in `tests/test_race_condition.py`:
- `test_requeue_uses_queued_status`: Verify status='queued' after requeue
- `test_requeued_job_can_be_claimed`: End-to-end test: claim → requeue → claim again
- `test_multiple_requeue_cycles`: Verify jobs can be requeued multiple times
- `test_requeue_only_works_on_running_jobs`: Verify state transition rules

## Testing

### New Test Suite
Created `tests/test_race_condition.py` with 7 comprehensive tests:

```bash
$ python3 -m pytest tests/test_race_condition.py -v
================================= 7 passed in 0.09s =================================
```

### Existing Tests
Verified all existing tests still pass:

```bash
$ python3 -m pytest tests/test_orphan_detection.py -v
================================ 14 passed in 0.20s =================================
```

## Files Modified

### Core Logic Changes
- `store.py`:
  - Line 386-396: Fixed race condition in `claim_next_job()`
  - Line 589-603: Fixed status mismatch in `requeue_orphaned_job()`

### Test Updates
- `tests/test_orphan_detection.py`: Updated status assertions (pending → queued)
- `tests/test_race_condition.py`: NEW - comprehensive race condition tests

### Documentation Updates
- `worker.py`: Updated docstring
- `ORPHAN_JOB_DETECTION.md`: Updated documentation

## Verification

### Race Condition Test Results
```
✅ test_concurrent_claim_only_one_succeeds
   - 10 workers racing for 1 job
   - Exactly 1 worker succeeds
   - No duplicate claims

✅ test_sequential_claims_after_requeue
   - Worker 1 claims job
   - Job requeued
   - Worker 2 successfully claims it
```

### Status Consistency Test Results
```
✅ test_requeue_uses_queued_status
   - Requeued job has status='queued'
   - Not 'pending'

✅ test_requeued_job_can_be_claimed
   - Claim → requeue → claim works
   - Requeued jobs are not stuck

✅ test_multiple_requeue_cycles
   - Jobs can be requeued multiple times
   - Each cycle works correctly
```

## Impact Analysis

### Before Fix
❌ **Race condition**: Multiple workers could process the same job
❌ **Silent failures**: Requeued jobs stuck in 'pending' forever
❌ **Resource waste**: Duplicate execution of same jobs
❌ **Data loss**: Jobs that timeout are never retried

### After Fix
✅ **Atomic claiming**: Only one worker can claim a job
✅ **Proper requeue**: Requeued jobs return to the queue
✅ **No duplicates**: Jobs executed exactly once
✅ **Resilient**: Jobs survive timeouts and crashes

## Deployment Notes

### Safety
- Changes are backward compatible (no schema changes)
- Existing 'running' and 'complete' jobs unaffected
- Only 'pending' jobs (if any exist) might need manual intervention

### Post-Deployment Verification
1. Check for any stuck jobs:
   ```sql
   SELECT * FROM thinking_jobs WHERE status='pending';
   ```
   If any found, manually update to 'queued':
   ```sql
   UPDATE thinking_jobs SET status='queued' WHERE status='pending';
   ```

2. Monitor metrics for:
   - Duplicate job execution (should be 0)
   - Orphaned job requeue success rate (should be 100%)
   - Job processing throughput (should be stable or improved)

## References
- Original bug report in code review
- SQLite transaction documentation: https://www.sqlite.org/lang_transaction.html
- Test methodology: Race condition testing with threading
