# Chapter 5: Evaluation Layer Edge Cases

The evaluation layer (`eval/`) is the system's data engine — it generates calibrated test scenarios, executes them through real Razorpay checkout flows, collects the outcomes, and trains the ML model. Every edge case in this chapter lives in three files: `eval/data_collector.py` (two-phase outcome collector), `eval/checkout_bot.py` (Playwright checkout automation), and `eval/train_model.py` (L2 logistic regression trainer).

Each edge case is tagged with a severity rating:

| Severity | Meaning |
|----------|---------|
| **CRITICAL** | Data loss or corruption in production |
| **HIGH** | Silent misclassification or safety violation |
| **MEDIUM** | Correctness issue that may surface under load |
| **LOW** | Dead code or cosmetic inconsistency |

---

## Table of Contents

- [data_collector.py Edge Cases](#data_collectorpy-edge-cases)
  - [EDGE-044: Bank weights loaded once at startup — stale if CSV updated](#edge-044-bank-weights-loaded-once-at-startup--stale-if-csv-updated)
  - [EDGE-045: `pays` variable reused and overwritten — boolean × factor produces 0 or factor](#edge-045-pays-variable-reused-and-overwritten--boolean--factor-produces-0-or-factor)
  - [EDGE-046: Throttle cooldown is fixed 150s — doesn't adapt to rate-limit severity](#edge-046-throttle-cooldown-is-fixed-150s--doesnt-adapt-to-rate-limit-severity)
  - [EDGE-047: Circuit breaker stops entire batch on 3 consecutive throttles](#edge-047-circuit-breaker-stops-entire-batch-on-3-consecutive-throttles)
  - [EDGE-048: SQLite connection shared across concurrent workers — WAL but single writer](#edge-048-sqlite-connection-shared-across-concurrent-workers--wal-but-single-writer)
  - [EDGE-049: INSERT OR REPLACE overwrites existing scenario rows](#edge-049-insert-or-replace-overwrites-existing-scenario-rows)
  - [EDGE-050: checkout_timeout errors are not retried](#edge-050-checkout_timeout-errors-are-not-retried)
  - [EDGE-051: _poll_link returns None on timeout — ambiguous outcome](#edge-051-_poll_link-returns-none-on-timeout--ambiguous-outcome)
  - [EDGE-052: Design version filter excludes v1 rows from training](#edge-052-design-version-filter-excludes-v1-rows-from-training)
- [checkout_bot.py Edge Cases](#checkout_botpy-edge-cases)
  - [EDGE-053: Bank name mapping hardcoded — Razorpay UI changes break it](#edge-053-bank-name-mapping-hardcoded--razorpay-ui-changes-break-it)
  - [EDGE-054: Unknown NPCI banks fallback to "Bank of Baroda" — misleading](#edge-054-unknown-npci-banks-fallback-to-bank-of-baroda--misleading)
  - [EDGE-055: Fixed mobile number "9820123456" for all scenarios](#edge-055-fixed-mobile-number-9820123456-for-all-scenarios)
  - [EDGE-056: Chrome UA hardcoded to version 151 — will become stale](#edge-056-chrome-ua-hardcoded-to-version-151--will-become-stale)
  - [EDGE-057: BANK_PAGE_TIMEOUT used for both bank page load and button click](#edge-057-bank_page_timeout-used-for-both-bank-page-load-and-button-click)
- [train_model.py Edge Cases](#train_modelpy-edge-cases)
  - [EDGE-058: Minimum 20 rows required — blocks training on small datasets](#edge-058-minimum-20-rows-required--blocks-training-on-small-datasets)
  - [EDGE-059: Model saved directly to disk — no atomic write](#edge-059-model-saved-directly-to-disk--no-atomic-write)
  - [EDGE-060: No overfitting detection — CV metrics only](#edge-060-no-overfitting-detection--cv-metrics-only)
  - [EDGE-061: Hyperparameters are hardcoded constants](#edge-061-hyperparameters-are-hardcoded-constants)
  - [EDGE-062: train_incremental() uses file-based last_rows tracking](#edge-062-train_incremental-uses-file-based-last_rows-tracking)
  - [EDGE-063: Feature encoding rebuilt every training run](#edge-063-feature-encoding-rebuilt-every-training-run)

---

## data_collector.py Edge Cases

### EDGE-044: Bank weights loaded once at startup — stale if CSV updated

**Severity:** LOW
**File:** `eval/data_collector.py:116-142`

#### What it is

```python
def load_bank_weights(csv_path: Path | None = None) -> list[BankWeights]:
    path = csv_path or NPCI_CSV
    rows_by_bank: dict[str, dict[str, float]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ...
```

`load_bank_weights()` reads the frozen NPCI CSV once. The returned `banks` list is used for the entire batch. If the CSV file is updated while the server is running, the new data has no effect until the process restarts.

#### Why it matters

This is intentional. The CSV (`data/npci-autopay-execution-2026-07.csv`) is a frozen snapshot of NPCI AutoPay execution data. It is not expected to change at runtime. The "stale" risk only exists if someone manually replaces the CSV without restarting the server.

#### When it happens

- CSV file is replaced while the server is running
- Operator forgets to restart after updating data files

#### Mitigation

Frozen data is by design. The filename includes the month (2026-07) to make staleness obvious.

#### Recommended fix

No fix needed. The frozen snapshot is the intended behavior. If live data becomes necessary in the future, reload on each batch start instead of at server startup.

---

### EDGE-045: `pays` variable reused and overwritten — boolean × factor produces 0 or factor

**Severity:** HIGH
**File:** `eval/data_collector.py:340-345`

#### What it is

```python
pays = _draw(scn["scenario_key"], f"pays|{scn['regime']}") < REGIMES[scn["regime"]]
amount_factor = {19_900: 1.15, 49_900: 1.10, 99_900: 1.0, 149_900: 0.92, 299_900: 0.82}.get(
    scn["amount_paise"], 1.0
)
effective_p = max(0.03, min(0.97, pays * (0.35 + scn["retry_prior"]) * amount_factor))
pays = _draw(scn["scenario_key"], "complete") < effective_p
```

Line 340: `pays` is a boolean — `True` (1) or `False` (0). Line 344: `pays * (0.35 + retry_prior) * amount_factor` multiplies a boolean by floats. When `pays=False` (0), `effective_p = 0` → clamped to `0.03`. When `pays=True` (1), `effective_p = (0.35 + prior) * amount_factor`.

This is the **intended behavior** — a regime gate (does the simulated payer attempt recovery?) modulated by the bank's approval rate and the amount tier. But reusing the same variable name for two different concepts is confusing.

#### Why it matters

The variable reuse obscures the two-stage treatment assignment: (1) regime gate → (2) feature-modulated probability. A reader might think `pays` is the final answer when it is actually an intermediate gate. The code works correctly; the naming does not.

#### When it happens

- Every scenario, always
- Affects readability, not correctness

#### Mitigation

The logic is documented in code comments. The output is correct.

#### Recommended fix

Rename the first `pays` to `regime_gate` for clarity:

```python
regime_gate = _draw(scn["scenario_key"], f"pays|{scn['regime']}") < REGIMES[scn["regime"]]
amount_factor = {19_900: 1.15, 49_900: 1.10, 99_900: 1.0, 149_900: 0.92, 299_900: 0.82}.get(
    scn["amount_paise"], 1.0
)
effective_p = max(0.03, min(0.97, regime_gate * (0.35 + scn["retry_prior"]) * amount_factor))
pays = _draw(scn["scenario_key"], "complete") < effective_p
```

This makes the two-stage assignment explicit and eliminates the ambiguity.

---

### EDGE-046: Throttle cooldown is fixed 150s — doesn't adapt to rate-limit severity

**Severity:** MEDIUM
**File:** `eval/data_collector.py:259-268`

#### What it is

```python
logger.warning("throttled_cooling_down", scenario=scn_key, wait_s=150)
await emit(...)
await asyncio.sleep(150)
result: dict[str, Any] = await _build()
return result
```

When the Razorpay API returns "too many requests", the collector sleeps exactly 150 seconds, then retries **once**. If the rate-limit window is longer than 150 seconds, the retry also fails and the scenario is marked as an error.

#### Why it matters

A fixed cooldown cannot adapt to the actual rate-limit window. Razorpay's throttle window is undocumented and may vary. If the window is 300 seconds, the 150-second cooldown is insufficient, and the retry wastes another API call. The scenario is lost from the training set.

#### When it happens

- Razorpay enforces a rate-limit window longer than 150 seconds
- Burst traffic triggers aggressive throttling
- Multiple scenarios hit the throttle simultaneously

#### Mitigation

Single retry after cooldown; the circuit breaker (EDGE-047) catches 3 consecutive failures and stops the batch before wasting more time.

#### Recommended fix

Read the `Retry-After` header if present, or use exponential cooldown (150s → 300s → 600s):

```python
async def _create_throttled(scn_key: str, phase: str) -> dict[str, Any]:
    cooldowns = [150, 300, 600]
    for attempt, wait in enumerate(cooldowns):
        try:
            return await _build()
        except RazorpayError as exc:
            if "too many request" not in exc.description.lower():
                raise
            if attempt == len(cooldowns) - 1:
                raise
            logger.warning("throttled_cooling_down", scenario=scn_key, wait_s=wait, attempt=attempt)
            await asyncio.sleep(wait)
    raise RuntimeError("unreachable")
```

---

### EDGE-047: Circuit breaker stops entire batch on 3 consecutive throttles

**Severity:** MEDIUM
**File:** `eval/data_collector.py:503-508`

#### What it is

```python
if row.error and "too many request" in row.error.lower():
    consecutive_throttles += 1
else:
    consecutive_throttles = 0
if consecutive_throttles >= 3:
    logger.warning("circuit_breaker_stopping_batch")
    if stop_event is not None:
        stop_event.set()
    await emit({"type": "batch_stopped", "reason": "rate-limited 3x consecutively"})
    return
```

If 3 scenarios in a row hit rate limits, the circuit breaker fires and stops the entire batch. The threshold is hardcoded to 3.

#### Why it matters

A burst of legitimate throttles — Razorpay API instability, a temporary spike in API usage from other sources — can kill a batch prematurely. The circuit breaker treats any 3 consecutive throttles as a systemic failure, when they may be transient. This discards all remaining queued scenarios.

#### When it happens

- Razorpay API instability causes temporary rate-limiting
- Multiple concurrent processes share the same API key
- Burst traffic from other integrations on the same Razorpay account

#### Mitigation

The threshold is hardcoded to 3. The stop event propagates to in-flight workers, which finish their current scenario before stopping.

#### Recommended fix

Add a cooldown-and-retry before stopping, or make the threshold configurable:

```python
if consecutive_throttles >= 3:
    logger.warning("circuit_breaker_cooling_down", wait_s=300)
    await asyncio.sleep(300)  # cool down before giving up
    consecutive_throttles = 0  # reset and try again
```

Alternatively, expose the threshold as a parameter to `run_batch()`.

---

### EDGE-048: SQLite connection shared across concurrent workers — WAL but single writer

**Severity:** HIGH
**File:** `eval/data_collector.py:455,509-539`

#### What it is

```python
conn = init_db(db_path)
# ... later, inside each worker:
conn.execute("INSERT OR REPLACE INTO outcomes ...", (...))
conn.commit()
```

`init_db()` returns a single `sqlite3.Connection`. All workers share this connection. SQLite WAL mode allows concurrent reads but only one writer at a time. Multiple workers calling `conn.execute()` and `conn.commit()` concurrently can cause `"database is locked"` errors.

#### Why it matters

Under asyncio, only one coroutine runs at a time on the event loop. Since `conn.execute()` and `conn.commit()` are synchronous (they block the event loop), there is no true concurrency — but the blocking means the event loop is stalled during writes. If a write takes longer than expected (disk I/O, fsync), other workers are starved.

More critically, if the code ever migrates to `aiosqlite` or a multi-process model, the shared connection will immediately break.

#### When it happens

- All worker scenarios that successfully complete
- Under high worker counts (the default is 3, which is manageable)
- Disk I/O stalls during `conn.commit()`

#### Mitigation

Asyncio workers run on the same event loop (cooperative scheduling), so only one writer executes at a time. This works in practice but is fragile.

#### Recommended fix

Use a connection per worker or serialize writes through an `asyncio.Queue`:

```python
# Option 1: connection per worker
async def worker(ctx_factory: Callable[[], Awaitable[BrowserContext]]) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    # ... use conn locally ...
    conn.close()

# Option 2: serialize writes through a queue
write_queue: asyncio.Queue[ScenarioRow] = asyncio.Queue()

async def writer_task(conn: sqlite3.Connection) -> None:
    while True:
        row = await write_queue.get()
        conn.execute("INSERT OR REPLACE INTO outcomes ...", (...))
        conn.commit()
```

---

### EDGE-049: INSERT OR REPLACE overwrites existing scenario rows

**Severity:** MEDIUM
**File:** `eval/data_collector.py:510`

#### What it is

```python
conn.execute(
    """INSERT OR REPLACE INTO outcomes
       (scenario_key, batch_id, npci_bank, ...)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
    (...),
)
```

If the same `scenario_key` is collected twice (e.g., a batch is restarted), the old row is overwritten. The previous evidence — `failed_payment_id`, `order_id`, `plink_id` — is lost.

#### Why it matters

The `scenario_key` format is `{batch_id}_{index}`, so duplicates across different batches are impossible. However, if the same batch is restarted (e.g., after a crash), scenarios that were already collected will be overwritten with potentially different outcomes. The first attempt's real Razorpay evidence (payment IDs, order IDs) is replaced.

#### When it happens

- Batch restart after a crash
- Manual re-run with the same `batch_id`
- Operator forgets to change the batch ID

#### Mitigation

`scenario_key` includes `batch_id`, so duplicates across batches are impossible. Within a batch, restarts are rare.

#### Recommended fix

Use `INSERT OR IGNORE` to preserve the first attempt:

```python
conn.execute(
    """INSERT OR IGNORE INTO outcomes ...""",
    (...),
)
```

If re-collection is desired, explicitly delete the old rows before starting the batch.

---

### EDGE-050: checkout_timeout errors are not retried

**Severity:** MEDIUM
**File:** `eval/data_collector.py:356-359`

#### What it is

```python
if bot_outcome == "timeout":
    row.error = "checkout_timeout"
    await emit({"type": "step", "node": "bot", "status": "timeout"})
    return row
```

If the checkout bot times out (Playwright cannot complete the flow within the timeout), the row is marked `error="checkout_timeout"` and the scenario is discarded. No retry is attempted.

#### Why it matters

Timeouts account for roughly 24% of v2 rows. Each timeout is a wasted scenario — the Razorpay order and payment link were created but never resolved. The training set loses a labeled data point. With enough data, this is tolerable; with small datasets (e.g., after a design change), it hurts.

#### When it happens

- Razorpay checkout page loads slowly
- Bank page times out waiting for the mock Success/Failure button
- Network latency spikes during the Playwright flow

#### Mitigation

Timeout is rare enough (~24% of rows) that enough data compensates. The 25-second success timeout and 6-second failure timeout are tuned for test mode.

#### Recommended fix

Retry `checkout_timeout` scenarios at the end of the batch:

```python
# After worker loop completes
timeout_scenarios = [s for s in scenarios if s.error == "checkout_timeout"]
for scn in timeout_scenarios:
    retry_row = await collect_one(scn, batch_id, context, auth, sink=sink)
    if retry_row.error is None:
        # save the retried row
```

---

### EDGE-051: _poll_link returns None on timeout — ambiguous outcome

**Severity:** LOW
**File:** `eval/data_collector.py:422-438`

#### What it is

```python
async def _poll_link(link_id: str, auth: tuple[str, str], timeout_s: float = 25.0) -> str | None:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=10.0, auth=auth) as client:
        while time.monotonic() < deadline:
            ...
            if status in ("paid", "expired", "cancelled"):
                return status
            ...
    return None
```

If polling times out, the function returns `None`. Back in `collect_one()`, `row.poll_status = None` and `row.recovered = 0`. A genuinely successful payment that was slow to confirm is counted as "not recovered."

#### Why it matters

The outcome is ambiguous: `None` could mean "payment didn't happen" or "payment happened but confirmation was slow." Both are recorded as `recovered=0`, which biases the training data slightly toward undercounting recoveries.

#### When it happens

- Razorpay's confirmation webhook is slow
- Network latency delays the status poll
- Bank page responds slowly but eventually succeeds

#### Mitigation

25-second timeout for success, 6-second failure timeout. These are generous for test mode. Actual Razorpay test-mode payments confirm in under 5 seconds.

#### Recommended fix

Return `"unknown"` instead of `None`, and count it separately in metrics:

```python
return "unknown"  # instead of return None

# In collect_one:
if status == "unknown":
    row.recovered = 0  # conservative default
    # but log the ambiguity for analysis
```

---

### EDGE-052: Design version filter excludes v1 rows from training

**Severity:** MEDIUM
**File:** `eval/data_collector.py:61`, `eval/train_model.py:39-61`

#### What it is

```python
DESIGN_VERSION = 2  # data_collector.py:61

# train_model.py:39-61
def load_rows(db_path: Path | None = None, min_design_version: int = 2) -> list[dict[str, Any]]:
    ...
    rows = conn.execute(
        """
        SELECT ... FROM outcomes
        WHERE error IS NULL AND assigned_click IS NOT NULL
          AND COALESCE(design_version, 1) >= ?
        """,
        (min_design_version,),
    ).fetchall()
```

`DESIGN_VERSION=2` means only rows with `design_version >= 2` are used for training. v1 rows (which had no learnable feature signal — outcome depended only on the regime draw) are excluded.

#### Why it matters

If all data is v1 (e.g., before the design change was deployed), the model cannot train — `load_rows()` returns an empty list. The `train()` function returns `"insufficient_data"` with a row count, but the dashboard may not surface this clearly.

#### When it happens

- Before the v2 design change is deployed
- After a data migration that resets `design_version` to 1
- Accidental bulk update of the `design_version` column

#### Mitigation

`train()` returns `"insufficient_data"` with the row count. The system does not crash; it just doesn't train.

#### Recommended fix

Allow forcing training on all versions with a flag:

```python
def load_rows(db_path: Path | None = None, min_design_version: int = 2, include_all: bool = False) -> list[dict[str, Any]]:
    if include_all:
        min_design_version = 0
    ...
```

---

## checkout_bot.py Edge Cases

### EDGE-053: Bank name mapping hardcoded — Razorpay UI changes break it

**Severity:** HIGH
**File:** `eval/checkout_bot.py:149-185`

#### What it is

```python
def npcibank_to_rzp_bank(npci_bank: str) -> str:
    mapping = {
        "state bank of india": "Bank of Baroda",
        "bank of baroda": "Bank of Baroda",
        "union bank of india": "Union Bank of India",
        "canara bank": "Canara Bank",
        ...
    }
    key = npci_bank.strip().lower()
    if key in mapping:
        return mapping[key]
    for candidate, label in mapping.items():
        if candidate.split()[0] in key:
            return label
    return "Bank of Baroda"
```

`npcibank_to_rzp_bank()` maps NPCI remitter-bank names to the display labels Razorpay uses in its netbanking checkout. If Razorpay changes their bank list — adds, removes, or renames banks — the mapping breaks silently. The bot would try to click a bank label that no longer exists, causing a `PWTimeout`.

Note that SBI, HDFC, ICICI, Axis, and Kotak — the five largest banks — all map to "Bank of Baroda" because they are not in Razorpay's test-mode bank list. The substitution is recorded per-row via the `rzp_bank` column.

#### Why it matters

Razorpay periodically updates their checkout UI. If they add a new bank, rename an existing one, or remove one from the test-mode list, the bot fails on scenarios that use that bank. The mapping was verified against the live test-mode bank list at the time of writing, but there is no automated verification.

#### When it happens

- Razorpay updates their bank list in test mode
- New NPCI bank names appear in future CSV snapshots
- Razorpay changes display labels (e.g., "Bank of Baroda" → "BoB")

#### Mitigation

Fallback to "Bank of Baroda" for unknown banks. The `rzp_bank` column records what was actually used, so the training data is still accurate.

#### Recommended fix

Periodically verify the mapping against the live Razorpay bank list. Add a smoke test that loads the checkout page and asserts the expected bank labels are present.

---

### EDGE-054: Unknown NPCI banks fallback to "Bank of Baroda" — misleading

**Severity:** MEDIUM
**File:** `eval/checkout_bot.py:185`

#### What it is

```python
return "Bank of Baroda"
```

Any NPCI bank name not in the mapping defaults to "Bank of Baroda". The dataset records the NPCI bank name (e.g., "Karur Vysya Bank") but the checkout actually used Bank of Baroda.

#### Why it matters

A reader of the training data might think "Karur Vysya Bank" was tested, when in fact Bank of Baroda was used. The `rzp_bank` column records what was actually used, but the discrepancy between `npci_bank` and `rzp_bank` is non-obvious without checking both columns.

#### When it happens

- Any bank not in the hardcoded mapping
- Future NPCI CSV snapshots with new bank names

#### Mitigation

The `rzp_bank` column records the actual bank used. The mapping is documented in the function's docstring.

#### Recommended fix

Log a warning when fallback occurs:

```python
if key not in mapping:
    logger.warning("bank_fallback", npci_bank=npci_bank, fallback="Bank of Baroda")
```

---

### EDGE-055: Fixed mobile number "9820123456" for all scenarios

**Severity:** LOW
**File:** `eval/checkout_bot.py:298,352`

#### What it is

```python
await pay_payment_link(
    context=context,
    short_url=debit_link["short_url"],
    mobile="9820123456",
    ...
)
```

All scenarios use the same test mobile number `9820123456`. Every checkout session enters this number into the contact field.

#### Why it matters

Razorpay might flag repeated payments from the same number as suspicious behavior, even in test mode. If Razorpay adds rate limiting per mobile number in test mode, batches would fail.

#### When it happens

- Large batches with many scenarios (150+)
- Razorpay adds per-number throttling in test mode

#### Mitigation

Test mode does not enforce unique numbers. This is test-mode-only behavior.

#### Recommended fix

Generate sequential test numbers to spread the load:

```python
mobile = f"98201234{56 + (i % 100):03d}"  # 9820123456, 9820123457, ...
```

---

### EDGE-056: Chrome UA hardcoded to version 151 — will become stale

**Severity:** LOW
**File:** `eval/checkout_bot.py:29-32`

#### What it is

```python
CHECKOUT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
```

The user agent string specifies Chrome version 151. Over time, this version becomes increasingly old. If Razorpay checks the UA version and blocks old browsers, the checkout fails.

#### Why it matters

Razorpay's hosted checkout may eventually require a minimum browser version for security compliance. An old UA version would cause silent failures — the page loads but the checkout flow behaves differently or blocks the session.

#### When it happens

- Razorpay updates their browser compatibility requirements
- Chrome 151 becomes years old

#### Mitigation

Razorpay does not block based on UA version in test mode. The UA is primarily needed to trigger the mobile checkout layout.

#### Recommended fix

Update the UA string periodically, or use Playwright's built-in UA rotation:

```python
# Let Playwright handle UA based on the browser channel
context = await browser.new_context(
    viewport={"width": 414, "height": 896},
    # omit user_agent to use Playwright's default
)
```

---

### EDGE-057: BANK_PAGE_TIMEOUT used for both bank page load and button click

**Severity:** LOW
**File:** `eval/checkout_bot.py:24,119,135`

#### What it is

```python
BANK_PAGE_TIMEOUT = 30_000
# used at line 119:
async with page.context.expect_page(timeout=BANK_PAGE_TIMEOUT) as new_page_info:
    ...
# used at line 135:
await btn.click(timeout=BANK_PAGE_TIMEOUT)
```

`BANK_PAGE_TIMEOUT = 30_000ms` is used for both `expect_page()` (waiting for the bank page to open) and `btn.click()` (waiting for the Success/Failure button). These have different expected latencies — the bank page opening is slow, but once loaded, the button should be immediately available.

#### Why it matters

If the bank page loads slowly, the 30-second timeout is generous for `expect_page()`. But once the page is loaded, the button click should be near-instant. Using the same 30-second timeout for the click means a stuck button (e.g., the mock bank page failed to render) waits unnecessarily long before failing.

#### When it happens

- Bank page renders slowly (30s is generous for the click)
- Mock bank page fails to render the button (30s wait before timeout)

#### Mitigation

The timeout is per-operation, not cumulative. The total bank-page phase is at most 60 seconds (30s for page open + 30s for click), which is acceptable.

#### Recommended fix

Use a shorter timeout for the button click since the page is already loaded:

```python
await btn.click(timeout=10_000)  # page is already loaded
```

---

## train_model.py Edge Cases

### EDGE-058: Minimum 20 rows required — blocks training on small datasets

**Severity:** MEDIUM
**File:** `eval/train_model.py:198-200`

#### What it is

```python
if len(rows) < 20:
    logger.warning("insufficient_data", rows=len(rows), minimum=20)
    return {"status": "insufficient_data", "rows": len(rows)}
```

`train()` returns `"insufficient_data"` if fewer than 20 rows are available. After the v2 design change, only 62 clean rows exist. Training works but is fragile — the model may not generalize well with so few data points.

#### Why it matters

The minimum of 20 ensures basic statistical validity (enough samples for k-fold cross-validation). But 20 is low for a production model. With 62 rows and 5-fold CV, each fold has ~12 test samples — too few for stable metrics.

#### When it happens

- After a design change that excludes old data
- First run with no historical data
- Data cleanup removes most rows (e.g., high error rate)

#### Mitigation

Minimum ensures statistical validity. The model is not used in production (this is test-mode-only), so fragility is acceptable.

#### Recommended fix

Allow configurable minimum; warn if < 50 for production use:

```python
RECOMMENDED_MIN = 50

if len(rows) < 20:
    return {"status": "insufficient_data", "rows": len(rows)}
if len(rows) < RECOMMENDED_MIN:
    logger.warning("low_data_warning", rows=len(rows), recommended=RECOMMENDED_MIN)
```

---

### EDGE-059: Model saved directly to disk — no atomic write

**Severity:** MEDIUM
**File:** `eval/train_model.py:236-237`

#### What it is

```python
path = out / "recovery_model.json"
path.write_text(json.dumps(artifact, indent=2))
```

The model is written directly to `recovery_model.json`. `path.write_text()` is not atomic — if the process crashes mid-write, the file is corrupted (partial JSON).

#### Why it matters

If the process is killed during the write (OOM, crash, power loss), the model file contains truncated JSON. The policy engine tries to `json.loads()` this file and gets a `JSONDecodeError`. The system falls back to no model, which is safe but degrades recovery quality.

#### When it happens

- Process crash during training
- OOM kill while serializing large model artifacts
- Disk full during write

#### Mitigation

JSON serialization is fast for small models (< 100KB). The corruption window is milliseconds. The policy engine should handle `JSONDecodeError` gracefully.

#### Recommended fix

Write to a temp file, then atomic rename:

```python
import tempfile

def _atomic_write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)  # atomic on POSIX
    except BaseException:
        os.unlink(tmp)
        raise
```

---

### EDGE-060: No overfitting detection — CV metrics only

**Severity:** LOW
**File:** `eval/train_model.py:206`

#### What it is

```python
metrics = kfold_metrics(X, y)
```

`kfold_metrics()` reports CV accuracy, precision, recall, log-loss, and AUC. It does not compare CV metrics against in-sample (training) metrics. If `in_sample_accuracy >> cv_accuracy`, the model is overfitting — but no alert is raised.

#### Why it matters

Overfitting is the primary failure mode for small datasets. With 62 rows and many one-hot features (banks, error classes, regimes, amounts), the model can memorize the training set. The CV metrics would then be poor while in-sample metrics look great, but nothing flags this discrepancy.

#### When it happens

- Small datasets with many features
- High cardinality in one-hot columns (many unique banks)
- After a design change that reduces available data

#### Mitigation

In-sample accuracy is logged alongside CV accuracy for manual comparison. The metrics are saved in the model artifact, so they can be inspected.

#### Recommended fix

Add an overfitting alert when the gap exceeds a threshold:

```python
insample_acc = float(((p >= 0.5).astype(float) == y).mean())
cv_acc = metrics["cv_accuracy"]
if insample_acc - cv_acc > 0.15:
    logger.warning("possible_overfitting", in_sample=insample_acc, cv=cv_acc, gap=insample_acc - cv_acc)
```

---

### EDGE-061: Hyperparameters are hardcoded constants

**Severity:** LOW
**File:** `eval/train_model.py:33-36`

#### What it is

```python
RANDOM_SEED = 42
L2_LAMBDA = 1.0
LR = 0.1
EPOCHS = 3000
```

These module-level constants control the logistic regression training. There is no hyperparameter tuning or search. The values are reasonable defaults but may be suboptimal for new data distributions.

#### Why it matters

For a fixed dataset, hardcoded hyperparameters work fine. But as the data distribution shifts (new banks, different error patterns), the optimal `L2_LAMBDA`, `LR`, and `EPOCHS` may change. Without tuning, the model may underfit or overfit.

#### When it happens

- Data distribution shifts significantly
- New bank or error-class features are added
- Dataset grows to the point where different regularization is needed

#### Mitigation

Reasonable defaults for small datasets. The model is not used in production, so suboptimal hyperparameters have low impact.

#### Recommended fix

Add grid search or use optuna for tuning:

```python
# Simple grid search
best_acc = 0.0
best_params = {}
for l2 in [0.01, 0.1, 1.0, 10.0]:
    for lr in [0.01, 0.05, 0.1]:
        w = fit_logistic(X_train, y_train, l2=l2, lr=lr)
        acc = evaluate(w, X_val, y_val)
        if acc > best_acc:
            best_acc = acc
            best_params = {"l2": l2, "lr": lr}
```

---

### EDGE-062: train_incremental() uses file-based last_rows tracking

**Severity:** LOW
**File:** `eval/train_model.py:254-267`

#### What it is

```python
def train_incremental(min_new_rows: int = 5) -> dict[str, Any]:
    log_path = MODELS_DIR / "training_log.jsonl"
    rows = load_rows()
    last_rows = 0
    if log_path.exists():
        lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
        if lines:
            last_rows = int(json.loads(lines[-1]).get("rows", 0))
    if log_path.exists() and len(rows) - last_rows < min_new_rows:
        return {"status": "skipped", ...}
```

`train_incremental()` reads `training_log.jsonl` to find the last trained row count. If the log is deleted or corrupted, `last_rows` defaults to 0, and incremental training always triggers (safe default).

#### Why it matters

The file-based tracking is fragile. If the log is accidentally deleted, every batch triggers a full retrain — wasteful but not incorrect. If the log is corrupted (e.g., partial write), `json.loads()` fails and the fallback is the same as deletion.

#### When it happens

- Log file deleted accidentally
- Log file corrupted by partial write
- Log file moved or renamed

#### Mitigation

Fallback to full retrain is safe. The worst case is unnecessary computation.

#### Recommended fix

Store `last_trained_rows` in the model artifact itself:

```python
artifact["last_trained_rows"] = len(rows)
```

Then read it from the model file instead of the log, eliminating the separate tracking file.

---

### EDGE-063: Feature encoding rebuilt every training run

**Severity:** LOW
**File:** `eval/train_model.py:66-112`

#### What it is

```python
def build_design(rows: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray, list[str], dict[str, list[Any]]]:
    banks = sorted({r["npci_bank"] for r in rows})
    classes = sorted({r["error_class"] for r in rows})
    ...
```

`build_design()` sorts unique values and builds one-hot columns from scratch every training run. If new banks or amounts appear in new data, the feature dimension changes. The saved model's weights have a different shape than the new encoding.

#### Why it matters

If the training data gains a new bank (e.g., a previously unseen NPCI bank), `build_design()` creates a new one-hot column. The saved model's weights don't include this column. When `score_row()` evaluates a row with the new bank, it uses `wmap.get(key, 0.0)` — which returns 0.0 for the unknown feature. This is safe (zero weight = no contribution) but the model cannot learn the new bank's effect until it is retrained with enough data.

#### When it happens

- New banks appear in the NPCI CSV
- New error codes appear from Razorpay
- New amount tiers are added to the experiment design

#### Mitigation

`score_row()` uses the saved encoding, so it ignores unseen features gracefully (returns 0.0). The model degrades rather than breaks.

#### Recommended fix

Save the encoding with the model and validate new data against it:

```python
# In train():
artifact["encoding"] = encoding  # already done

# In load_rows() or build_design():
saved_encoding = load_model()["encoding"]
new_banks = set(r["npci_bank"] for r in rows) - set(saved_encoding["banks"])
if new_banks:
    logger.warning("new_banks_not_in_model", banks=list(new_banks))
```

---

## Summary

| Edge Case | Severity | File | Core Issue |
|-----------|----------|------|------------|
| EDGE-044 | LOW | data_collector.py:116-142 | Bank weights loaded once (frozen by design) |
| EDGE-045 | HIGH | data_collector.py:340-345 | Variable reuse obscures two-stage treatment assignment |
| EDGE-046 | MEDIUM | data_collector.py:259-268 | Fixed 150s cooldown doesn't adapt to rate-limit window |
| EDGE-047 | MEDIUM | data_collector.py:503-508 | Circuit breaker kills batch on 3 consecutive throttles |
| EDGE-048 | HIGH | data_collector.py:455,509-539 | Shared SQLite connection across asyncio workers |
| EDGE-049 | MEDIUM | data_collector.py:510 | INSERT OR REPLACE overwrites first-attempt evidence |
| EDGE-050 | MEDIUM | data_collector.py:356-359 | No retry for checkout_timeout scenarios |
| EDGE-051 | LOW | data_collector.py:422-438 | `_poll_link` returns None — ambiguous outcome |
| EDGE-052 | MEDIUM | data_collector.py:61, train_model.py:39-61 | v2 filter blocks training if all data is v1 |
| EDGE-053 | HIGH | checkout_bot.py:149-185 | Hardcoded bank mapping breaks on UI changes |
| EDGE-054 | MEDIUM | checkout_bot.py:185 | Unknown banks silently fall back to Bank of Baroda |
| EDGE-055 | LOW | checkout_bot.py:298,352 | Fixed mobile number for all scenarios |
| EDGE-056 | LOW | checkout_bot.py:29-32 | Chrome 151 UA will become stale |
| EDGE-057 | LOW | checkout_bot.py:24,119,135 | Same timeout for page load and button click |
| EDGE-058 | MEDIUM | train_model.py:198-200 | 20-row minimum blocks small-dataset training |
| EDGE-059 | MEDIUM | train_model.py:236-237 | Non-atomic model write — corruption on crash |
| EDGE-060 | LOW | train_model.py:206 | No overfitting detection in CV metrics |
| EDGE-061 | LOW | train_model.py:33-36 | Hardcoded hyperparameters — no tuning |
| EDGE-062 | LOW | train_model.py:254-267 | File-based incremental tracking is fragile |
| EDGE-063 | LOW | train_model.py:66-112 | Feature encoding rebuilt every run — dimension drift |

**HIGH:** 2 — both involve data correctness (treatment assignment clarity, bank mapping accuracy).
**MEDIUM:** 8 — affect batch reliability, data quality, or operational resilience.
**LOW:** 10 — minor issues with existing mitigations or low impact.

### Next Steps

- [Chapter 6: Policy Engine Edge Cases](./06-policy-engine.md) — covers retry budget exhaustion, fail-closed gate, and decision logging.
- [Chapter 7: Idempotency & Recovery Edge Cases](./07-idempotency-recovery.md) — covers SQLite contention, race conditions, and exactly-once guarantees.
