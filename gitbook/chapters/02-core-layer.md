# Chapter 2: Core Layer Edge Cases

The core layer (`src/mandate_doctor/core/`) is where classification, policy enforcement, and idempotency logic live. This chapter documents every known edge case across five modules: `models.py`, `classifier.py`, `codes.py`, `policy.py`, and `idempotency.py`.

Each edge case is tagged with a severity rating:

| Severity | Meaning |
|----------|---------|
| **CRITICAL** | Data loss or corruption in production |
| **HIGH** | Silent misclassification or safety violation |
| **MEDIUM** | Correctness issue that may surface under load |
| **LOW** | Dead code or cosmetic inconsistency |

---

## models.py

### EDGE-001 — `datetime.now` uses local time, not UTC (MEDIUM)

**Location:** `src/mandate_doctor/core/models.py:50,77,96,106`

`Field(default_factory=datetime.now)` defaults to system-local time. Every timestamp in `Mandate`, `DebitAttempt`, `Decision`, and `AuditEntry` inherits the server's timezone.

**Problem:** Razorpay webhook timestamps are UTC. If the server runs in `Asia/Kolkata` (UTC+5:30), all internal timestamps are 5.5 hours ahead of webhook data. Any time-based comparison — `sweep_stale`, retry windows, audit ordering — produces incorrect results.

**Mitigation:** None currently.

**Fix:** Replace all occurrences with:

```python
from datetime import UTC, datetime

Field(default_factory=lambda: datetime.now(UTC))
```

Or use a shared helper:

```python
def utc_now() -> datetime:
    return datetime.now(UTC)
```

Then reference `utc_now` as the default factory everywhere.

---

### EDGE-002 — `cycle_id` defaults to `"cycle_default"` — cross-cycle retry leak (HIGH)

**Location:** `src/mandate_doctor/core/models.py:76`

If different billing cycles don't set unique `cycle_id` values, all retries share one budget. A mandate with 3 failures across 3 months would exhaust the budget on the first failure.

**Mitigation:** The default exists for test mode simplicity. Production payloads should always supply a unique cycle identifier.

**Fix:** In production, require `cycle_id` from the webhook payload (e.g., `subscription.current_cycle`). In dev/test, add a warning log when the default is used:

```python
if cycle_id == "cycle_default":
    logger.warning("Using default cycle_id — retry budget is shared across all cycles")
```

---

### EDGE-003 — `confidence` field is unused downstream (LOW)

**Location:** `src/mandate_doctor/core/models.py:90`

`Decision.confidence` is set by the classifier but `policy.py` never reads it for any decision. A high-confidence STOP could be overridden by budget logic without consulting confidence.

**Mitigation:** Budget override is intentional (fail-safe). Confidence is dead data.

**Note:** This is a design choice, not a bug. The system is correct to prefer budget safety over confidence. But the field adds complexity without value. Either use it or remove it.

---

### EDGE-004 — `is_synthetic` flag is never set to `True` (LOW)

**Location:** `src/mandate_doctor/core/models.py:81`

`DebitAttempt.is_synthetic` defaults to `False` and no code path sets it to `True`. In test mode, ALL attempts are synthetic. This flag is dead.

**Mitigation:** None. The field is never read or written to.

**Fix:** Either populate it from the test harness when building synthetic attempts, or remove the field entirely.

---

## classifier.py

### EDGE-005 — No error detail → AMBIGUOUS with hardcoded 0.5 confidence (HIGH)

**Location:** `src/mandate_doctor/core/classifier.py:50-56`

If Razorpay sends a `payment.failed` webhook with no `error` object, the classifier returns `AMBIGUOUS` at `0.5` confidence. This means any webhook parsing bug (missing error field) silently becomes "hold for review" instead of being flagged.

**Mitigation:** Logged as warning, but no alert or metric is emitted.

**Fix:** Emit a metric or structured log for "missing error detail" to detect parsing bugs early:

```python
if error_detail is None:
    logger.warning("payment.failed webhook missing error detail", extra={
        "event_id": event_id,
        "metric": "classifier.missing_error_detail",
    })
    # TODO: emit Prometheus counter or alert
```

---

### EDGE-006 — Pattern matching uses substring — false positives on partial matches (MEDIUM)

**Location:** `src/mandate_doctor/core/classifier.py:116-131`

```python
any(kw in desc for kw in balance_keywords)
```

matches `"insufficient"` anywhere in the string. A description like `"Transaction rejected due to insufficient merchant configuration"` would false-positive to `LOW_BALANCE`.

**Mitigation:** Keywords are reasonably specific but not anchored.

**Fix:** Use word-boundary regex or require exact phrase matches:

```python
import re

def _matches_keywords(description: str, keywords: list[str]) -> bool:
    for kw in keywords:
        if re.search(rf'\b{re.escape(kw)}\b', description, re.IGNORECASE):
            return True
    return False
```

---

### EDGE-007 — LLM layer (Step 3) is documented but never implemented (HIGH)

**Location:** `src/mandate_doctor/core/classifier.py:92-104`

The docstring says:

> 3. LLM classification for truly unknown errors (AI at the edge)

But Step 3 just returns `AMBIGUOUS`. Unknown error codes ALWAYS become `AMBIGUOUS`. The system never learns from them.

**Mitigation:** `AMBIGUOUS` → `HOLD_FOR_REVIEW` is safe (fail-closed).

**Fix:** Either implement the LLM call for unknown codes, or update the docstring to reflect the current behavior:

```python
# Step 3: Unknown error codes
# In v1, unknown codes are routed to human review (HOLD_FOR_REVIEW).
# LLM classification is planned for a future release.
```

---

### EDGE-008 — `SCORER_WEIGHTS` and thresholds are defined but never used (MEDIUM)

**Location:** `src/mandate_doctor/core/classifier.py:31-41`

`SCORER_WEIGHTS` dict and `THRESHOLD_HIGH`/`THRESHOLD_LOW` constants exist but no code references them. This was intended for a confidence scoring pipeline that was never built.

**Mitigation:** Dead code — no runtime impact.

**Fix:** Remove or archive. Dead constants confuse readers into thinking they matter.

---

## codes.py

### EDGE-009 — 62 error codes mapped — new Razorpay/NPCI codes are unhandled (HIGH)

**Location:** `src/mandate_doctor/core/codes.py:13-62`

Razorpay and NPCI regularly introduce new error codes. Any code not in `CODE_TO_BUCKET` falls through to `AMBIGUOUS`. Recent additions like `"BAD_REQUEST_ERROR"` (the most common throttle error) map to `AMBIGUOUS`, not `TECHNICAL`.

**Mitigation:** Unknown codes → `AMBIGUOUS` → `HOLD_FOR_REVIEW` (safe).

**Fix:** Monitor `AMBIGUOUS` rate in production. Periodically add new codes. Consider a fallback bucket for unrecognized technical codes:

```python
def classify(error_code: str) -> FailureBucket:
    bucket = CODE_TO_BUCKET.get(error_code.lower().strip())
    if bucket is not None:
        return bucket

    # Fallback: if the code contains "technical", "timeout", "gateway"
    # it's likely a transient error
    if any(kw in error_code.lower() for kw in ["technical", "timeout", "gateway", "server"]):
        return FailureBucket.TECHNICAL

    return FailureBucket.AMBIGUOUS
```

---

### EDGE-010 — `"payment_declined"` maps to AMBIGUOUS — most common error is unclassified (HIGH)

**Location:** `src/mandate_doctor/core/codes.py:60`

`"payment_declined"` is Razorpay's generic catch-all. It maps to `AMBIGUOUS` (0.85 confidence). In practice, most webhooks carry this code. The system defaults to `HOLD_FOR_REVIEW` for the majority of failures.

**Mitigation:** Safe (fail-closed), but limits automation rate.

**Fix:** For `payment_declined`, fall through to pattern matching on the description text. This is already the intended flow in `classify.py`, but it requires the caller to pass the description. Verify the call chain:

```
webhook payload
  → classify_payment_failure(error_code, description)
    → codes.classify(error_code)  # returns AMBIGUOUS for payment_declined
    → if AMBIGUOUS, try pattern matching on description
```

If the description is also missing or generic, `AMBIGUOUS` is the correct fallback.

---

### EDGE-011 — Confidence scores are fixed per bucket — no context sensitivity (MEDIUM)

**Location:** `src/mandate_doctor/core/codes.py:65-70`

`LOW_BALANCE` always gets 0.95, `TECHNICAL` always 0.90, `STOP` always 0.98. A `"bank_technical_error"` on a known-unreliable bank should have lower confidence than on a reliable bank.

**Mitigation:** Acceptable for v1; no bank reliability data available.

**Note:** This is a known limitation. Bank reliability scoring would require historical success-rate data per bank, which is a separate feature. Document it as "future enhancement."

---

### EDGE-012 — Case-insensitive matching with strip — but no normalization of separators (LOW)

**Location:** `src/mandate_doctor/core/codes.py:78`

```python
error_code.lower().strip()
```

handles case and whitespace. Codes with hyphens, underscores, or spaces (e.g., `"insufficient-funds"` vs `"insufficient_funds"`) won't match.

**Mitigation:** Razorpay consistently uses underscores. This is a theoretical issue.

**Note:** Razorpay's API documentation confirms underscore-only naming. If this changes, the fallback in EDGE-009 would catch it.

---

## policy.py

### EDGE-013 — `RetryBudget` is in-memory — lost on server restart (CRITICAL)

**Location:** `src/mandate_doctor/core/policy.py:37-63,66-73`

The global `retry_budget` dict resets when the process dies. After a restart, all mandates get a fresh 3-retry budget, enabling retry-budget violations.

**Mitigation:** None. In test mode, budgets reset anyway between batches.

**Fix:** Persist budget to SQLite. Key by `(mandate_id, cycle_id)` with `retry_count`:

```sql
CREATE TABLE retry_budgets (
    mandate_id TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (mandate_id, cycle_id)
);
```

Or use a lightweight SQLite-backed cache with TTL per cycle.

---

### EDGE-014 — Global singleton — no per-request isolation (MEDIUM)

**Location:** `src/mandate_doctor/core/policy.py:67`

```python
retry_budget = RetryBudget()
```

is module-level. All concurrent requests share it. A race between two webhook handlers for the same mandate could double-consume a retry slot.

**Mitigation:** SQLite idempotency layer catches duplicate executions, but not double budget consumption.

**Fix:** Lock around `consume_retry`, or use a database-backed budget:

```python
import threading

class RetryBudget:
    def __init__(self):
        self._lock = threading.Lock()
        self._budgets: dict[tuple[str, str], int] = {}

    def consume_retry(self, mandate_id: str, cycle_id: str) -> bool:
        with self._lock:
            key = (mandate_id, cycle_id)
            count = self._budgets.get(key, 0)
            if count >= self.max_retries:
                return False
            self._budgets[key] = count + 1
            return True
```

---

### EDGE-015 — No automatic cycle reset (MEDIUM)

**Location:** `src/mandate_doctor/core/policy.py:61-63`

`reset_cycle()` exists but is never called automatically. Old budgets persist until manual reset or server restart. In production, a new billing cycle should auto-reset the budget.

**Mitigation:** Manual reset in tests. Production would need cycle detection from webhook events.

**Fix:** Detect cycle transitions from incoming webhooks. When a new `cycle_id` is observed, reset the budget for that mandate:

```python
def on_webhook_received(mandate_id: str, cycle_id: str, previous_cycle_id: str | None):
    if previous_cycle_id and previous_cycle_id != cycle_id:
        retry_budget.reset_cycle(mandate_id, previous_cycle_id)
```

---

### EDGE-016 — Bucket-to-action mapping has no fallback (LOW)

**Location:** `src/mandate_doctor/core/policy.py:148-158`

The `match` statement covers all 4 `FailureBucket` values exhaustively. If a new bucket is added to the enum, Python raises `MatchError` at runtime.

**Mitigation:** Type system catches this at dev time (mypy strict).

**Note:** This is a feature, not a bug. Exhaustive matching forces you to handle every bucket. The `MatchError` is a safety net — it prevents silently ignoring new buckets.

---

## idempotency.py

### EDGE-017 — `check_same_thread=False` — unsafe for multi-threaded access (MEDIUM)

**Location:** `src/mandate_doctor/core/idempotency.py:45`

SQLite connection created with `check_same_thread=False`. FastAPI runs async handlers on a single thread, but `asyncio.to_thread()` dispatches to a thread pool.

**Mitigation:** Thread-local connections (`_local`) isolate per-thread. But WAL mode allows concurrent reads.

**Fix:** Use a connection pool or ensure all DB access is serialized. For a single-server deployment, thread-local + WAL is acceptable. For horizontal scaling, move to a proper database.

---

### EDGE-018 — `sweep_stale` uses wall clock — NTP jumps cause false escalations (LOW)

**Location:** `src/mandate_doctor/core/idempotency.py:148-163`

`datetime.now(UTC)` is wall clock. If NTP adjusts time backward, a recently claimed record could appear stale.

**Mitigation:** 10-minute default threshold is large enough for typical NTP drift.

**Fix:** Use monotonic time for age calculation:

```python
import time

_claimed_at_mono: dict[str, float] = {}

def claim(idempotency_key: str) -> bool:
    _claimed_at_mono[idempotency_key] = time.monotonic()
    # ...

def sweep_stale(threshold_seconds: int = 600) -> list[str]:
    now = time.monotonic()
    stale = [
        key for key, claimed_at in _claimed_at_mono.items()
        if now - claimed_at > threshold_seconds
    ]
    # ...
```

---

### EDGE-019 — No cleanup of old records (MEDIUM)

**Location:** `src/mandate_doctor/core/idempotency.py`

`recovery_claims` and `executions` tables grow indefinitely. No TTL or vacuum. In a long-running production system, this causes disk growth and slower queries.

**Mitigation:** Acceptable for test mode (bounded scenarios).

**Fix:** Add TTL-based cleanup or periodic vacuum:

```python
def cleanup_old_records(max_age_days: int = 30):
    cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
    conn.execute("DELETE FROM executions WHERE executed_at < ?", (cutoff,))
    conn.execute("DELETE FROM recovery_claims WHERE claimed_at < ?", (cutoff,))
```

Call this on a daily schedule or during off-peak hours.

---

### EDGE-020 — `stats()` counts claims vs executions — deduped = claims - executed (LOW)

**Location:** `src/mandate_doctor/core/idempotency.py:165-173`

Deduplication count is `claims - executed`. But claims include `PENDING`, `DECIDED`, and `ESCALATE_STALE` states. A crashed claim (`ESCALATE_STALE`) is counted as "deduplicated" even though it wasn't — it failed.

**Mitigation:** Stats are informational only.

**Fix:** Count only `DECIDED` (non-`ESCALATE`) claims as deduplicated:

```python
def stats() -> dict:
    claims = conn.execute("SELECT COUNT(*) FROM recovery_claims").fetchone()[0]
    executed = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
    escalated = conn.execute(
        "SELECT COUNT(*) FROM recovery_claims WHERE state = 'ESCALATE_STALE'"
    ).fetchone()[0]

    successful_dedup = claims - escalated
    return {
        "total_claims": claims,
        "total_executions": executed,
        "deduplicated": successful_dedup,
        "escalated_stale": escalated,
    }
```

---

## Summary

| Module | Critical | High | Medium | Low | Total |
|--------|----------|------|--------|-----|-------|
| models.py | 0 | 1 | 1 | 2 | 4 |
| classifier.py | 0 | 3 | 2 | 0 | 5 |
| codes.py | 0 | 3 | 2 | 1 | 6 |
| policy.py | 1 | 0 | 3 | 1 | 5 |
| idempotency.py | 0 | 0 | 3 | 2 | 5 |
| **Total** | **1** | **7** | **11** | **6** | **25** |

**Priority actions:**

1. **EDGE-013 (CRITICAL):** Persist retry budget to survive restarts.
2. **EDGE-002, 005, 007, 009, 010 (HIGH):** Address classification gaps and budget leaks.
3. **EDGE-001, 006, 011, 014, 015, 017, 019 (MEDIUM):** Fix concurrency, time, and cleanup issues before scaling.

---

*Next: [Chapter 3: Policy Engine Deep Dive →](03-policy-engine.md)*
