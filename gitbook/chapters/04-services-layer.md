# Chapter 4: Services Layer Edge Cases

The services layer (`src/mandate_doctor/services/`) is the system's outbound boundary — it talks to Razorpay's HTTP API, manages retries, and surfaces structured errors. This chapter documents every known edge case in `razorpay.py`, the httpx-based Razorpay API client.

Each edge case is tagged with a severity rating:

| Severity | Meaning |
|----------|---------|
| **CRITICAL** | Data loss or corruption in production |
| **HIGH** | Silent misclassification or safety violation |
| **MEDIUM** | Correctness issue that may surface under load |
| **LOW** | Dead code or cosmetic inconsistency |

---

## Table of Contents

- [razorpay.py Edge Cases](#razorpaypy-edge-cases)
  - [EDGE-035: New HTTP client created per request — no connection pooling](#edge-035-new-http-client-created-per-request--no-connection-pooling)
  - [EDGE-036: Exponential backoff caps at ~32.5s — 6 retries take ~2 minutes](#edge-036-exponential-backoff-caps-at-325s--6-retries-take-2-minutes)
  - [EDGE-037: _auth() recomputes on every call — minor overhead](#edge-037-_auth-recomputes-on-every-call--minor-overhead)
  - [EDGE-038: Non-retryable errors raise immediately — no logging of attempt count](#edge-038-non-retryable-errors-raise-immediately--no-logging-of-attempt-count)
  - [EDGE-039: "too many request" substring match — fragile throttle detection](#edge-039-too-many-request-substring-match--fragile-throttle-detection)
  - [EDGE-040: create_payment_link uses hardcoded customer details](#edge-040-create_payment_link-uses-hardcoded-customer-details)
  - [EDGE-041: fetch_order_payments returns empty list on non-200](#edge-041-fetch_order_payments-returns-empty-list-on-non-200)
  - [EDGE-042: RazorpayError.__str__ includes error_code and description](#edge-042-razorpayerror__str__-includes-error_code-and-description)
  - [EDGE-043: No timeout on _with_retries itself — caller controls overall timeout](#edge-043-no-timeout-on-_with_retries-itself--caller-controls-overall-timeout)

---

## razorpay.py Edge Cases

### EDGE-035: New HTTP client created per request — no connection pooling

**Severity:** MEDIUM
**File:** `src/mandate_doctor/services/razorpay.py:98,131,167,187,201`

#### What it is

```python
async with httpx.AsyncClient(timeout=30.0) as client:
```

Every API call — `create_order`, `create_payment_link`, `fetch_order_payments`, `fetch_payment_link`, `fetch_payment` — opens a fresh `httpx.AsyncClient` via an `async with` block. The client is created, used for a single HTTP request, and destroyed.

#### Why it matters

Each new client means a new TCP connection, a new TLS handshake, and new connection state. In a batch of 150 scenarios, each requiring at least 2 API calls (create order + create payment link), that is **300+ new TCP connections**. The TLS handshake alone costs 1–2 round trips per connection. Connection pooling eliminates this overhead entirely — httpx's `AsyncClient` supports HTTP/1.1 keep-alive and connection reuse out of the box.

Under sustained load, the connection churn also increases pressure on the OS socket layer (ephemeral port exhaustion, TIME_WAIT buildup).

#### When it happens

- Every single Razorpay API call, always
- Batch processing amplifies the effect (150 scenarios × 2+ calls = 300+ connection cycles)

#### Mitigation

httpx handles connection cleanup properly — there are no leaked sockets or leaked SSL contexts. The overhead is performance, not correctness.

#### Recommended fix

Create a module-level or class-level `AsyncClient` that persists across requests:

```python
_client: httpx.AsyncClient | None = None

def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client
```

Alternatively, inject the client via dependency injection so tests can mock it cleanly.

---

### EDGE-036: Exponential backoff caps at ~32.5s — 6 retries take ~2 minutes

**Severity:** MEDIUM
**File:** `src/mandate_doctor/services/razorpay.py:65-83`

#### What it is

```python
delay = (2**attempt) + random.uniform(0, 0.5)
await asyncio.sleep(delay)
```

The `_with_retries` loop runs up to `_MAX_ATTEMPTS = 6` iterations. The delay per attempt follows an exponential backoff with jitter:

| Attempt | Base (2^attempt) | Jitter | Total delay |
|---------|-------------------|--------|-------------|
| 0       | 1s                | 0–0.5s | ~1.0–1.5s   |
| 1       | 2s                | 0–0.5s | ~2.0–2.5s   |
| 2       | 4s                | 0–0.5s | ~4.0–4.5s   |
| 3       | 8s                | 0–0.5s | ~8.0–8.5s   |
| 4       | 16s               | 0–0.5s | ~16.0–16.5s |
| 5       | 32s               | 0–0.5s | ~32.0–32.5s |

**Worst-case cumulative delay:** ~64.5 seconds of sleep, plus up to 6 × 30s = 180s of HTTP timeouts. Total worst-case: **~245 seconds (4+ minutes)** per API call.

#### Why it matters

A single throttled payment-link creation can block a worker coroutine for 2+ minutes. In a batch of 150 scenarios, if Razorpay rate-limits after the first 10, the remaining 140 scenarios each wait minutes. The entire batch stalls.

There is no circuit breaker to stop retrying after repeated failures. There is no `max_delay` cap to bound the total wait time.

#### When it happens

- Razorpay returns HTTP 429 (throttled)
- Razorpay returns HTTP 500/502/503/504 (transient server errors)
- Network hiccups during production runs

#### Mitigation

The backoff is reasonable for test mode, where batches are small and speed is less critical. In production with high concurrency, the wait times compound.

#### Recommended fix

Add a `max_delay` cap (e.g., 10s) and optionally a circuit breaker:

```python
delay = min((2**attempt) + random.uniform(0, 0.5), MAX_DELAY)
```

For production, consider a circuit breaker that stops retries after N consecutive failures on the same error code, failing fast instead of waiting minutes.

---

### EDGE-037: _auth() recomputes on every call — minor overhead

**Severity:** LOW
**File:** `src/mandate_doctor/services/razorpay.py:41-52`

#### What it is

```python
def _auth() -> tuple[str, str]:
    key_id = settings.razorpay_key_id
    key_secret = settings.razorpay_key_secret
    ...
    return (key_id, key_secret)
```

Every API call invokes `_auth()`, which reads `settings.razorpay_key_id` and `settings.razorpay_key_secret` from the global `Settings` object. The settings are loaded once at startup and never change.

#### Why it matters

The overhead is negligible — a dict lookup on an immutable object. But the pattern is unnecessary and sets a bad precedent: if `_auth()` were ever called in a hot loop, the repeated attribute access would add up. More importantly, it obscures the fact that credentials are constant.

#### When it happens

- Every API call (so 300+ times per batch)
- Negligible in isolation, but architecturally unnecessary

#### Mitigation

None needed for performance. The Settings object is immutable after initialization, so the values are always the same.

#### Recommended fix

Cache the auth tuple at module level:

```python
_AUTH: tuple[str, str] | None = None

def _auth() -> tuple[str, str]:
    global _AUTH
    if _AUTH is None:
        key_id = settings.razorpay_key_id
        key_secret = settings.razorpay_key_secret
        if not key_id or not key_secret:
            raise RazorpayError(0, "CONFIG_ERROR", "RAZORPAY_KEY_ID or RAZORPAY_KEY_SECRET not set")
        if not key_id.startswith("rzp_test_"):
            raise RazorpayError(0, "CONFIG_ERROR", "Refusing to use non-test-mode keys.")
        _AUTH = (key_id, key_secret)
    return _AUTH
```

This also moves the validation checks to first-call only, avoiding redundant string prefix checks on every invocation.

---

### EDGE-038: Non-retryable errors raise immediately — no logging of attempt count

**Severity:** LOW
**File:** `src/mandate_doctor/services/razorpay.py:71`

#### What it is

```python
if not _is_retryable(exc) or attempt == _MAX_ATTEMPTS - 1:
    raise
```

When a Razorpay API call fails with a non-retryable error (e.g., `invalid_api_key`, malformed request), `_with_retries` raises on the very first attempt. The error propagates to the caller with no indication of how many attempts were made.

#### Why it matters

The caller receives a `RazorpayError` with `status_code`, `error_code`, and `description`, but has no way to know if this was attempt 1 of 6 or attempt 6 of 6. For retryable errors that exhaust all attempts, the caller also cannot distinguish "failed after 6 attempts" from "failed on attempt 1" — both raise the same exception.

This makes debugging harder. Log analysis requires correlating timestamps to figure out whether retries happened.

#### When it happens

- Any non-retryable Razorpay error (invalid key, bad request format)
- Any retryable error that exhausts all 6 attempts

#### Mitigation

The error message includes `error_code` and `description`, which provide enough context to diagnose most issues.

#### Recommended fix

Include the attempt count in `RazorpayError`:

```python
class RazorpayError(Exception):
    def __init__(self, status_code: int, error_code: str, description: str, attempts: int = 1):
        self.attempts = attempts
        super().__init__(f"[{error_code}] {description} (after {attempts} attempt{'s' if attempts > 1 else ''})")
```

Update `_with_retries` to pass `attempt + 1` when raising.

---

### EDGE-039: "too many request" substring match — fragile throttle detection

**Severity:** MEDIUM
**File:** `src/mandate_doctor/services/razorpay.py:56,62`

#### What it is

```python
def _is_throttled(err: RazorpayError) -> bool:
    return "too many request" in err.description.lower()
```

Throttle detection relies on a substring match against the Razorpay error description. If the description changes — say, Razorpay updates their wording to "Rate limit exceeded" or "Too many requests" (note the trailing 's') — the match silently breaks.

The `_is_retryable()` function at line 62 uses the same substring match. It also checks `status_code in _RETRYABLE_STATUS` (which includes 429), so HTTP 429 throttling is still caught even if the description changes.

#### Why it matters

If Razorpay changes their error description wording, `_is_throttled()` returns `False` for throttled requests. `_is_retryable()` still catches 429 via the status code check, but `_is_throttled()` — which is used elsewhere for logging — would misclassify the error.

The description-based check also misses edge cases: Razorpay might return HTTP 400 with a "too many requests" description (as the comment in the source says), which would not be caught by the status code alone.

#### When it happens

- Razorpay updates error message wording
- Razorpay returns throttling as HTTP 400 with a description-based signal
- Current behavior works, but is fragile against upstream changes

#### Mitigation

The `_is_retryable()` function checks both `status_code in _RETRYABLE_STATUS` and the description substring. So retry behavior is robust — at worst, a throttled request gets one fewer retry if the description changes but the status code is 429.

#### Recommended fix

Check both the status code and the description for robustness:

```python
def _is_throttled(err: RazorpayError) -> bool:
    return err.status_code == 429 or "too many request" in err.description.lower()
```

This makes the check redundant with `_is_retryable()` for 429, but keeps the intent explicit and resilient.

---

### EDGE-040: create_payment_link uses hardcoded customer details

**Severity:** LOW
**File:** `src/mandate_doctor/services/razorpay.py:117-119`

#### What it is

```python
customer_name: str = "Test Customer",
customer_email: str = "test@example.com",
customer_contact: str = "+919820123456",
```

The `create_payment_link` function has default parameters with hardcoded test values for customer name, email, and phone number.

#### Why it matters

In test mode, these defaults are fine — they let you create payment links without supplying customer details. In production, using "Test Customer" for real payment links would be a correctness issue: Razorpay payment links would show fake customer information, and customers would receive links addressed to the wrong person.

#### When it happens

- Only in test mode (the client refuses non-test keys — see `_auth()`)
- The defaults are acceptable for the current test-only usage

#### Mitigation

The `_auth()` guard on line 46 prevents this client from being used with production keys. So hardcoded test values cannot leak into production Razorpay accounts.

#### Recommended fix

When the system moves to production, remove the defaults and make customer details required parameters:

```python
async def create_payment_link(
    amount_paise: int,
    reference_id: str,
    customer_name: str,       # no default
    customer_email: str,      # no default
    customer_contact: str,    # no default
    ...
)
```

For now, document that these defaults exist for test mode only.

---

### EDGE-041: fetch_order_payments returns empty list on non-200

**Severity:** LOW
**File:** `src/mandate_doctor/services/razorpay.py:172-176`

#### What it is

```python
if resp.status_code == 200:
    items: list[dict[str, Any]] = resp.json().get("items", [])
    return items
_handle_response(resp)
return []  # pragma: no cover
```

After the `if resp.status_code == 200` block, `_handle_response(resp)` is called. `_handle_response` always raises `RazorpayError` on non-200 status codes (line 228). The `return []` on line 176 is unreachable — `_handle_response` will have already raised before execution reaches it.

The `# pragma: no cover` annotation confirms this is known dead code.

#### Why it matters

Dead code is a maintenance burden. A future maintainer might read the `return []` and assume non-200 responses silently return an empty list, when in reality they raise. This is misleading.

#### When it happens

- Never at runtime — the code path is unreachable
- The `pragma: no cover` annotation excludes it from coverage reports

#### Mitigation

None needed for runtime behavior. The dead code has no impact.

#### Recommended fix

Remove the unreachable `return []` statement:

```python
if resp.status_code == 200:
    items: list[dict[str, Any]] = resp.json().get("items", [])
    return items
_handle_response(resp)  # always raises on non-200
```

This makes the control flow obvious: either return on 200, or raise.

---

### EDGE-042: RazorpayError.__str__ includes error_code and description

**Severity:** LOW
**File:** `src/mandate_doctor/services/razorpay.py:38`

#### What it is

```python
super().__init__(f"[{error_code}] {description}")
```

The `RazorpayError` string representation includes both the `error_code` and the full `description` from Razorpay's API response.

#### Why it matters

Razorpay error descriptions are typically generic (e.g., "The API key provided is invalid"). However, some Razorpay errors include contextual information in the description — transaction IDs, partial account numbers, or request parameters. If such information leaks into error descriptions, it would appear in:

- Structured logs (via `logger.error("razorpay_api_error", ...)`)
- Exception tracebacks
- Any place `str(error)` is called

This is a potential PII leak vector, depending on what Razorpay includes in their error responses.

#### When it happens

- Every time a Razorpay API call fails
- Error descriptions are logged at the `error` level in `_handle_response` (line 227)

#### Mitigation

Razorpay error descriptions are generally generic and do not contain PII. The current logging on line 227 only logs `status` and `code`, not `description`.

#### Recommended fix

For defense in depth, log only the `error_code` and redact the description in the string representation:

```python
class RazorpayError(Exception):
    def __init__(self, status_code: int, error_code: str, description: str):
        self.status_code = status_code
        self.error_code = error_code
        self.description = description
        super().__init__(f"[{error_code}] HTTP {status_code}")
```

Keep `description` as an attribute for programmatic access, but don't embed it in the default string representation that ends up in logs and tracebacks.

---

### EDGE-043: No timeout on _with_retries itself — caller controls overall timeout

**Severity:** LOW
**File:** `src/mandate_doctor/services/razorpay.py:65`

#### What it is

```python
async def _with_retries(fn: Callable[[], Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    last_exc: RazorpayError | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return await fn(*args, **kwargs)
        except RazorpayError as exc:
            if not _is_retryable(exc) or attempt == _MAX_ATTEMPTS - 1:
                raise
            delay = (2**attempt) + random.uniform(0, 0.5)
            ...
            await asyncio.sleep(delay)
    raise last_exc
```

The retry loop has no total timeout. Each individual HTTP request has a 30-second `httpx` timeout, but the loop itself can run all 6 attempts with exponential backoff.

**Worst-case total time per API call:**

| Component         | Time        |
|-------------------|-------------|
| 6 HTTP attempts   | 6 × 30s = 180s |
| Backoff delays    | ~64.5s      |
| **Total**         | **~245s (4+ min)** |

#### Why it matters

The caller (`data_collector`) has its own timeout but does not wrap `_with_retries`. A single API call that retries through all 6 attempts can block a worker for 4+ minutes. In a batch scenario, this means one slow Razorpay response can delay the entire batch significantly.

#### When it happens

- Razorpay is slow or throttled
- Network issues cause timeouts on individual attempts
- All 6 attempts hit the 30-second httpx timeout

#### Mitigation

The httpx timeout of 30 seconds per attempt is the practical bound. Backoff delays add ~64 seconds on top. The caller's timeout (if any) would need to wrap the entire `_with_retries` call.

#### Recommended fix

Add a `total_timeout` parameter to `_with_retries`:

```python
async def _with_retries(
    fn: Callable[[], Awaitable[Any]],
    *args: Any,
    total_timeout: float = 120.0,
    **kwargs: Any,
) -> Any:
    deadline = asyncio.get_event_loop().time() + total_timeout
    last_exc: RazorpayError | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return await fn(*args, **kwargs)
        except RazorpayError as exc:
            if not _is_retryable(exc) or attempt == _MAX_ATTEMPTS - 1:
                raise
            delay = min(
                (2**attempt) + random.uniform(0, 0.5),
                deadline - asyncio.get_event_loop().time(),
            )
            if delay <= 0:
                raise
            await asyncio.sleep(delay)
    raise last_exc
```

This bounds the total time to `total_timeout` seconds, regardless of how many attempts remain.

---

## Summary

| Edge Case | Severity | Status | Risk |
|-----------|----------|--------|------|
| EDGE-035 | MEDIUM | Active | Connection churn under load |
| EDGE-036 | MEDIUM | Active | Slow failover under rate limits |
| EDGE-037 | LOW | Active | Negligible overhead |
| EDGE-038 | LOW | Active | Debugging difficulty |
| EDGE-039 | MEDIUM | Active | Fragile throttle detection |
| EDGE-040 | LOW | Active | Test-mode only |
| EDGE-041 | LOW | Active | Dead code |
| EDGE-042 | LOW | Active | Potential PII in logs |
| EDGE-043 | LOW | Active | No total timeout bound |

The services layer is small (228 lines) but consequential — every API call to Razorpay flows through it. The two MEDIUM-severity issues (EDGE-035 connection pooling, EDGE-036 backoff caps) are the most impactful for production readiness. The LOW-severity issues are cleanup items that improve code quality without changing runtime behavior.
