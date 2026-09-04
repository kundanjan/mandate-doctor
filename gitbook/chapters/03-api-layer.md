# Chapter 3: API Layer Edge Cases

The API layer is the system's front door — it receives Razorpay webhooks, orchestrates collection batches, trains the ML model, and pushes live events to the dashboard. Every edge case in this chapter lives in two files: `src/mandate_doctor/api/app.py` (the FastAPI application) and `src/mandate_doctor/api/events.py` (the WebSocket event bus).

This chapter assumes you have read [Chapter 1: System Overview](./01-system-overview.md) and [Chapter 2: Data Pipeline](./02-data-pipeline.md).

---

## Table of Contents

- [app.py Edge Cases](#apppy-edge-cases)
  - [EDGE-021: Bounce evidence dict is in-memory — lost on restart](#edge-021-bounce-evidence-dict-is-in-memory--lost-on-restart)
  - [EDGE-022: received_events list grows unbounded](#edge-022-received_events-list-grows-unbounded)
  - [EDGE-023: HMAC verification doesn't handle malformed signature header](#edge-023-hmac-verification-doesnt-handle-malformed-signature-header)
  - [EDGE-024: CORS whitelist is hardcoded](#edge-024-cors-whitelist-is-hardcoded)
  - [EDGE-025: Only one batch can run at a time](#edge-025-only-one-batch-can-run-at-a-time)
  - [EDGE-026: _batch_stop Event not cleared between batches](#edge-026-_batch_stop-event-not-cleared-between-batches)
  - [EDGE-027: Periodic trainer starts after 90s warm-up](#edge-027-periodic-trainer-starts-after-90s-warm-up)
  - [EDGE-028: WebSocket clients don't get backpressure signal](#edge-028-websocket-clients-dont-get-backpressure-signal)
  - [EDGE-029: /api/stats opens SQLite connection per request](#edge-029-apistats-opens-sqlite-connection-per-request)
  - [EDGE-030: Post-batch training failure is caught but not surfaced](#edge-030-post-batch-training-failure-is-caught-but-not-surfaced)
- [events.py Edge Cases](#eventspy-edge-cases)
  - [EDGE-031: EventBus is in-memory — all events lost on restart](#edge-031-eventbus-is-in-memory--all-events-lost-on-restart)
  - [EDGE-032: Queue overflow silently drops events](#edge-032-queue-overflow-silently-drops-events)
  - [EDGE-033: No authentication on WebSocket connections](#edge-033-no-authentication-on-websocket-connections)
  - [EDGE-034: subscriber_count reads len(_subscribers) without lock](#edge-034-subscriber_count-reads-len_subscribers-without-lock)

---

## app.py Edge Cases

### EDGE-021: Bounce evidence dict is in-memory — lost on restart

**Severity:** HIGH
**File:** `src/mandate_doctor/api/app.py:47`

#### What it is

```python
bounce_evidence: dict[str, dict[str, Any]] = {}
```

The `bounce_evidence` dictionary stores real payment failure data received from Razorpay `payment.failed` webhooks. It is indexed by `reference_id` (the payment link's `notes.reference_id`). The collector's `_wait_for_bounce_evidence()` function looks up this dict to attach the real failed-payment ID and error code to each dataset row.

#### Why it matters

This dict lives entirely in process memory. If the server restarts mid-batch — whether from a crash, an OOM kill, or a deliberate deploy — every piece of collected bounce evidence vanishes.

The collector's `_wait_for_bounce_evidence()` will then timeout waiting for evidence that will never arrive. Rows that should have a populated `failed_payment_id` get `NULL` instead, degrading the quality of the training data.

#### When it happens

- Server crash during a batch
- Deploy restart while batch is running
- Process killed by OOM or systemd

#### Mitigation

In test mode, batches can be restarted from scratch, so data loss is recoverable. In production, the impact is degraded training data quality — not catastrophic, but compounding over time.

#### Recommended fix

Persist bounce evidence to SQLite or write through to the `outcomes` table immediately when the `payment.failed` webhook arrives. This makes bounce evidence survive restarts. A write-through approach also eliminates the race between webhook arrival and collector consumption.

---

### EDGE-022: received_events list grows unbounded

**Severity:** MEDIUM
**File:** `src/mandate_doctor/api/app.py:42`

#### What it is

```python
received_events: list[dict[str, str]] = []
```

Every inbound webhook appends an entry to `received_events`. The `GET /api/events` endpoint returns this entire list, and `GET /health` reports its length.

#### Why it matters

There is no maximum size, no TTL, and no cleanup. After thousands of webhooks, the `/api/events` endpoint returns a massive JSON payload. Memory grows linearly with webhook count. In a long-running process receiving sustained traffic, this is a slow memory leak.

#### When it happens

- Long-running server processing thousands of webhooks
- Dashboard polling `/api/events` repeatedly
- No server restart to reset the list

#### Mitigation

None currently.

#### Recommended fix

Replace the `list` with a `collections.deque(maxlen=1000)` or a ring buffer. The deque automatically evicts the oldest entries when full, bounding both memory usage and response payload size. If historical events are needed, persist them to a database.

```python
from collections import deque

received_events: deque[dict[str, str]] = deque(maxlen=1000)
```

---

### EDGE-023: HMAC verification doesn't handle malformed signature header

**Severity:** LOW
**File:** `src/mandate_doctor/api/app.py:52-54`

#### What it is

```python
def _verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

The signature comparison uses `hmac.compare_digest`, which is constant-time and safe against timing attacks. It works correctly even if the signature contains non-hex characters — it simply won't match the expected hex digest.

#### Why it matters

If Razorpay sends a signature header with trailing whitespace, newlines, or extra characters (which can happen with proxy headers or middleware), the comparison silently returns `False`. The webhook is rejected with a 400 error. The root cause is non-obvious because the signature *looks* correct.

#### When it happens

- Proxy or load balancer adds whitespace to headers
- Razorpay SDK or webhook sends padding
- Copy-paste of signature includes trailing newline

#### Mitigation

`compare_digest` is constant-time (safe against timing attacks). The rejection is correct behavior.

#### Recommended fix

Strip whitespace from the signature header before comparison:

```python
signature = request.headers.get("X-Razorpay-Signature", "").strip()
```

This is a one-line fix that eliminates a class of debugging headaches.

---

### EDGE-024: CORS whitelist is hardcoded

**Severity:** MEDIUM
**File:** `src/mandate_doctor/api/app.py:35-40`

#### What it is

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8501"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
```

The CORS origin whitelist is hardcoded to two localhost ports: 3000 (likely a dev frontend) and 8501 (Streamlit dashboard).

#### Why it matters

If the dashboard is served from any other origin — an ngrok tunnel, a production domain, a different port — the browser blocks all cross-origin requests. The dashboard appears to load but API calls fail silently or with CORS errors in the console.

#### When it happens

- ngrok or similar tunnel used for remote access
- Dashboard deployed to a different host/port
- Production domain differs from localhost

#### Mitigation

Acceptable for test mode. Development servers typically run on localhost.

#### Recommended fix

Read CORS origins from an environment variable:

```python
import os

cors_origins = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:3000,http://localhost:8501"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
```

---

### EDGE-025: Only one batch can run at a time

**Severity:** MEDIUM
**File:** `src/mandate_doctor/api/app.py:299`

#### What it is

```python
if _batch_task is not None and not _batch_task.done():
    raise HTTPException(status_code=409, detail="A batch is already running")
```

The `start_batch` endpoint checks whether a batch task is already running. If it is, the new request gets a 409 Conflict.

#### Why it matters

A batch that gets stuck — rate-limited by Razorpay, hung on a slow network request, or caught in an infinite loop — blocks all subsequent batch starts. There is no built-in timeout or force-kill mechanism. The only recourse is to call `POST /api/batch/stop`, which sets a stop event but waits for in-flight scenarios to finish.

If the batch is stuck in a way that doesn't check the stop event, you have to kill the entire process.

#### When it happens

- Razorpay rate-limits the batch worker
- Network partition causes hanging requests
- Batch worker enters a state where it doesn't poll the stop event

#### Mitigation

The `POST /api/batch/stop` endpoint exists and sets `_batch_stop`. In-flight work is expected to finish gracefully.

#### Recommended fix

Add a batch timeout (e.g., 10 minutes) and a force-kill mechanism. The timeout should be configurable via the `BatchRequest` model or a server-level setting. If the batch exceeds the timeout, cancel the task and reset `_batch_state`.

---

### EDGE-026: _batch_stop Event not cleared between batches

**Severity:** MEDIUM
**File:** `src/mandate_doctor/api/app.py:307`

#### What it is

```python
_batch_stop.clear()  # called in start_batch
```

When a new batch starts, `_batch_stop.clear()` is called to reset the stop signal. This is correct — a new batch should not inherit the stop signal from a previous batch.

#### Why it matters

The subtle edge case is the race between `stop_batch` and batch completion:

1. Batch is running.
2. `stop_batch` is called → `_batch_stop.set()`.
3. Batch finishes (the `finally` block sets `_batch_state.running = False` but does **not** clear `_batch_stop`).
4. A new batch starts immediately → `_batch_stop.clear()` resets it.

This works correctly. However, if `stop_batch` is called and the batch is already in its `finally` block (between setting `running=False` and returning), there is a brief window where `_batch_stop` is still set but `_batch_state.running` is `False`. A concurrent `start_batch` call would see `_batch_task.done() == True` and proceed, clearing the stop event. This is fine — but it relies on `start_batch` always being the one to clear the event.

#### When it happens

- `stop_batch` called at the exact moment a batch completes
- Rapid stop/start cycles

#### Mitigation

`_batch_stop.clear()` in `start_batch` prevents stale state from leaking across batches.

#### Recommended fix

Clear `_batch_stop` in the `finally` block of `_run()` as well, so the event is always in a clean state after batch completion regardless of how the batch ended:

```python
finally:
    _batch_state.update(running=False, batch_id=None)
    _batch_stop.clear()
```

---

### EDGE-027: Periodic trainer starts after 90s warm-up

**Severity:** LOW
**File:** `src/mandate_doctor/api/app.py:385`

#### What it is

```python
async def _periodic_trainer() -> None:
    await asyncio.sleep(90)  # warm-up
    while True:
        # ... train ...
        await asyncio.sleep(900)  # every 15 minutes
```

The periodic trainer waits 90 seconds after startup before running its first training cycle.

#### Why it matters

If the first batch completes before the 90-second warm-up expires, the model is not retrained from the fresh labeled data until the periodic loop kicks in (potentially up to 90 seconds later).

#### When it happens

- Fast initial batch (few pages, fast network)
- Model starts with stale metrics

#### Mitigation

Post-batch training also triggers via `_train_now()` inside the `_run()` function (the batch runner). So the model is retrained immediately after a batch, regardless of the periodic trainer's warm-up timer.

#### Recommended fix

No fix needed. The redundant training path (`_train_now` in `_run`) ensures fresh data is always used. The periodic trainer is a safety net for cases where the post-batch training fails or is skipped.

---

### EDGE-028: WebSocket clients don't get backpressure signal

**Severity:** MEDIUM
**File:** `src/mandate_doctor/api/app.py:446-458`

#### What it is

```python
@app.websocket("/ws")
async def ws_events(websocket: WebSocket) -> None:
    await websocket.accept()
    client_id, queue = await bus.subscribe()
    try:
        await websocket.send_json({"type": "connected", "subscribers": bus.subscriber_count})
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        await bus.unsubscribe(client_id)
```

Each WebSocket client gets an `asyncio.Queue(maxsize=500)`. If the client is slow (network lag, browser throttling, devtools open), events queue up. When the queue fills, the `EventBus.publish()` method silently drops events using `contextlib.suppress(asyncio.QueueFull)`.

#### Why it matters

The client never learns that events were dropped. The dashboard may show a gap in the event stream without any indication that data was lost. For a monitoring dashboard, this gap could mean the difference between catching an issue and missing it entirely.

#### When it happens

- Dashboard browser tab is backgrounded (browsers throttle background tabs)
- Network latency spikes
- Client processing is slow (e.g., DOM updates)

#### Mitigation

Queue overflow drops are logged by the `EventBus` (the `put_nowait` call is inside `suppress(QueueFull)`, but the logging happens elsewhere). This is acceptable for a dashboard where eventual consistency is the goal.

#### Recommended fix

Add a `dropped_count` field to the next event sent to the client:

```python
# In EventBus.publish(), track per-client drops
# In ws_events, send a summary on the next event
```

This gives the dashboard UI the information it needs to display a "X events dropped" banner.

---

### EDGE-029: /api/stats opens SQLite connection per request

**Severity:** LOW
**File:** `src/mandate_doctor/api/app.py:195-247`

#### What it is

```python
@app.get("/api/stats")
async def get_stats() -> dict[str, Any]:
    import sqlite3
    db = settings.project_root / "data" / "training_data.db"
    conn = sqlite3.connect(str(db))
    # ... 5 queries ...
    conn.close()
```

Every `GET /api/stats` call opens a new `sqlite3.connect()`, runs 5 queries (totals, by_regime, by_class, by_bank, by_amount), and closes the connection.

#### Why it matters

Under a high dashboard refresh rate (e.g., auto-refresh every 5 seconds), this creates many short-lived connections. SQLite handles this fine for a single-process application, but it is not ideal.

#### When it happens

- Dashboard auto-refreshes frequently
- Multiple browser tabs open to the dashboard
- Monitoring tools polling the endpoint

#### Mitigation

SQLite handles short-lived connections efficiently for single-process applications. The overhead is negligible at typical dashboard refresh rates.

#### Recommended fix

Use a module-level connection or a simple connection pool:

```python
import sqlite3

_stats_conn: sqlite3.Connection | None = None

def _get_stats_conn() -> sqlite3.Connection:
    global _stats_conn
    if _stats_conn is None:
        _stats_conn = sqlite3.connect(str(settings.project_root / "data" / "training_data.db"))
        _stats_conn.row_factory = sqlite3.Row
    return _stats_conn
```

---

### EDGE-030: Post-batch training failure is caught but not surfaced

**Severity:** MEDIUM
**File:** `src/mandate_doctor/api/app.py:326-327`

#### What it is

```python
try:
    await _train_now()
except Exception as exc:
    logger.error("post_batch_training_failed", error=str(exc))
return result
```

After a batch completes, `_train_now()` is called to retrain the model on fresh data. If training fails, the exception is caught and logged, but the batch result is returned as if everything succeeded.

#### Why it matters

The dashboard shows stale model metrics without indicating that training failed. A user might see a batch complete successfully and assume the model is up to date, when in fact it is still running on old data.

#### When it happens

- Training data is malformed or empty
- Model artifact file is locked or corrupted
- Disk full, write permission denied

#### Mitigation

Training failures are logged at error level. An operator monitoring logs would catch this.

#### Recommended fix

Include a `training_status` field in the batch result:

```python
training_status = "ok"
try:
    await _train_now()
except Exception as exc:
    logger.error("post_batch_training_failed", error=str(exc))
    training_status = f"failed: {exc}"
return {**result, "training_status": training_status}
```

---

## events.py Edge Cases

### EDGE-031: EventBus is in-memory — all events lost on restart

**Severity:** HIGH
**File:** `src/mandate_doctor/api/events.py:26-60`

#### What it is

```python
class EventBus:
    def __init__(self, max_queue: int = 500) -> None:
        self._subscribers: dict[int, asyncio.Queue[dict[str, Any]]] = {}
        self._next_id = 0
        self._max_queue = max_queue
        self._lock = asyncio.Lock()
```

The `EventBus` is a pure in-memory fan-out hub. It stores no events persistently. When the server restarts:

- All subscribers are disconnected.
- All queued events in client queues are lost.
- The `_next_id` counter resets to 0.

#### Why it matters

The dashboard loses its live feed on restart. Any events that were queued but not yet delivered to clients are gone. For a monitoring dashboard, this is a gap in observability.

#### When it happens

- Server restart (deploy, crash, OOM)
- Process manager restarts the application

#### Mitigation

The dashboard reconnects automatically and re-fetches stats via `GET /api/stats`. The live feed resumes from the restart point.

#### Recommended fix

Optionally persist the last N events (e.g., 1000) to a ring buffer or SQLite table. On reconnect, replay these events to the new subscriber so they can see what happened during the disconnection. This is a nice-to-have for observability, not a correctness requirement.

---

### EDGE-032: Queue overflow silently drops events

**Severity:** MEDIUM
**File:** `src/mandate_doctor/api/events.py:52-53`

#### What it is

```python
for q in queues:
    with contextlib.suppress(asyncio.QueueFull):
        q.put_nowait(event)
```

When a client's queue is full (500 events), the `QueueFull` exception is suppressed. The event is silently dropped for that client.

#### Why it matters

The client never receives notification that events were dropped. The dashboard may show a gap in the event timeline without any indication of data loss. For debugging pipeline issues, these gaps can be confusing.

#### When it happens

- Client is slow to consume events
- Browser tab is backgrounded (browsers throttle JavaScript)
- Network latency spikes

#### Mitigation

Acceptable for a dashboard where eventual consistency is the goal. The pipeline itself is not affected — only the dashboard view.

#### Recommended fix

Add a per-client dropped counter to the `EventBus`. On the next event delivered to that client, include a `dropped_since_last` field:

```python
class EventBus:
    def __init__(self, max_queue: int = 500) -> None:
        self._subscribers: dict[int, asyncio.Queue[dict[str, Any]]] = {}
        self._dropped: dict[int, int] = {}  # per-client drop count
        # ...

    async def publish(self, event: dict[str, Any]) -> None:
        event = {"ts": time.time(), **event}
        async with self._lock:
            queues = list(self._subscribers.items())
        for client_id, q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._dropped[client_id] = self._dropped.get(client_id, 0) + 1
```

---

### EDGE-033: No authentication on WebSocket connections

**Severity:** MEDIUM
**File:** `src/mandate_doctor/api/app.py:446-448`

#### What it is

```python
@app.websocket("/ws")
async def ws_events(websocket: WebSocket) -> None:
    await websocket.accept()
    client_id, queue = await bus.subscribe()
```

Any client that can reach the server can connect to `/ws` and receive all pipeline events. There is no authentication, no token check, and no origin validation.

#### Why it matters

In production, this exposes internal operation details — webhook events, batch progress, training metrics, idempotency decisions — to anyone who can reach the WebSocket endpoint. On a public network or with port forwarding, this is a security concern.

#### When it happens

- Server exposed to the internet (even behind a reverse proxy)
- Internal network with untrusted actors
- ngrok or similar tunnels used for remote access

#### Mitigation

Acceptable for test mode on localhost. The application is designed for single-developer use during the test/recovery phase.

#### Recommended fix

For production deployments, add token-based authentication:

```python
@app.websocket("/ws")
async def ws_events(websocket: WebSocket, token: str = Query(None)) -> None:
    if token != settings.ws_auth_token:
        await websocket.close(code=4001, reason="unauthorized")
        return
    await websocket.accept()
    # ...
```

Alternatively, validate the `Origin` header against a whitelist.

---

### EDGE-034: subscriber_count reads len(_subscribers) without lock

**Severity:** LOW
**File:** `src/mandate_doctor/api/events.py:56-57`

#### What it is

```python
@property
def subscriber_count(self) -> int:
    return len(self._subscribers)
```

The `subscriber_count` property reads `len(self._subscribers)` without acquiring `self._lock`. During concurrent `subscribe()` or `unsubscribe()` calls, the count could be stale.

#### Why it matters

This is used purely for informational display (shown in the WebSocket "connected" message). A stale count is harmless — it might show "2 subscribers" when there are actually 3, or vice versa. It does not affect correctness of any logic.

#### When it happens

- Multiple WebSocket clients connect/disconnect simultaneously
- High-frequency subscribe/unsubscribe race conditions

#### Mitigation

Used for informational display only. A stale count is harmless.

#### Recommended fix

For accuracy, acquire the lock:

```python
@property
def subscriber_count(self) -> int:
    # Note: acquiring async lock in sync property is tricky.
    # Use a snapshot or accept staleness.
    return len(self._subscribers)
```

In practice, `asyncio.Lock` cannot be acquired in a synchronous property. The pragmatic fix is to document that this value is approximate, or to convert it to an async method:

```python
async def get_subscriber_count(self) -> int:
    async with self._lock:
        return len(self._subscribers)
```

This requires changing the call site in `app.py` from `bus.subscriber_count` to `await bus.get_subscriber_count()`.

---

## Summary

| Edge Case | Severity | File | Core Issue |
|-----------|----------|------|------------|
| EDGE-021 | HIGH | app.py:47 | `bounce_evidence` dict lost on restart |
| EDGE-022 | MEDIUM | app.py:42 | `received_events` list unbounded |
| EDGE-023 | LOW | app.py:52-54 | Signature header not stripped |
| EDGE-024 | MEDIUM | app.py:35-40 | Hardcoded CORS origins |
| EDGE-025 | MEDIUM | app.py:299 | No batch timeout or force-kill |
| EDGE-026 | MEDIUM | app.py:307 | `_batch_stop` not cleared in finally |
| EDGE-027 | LOW | app.py:385 | 90s trainer warm-up (redundant path exists) |
| EDGE-028 | MEDIUM | app.py:446-458 | No backpressure signal to clients |
| EDGE-029 | LOW | app.py:195-247 | No SQLite connection pooling |
| EDGE-030 | MEDIUM | app.py:326-327 | Training failure not surfaced |
| EDGE-031 | HIGH | events.py:26-60 | EventBus in-memory, lost on restart |
| EDGE-032 | MEDIUM | events.py:52-53 | Silent event drops on queue full |
| EDGE-033 | MEDIUM | app.py:446-448 | No WebSocket authentication |
| EDGE-034 | LOW | events.py:56-57 | Unsynchronized subscriber count |

**HIGH:** 2 — both involve data loss on server restart.
**MEDIUM:** 8 — affect observability, security, or operational resilience.
**LOW:** 4 — minor issues with existing mitigations or low impact.

### Next Steps

- [Chapter 4: ML Model Edge Cases](./04-ml-model.md) — covers classifier retraining, feature drift, and model artifact corruption.
- [Chapter 5: Idempotency & Recovery](./05-idempotency-recovery.md) — covers exactly-once guarantees, SQLite contention, and race conditions.
