# Chapter 6: Deployment & Infrastructure Edge Cases

The deployment layer is the system's operational backbone — configuration management, process lifecycle, database persistence, and network exposure. This chapter documents every known edge case across `config.py`, `scripts/serve.sh`, `pyproject.toml`, and the cross-cutting infrastructure concerns.

Each edge case is tagged with a severity rating:

| Severity | Meaning |
|----------|---------|
| **CRITICAL** | Data loss or corruption in production |
| **HIGH** | Silent misclassification or safety violation |
| **MEDIUM** | Correctness issue that may surface under load |
| **LOW** | Dead code or cosmetic inconsistency |

---

## Table of Contents

- [config.py Edge Cases](#configpy-edge-cases)
  - [EDGE-064: Empty API keys raise on first Razorpay call — no startup validation](#edge-064-empty-api-keys-raise-on-first-razorpay-call--no-startup-validation)
  - [EDGE-065: Extra env vars silently ignored](#edge-065-extra-env-vars-silently-ignored)
  - [EDGE-066: auto_recover defaults to False — silent no-op on recovery](#edge-066-auto_recover-defaults-to-false--silent-no-op-on-recovery)
  - [EDGE-067: database_url points to non-existent SQLite file](#edge-067-database_url-points-to-non-existent-sqlite-file)
- [scripts/serve.sh Edge Cases](#scriptsservesh-edge-cases)
  - [EDGE-068: setsid nohup detaches from terminal — no log rotation](#edge-068-setsid-nohup-detaches-from-terminal--no-log-rotation)
  - [EDGE-069: No process health check after startup](#edge-069-no-process-health-check-after-startup)
  - [EDGE-070: ngrok static domain may expire](#edge-070-ngrok-static-domain-may-expire)
  - [EDGE-071: No graceful shutdown — SIGTERM kills in-flight batches](#edge-071-no-graceful-shutdown--sigterm-kills-in-flight-batches)
- [pyproject.toml Edge Cases](#pyprojecttoml-edge-cases)
  - [EDGE-072: mypy --strict may miss runtime type errors](#edge-072-mypy---strict-may-miss-runtime-type-errors)
  - [EDGE-073: ruff per-file-ignores for tests — test code quality blind spot](#edge-073-ruff-per-file-ignores-for-tests--test-code-quality-blind-spot)
- [Cross-Cutting Deployment Edge Cases](#cross-cutting-deployment-edge-cases)
  - [EDGE-074: Single-process architecture — no horizontal scaling](#edge-074-single-process-architecture--no-horizontal-scaling)
  - [EDGE-075: No HTTPS termination — ngrok handles TLS](#edge-075-no-https-termination--ngrok-handles-tls)
  - [EDGE-076: SQLite not suitable for production concurrency](#edge-076-sqlite-not-suitable-for-production-concurrency)
  - [EDGE-077: No structured logging to external system](#edge-077-no-structured-logging-to-external-system)
  - [EDGE-078: No rate limiting on API endpoints](#edge-078-no-rate-limiting-on-api-endpoints)
  - [EDGE-079: No authentication on any endpoint](#edge-079-no-authentication-on-any-endpoint)

---

## config.py Edge Cases

### EDGE-064: Empty API keys raise on first Razorpay call — no startup validation

**Severity:** HIGH
**File:** `src/mandate_doctor/config.py:19-22`

#### What it is

```python
razorpay_key_id: str = ""
razorpay_key_secret: str = ""
```

`razorpay_key_id` and `razorpay_key_secret` default to empty string `""`. There is no validation at startup. The error is only discovered when the first API call is made — specifically in `_auth()`, which checks for a `rzp_test_` prefix.

#### Why it matters

A misconfigured server runs fine until a webhook triggers a recovery attempt. The server starts, passes the health check, and appears operational. But the moment a customer mandate fails and the system tries to create a payment link, it crashes with `RazorpayError: [CONFIG_ERROR] RAZORPAY_KEY_ID or RAZORPAY_KEY_SECRET not set`. The crash happens during business logic, not at startup — making it harder to diagnose.

#### When it happens

- Deploying with missing environment variables
- Copying `.env.example` without filling in real values
- CI/CD pipeline that doesn't inject secrets

#### Mitigation

The `_auth()` function in `services/razorpay.py` does catch empty keys and raises `RazorpayError` with a clear message. The failure is not silent — but it is late.

#### Recommended fix

Validate non-empty keys at startup; fail fast if missing:

```python
@model_validator(mode="after")
def _validate_keys(self) -> "Settings":
    if not self.razorpay_key_id or not self.razorpay_key_secret:
        raise ValueError(
            "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be non-empty. "
            "Set them in your .env file or environment."
        )
    if not self.razorpay_key_id.startswith("rzp_test_"):
        raise ValueError(
            f"RAZORPAY_KEY_ID must start with 'rzp_test_'. Got: {self.razorpay_key_id[:10]}..."
        )
    return self
```

This catches misconfiguration at process start, not at first API call.

---

### EDGE-065: Extra env vars silently ignored

**Severity:** LOW
**File:** `src/mandate_doctor/config.py:43`

#### What it is

```python
model_config = {"extra": "ignore"}
```

Pydantic's `extra = "ignore"` means any environment variable that doesn't match a declared field is silently discarded. A typo like `RAZORPAY_KEY_IDD` (extra D) would not load the actual key — but no error is raised.

#### Why it matters

This is a common source of misconfiguration. An operator types `RAZORPAY_KEY_IDD` in their `.env` file, the server starts successfully, and the actual key remains empty. The operator thinks the key is loaded; it is not.

#### When it happens

- Typos in environment variable names
- Copy-paste errors when setting up `.env`
- Case sensitivity mismatches (`razorpay_key_id` vs `RAZORPAY_KEY_ID`)

#### Mitigation

EDGE-064's startup validation catches the downstream effect (empty key), but the root cause (typo in env var name) remains invisible.

#### Recommended fix

Use `extra = "forbid"` in production, or log a warning for unrecognized env vars:

```python
# Option 1: strict mode (production)
model_config = {"extra": "forbid"}

# Option 2: warn on unknown (development-friendly)
model_config = {"extra": "warn"}
```

For `extra = "forbid"`, ensure all expected env vars are declared in the Settings model. For `extra = "warn"`, Pydantic logs a warning for each unrecognized variable — helpful for catching typos without breaking startup.

---

### EDGE-066: auto_recover defaults to False — silent no-op on recovery

**Severity:** MEDIUM
**File:** `src/mandate_doctor/config.py:22`

#### What it is

```python
auto_recover: bool = False
```

When `auto_recover=False`, the idempotent recovery path records `"audit_only"` in the `execution_ref` but does not create a payment link. The dashboard shows recovery actions as "executed" but nothing actually happened — no payment link was sent to the customer.

#### Why it matters

An operator viewing the dashboard sees "Recovery executed" entries and assumes the customer received a payment link. In reality, the system was in audit-only mode. The customer never got the link. The mandate remains unpaid.

The `"audit_only"` prefix in `execution_ref` makes it distinguishable, but only if someone checks that field. The status column says "executed" regardless.

#### When it happens

- Default deployment (auto_recover defaults to False)
- Operator forgets to set `AUTO_RECOVER=true` in `.env`
- Test mode where audit-only is intentional but the dashboard doesn't clarify

#### Mitigation

`"audit_only"` prefix in `execution_ref` makes it distinguishable from real recoveries. The dashboard can filter on this prefix.

#### Recommended fix

Display "audit only" prominently in the dashboard when `auto_recover=False`:

```python
# In dashboard template or API response:
recovery_status = "audit_only" if not settings.auto_recover else "live"
```

Add a banner to the dashboard: "Recovery is in audit-only mode. No payment links will be sent."

---

### EDGE-067: database_url points to non-existent SQLite file

**Severity:** LOW
**File:** `src/mandate_doctor/config.py:31`

#### What it is

```python
database_url: str = "sqlite:///./mandate_doctor.db"
```

The default `database_url` points to `mandate_doctor.db`, but the actual code uses explicit paths:

```python
training_data.db  # for scenario outcomes
idempotency.db    # for idempotency keys
```

The `database_url` setting is unused — a red herring.

#### Why it matters

A developer reading the config sees `database_url` and assumes it controls database location. They change it, expecting the database to move. Nothing changes. The actual database paths are hardcoded elsewhere.

This is misleading configuration — it suggests control that doesn't exist.

#### When it happens

- Always — the setting is never used
- Causes confusion during debugging or migration

#### Mitigation

Code uses explicit paths (`settings.project_root / "data" / "training_data.db"`), so the unused setting has no runtime impact.

#### Recommended fix

Remove the unused `database_url` setting, or add a comment explaining it is unused:

```python
# NOTE: This setting is unused. Databases are at:
#   - data/training_data.db (scenario outcomes)
#   - data/idempotency.db (idempotency keys)
database_url: str = ""  # unused
```

Better: remove it entirely to avoid confusion.

---

## scripts/serve.sh Edge Cases

### EDGE-068: setsid nohup detaches from terminal — no log rotation

**Severity:** MEDIUM
**File:** `scripts/serve.sh`

#### What it is

```bash
setsid nohup uvicorn ... > /tmp/md-uvicorn.log 2>&1 &
setsid nohup ngrok ... > /tmp/ngrok.log 2>&1 &
```

Server output goes to `/tmp/md-uvicorn.log` and `/tmp/ngrok.log`. There is no log rotation. Logs grow unbounded. After days of operation, `/tmp` fills up.

#### Why it matters

In production, logs accumulate indefinitely. If the server runs for weeks, the log files can consume gigabytes. On systems with small `/tmp` partitions (e.g., containers with tmpfs), this can fill the filesystem and cause cascading failures.

#### When it happens

- Long-running server instances (days/weeks)
- Containers with limited `/tmp` size
- High webhook volume generating verbose logs

#### Mitigation

Acceptable for short-lived test mode sessions. The script is designed for development/testing, not persistent production deployment.

#### Recommended fix

Add logrotate config or use systemd journal:

```bash
# Option 1: logrotate config
# /etc/logrotate.d/md-doctor
/tmp/md-uvicorn.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
}

# Option 2: systemd service (preferred for production)
# [Service]
# StandardOutput=journal
# StandardError=journal
```

For containerized deployments, log to stdout and let the container runtime handle log collection.

---

### EDGE-069: No process health check after startup

**Severity:** MEDIUM
**File:** `scripts/serve.sh`

#### What it is

The script starts ngrok and uvicorn but doesn't verify they're running. If uvicorn crashes immediately (import error, port conflict), the script succeeds silently.

#### Why it matters

The operator runs `./scripts/serve.sh`, sees "Server started" output, and assumes everything is working. But uvicorn crashed on startup due to a missing dependency or port conflict. The ngrok tunnel is up, pointing to a dead server. Razorpay webhooks hit the tunnel and get connection refused.

#### When it happens

- Port 8000 already in use
- Missing Python dependency (import error at startup)
- uvicorn crashes before binding the port
- ngrok tunnel established but backend is down

#### Mitigation

Manual check via `curl localhost:8000/health`. But this requires the operator to know to check.

#### Recommended fix

Add health check loop after startup:

```bash
# Wait for uvicorn to be ready
for i in {1..10}; do
    if curl -s http://localhost:8000/health > /dev/null 2>&1; then
        echo "Server is ready"
        break
    fi
    if [ $i -eq 10 ]; then
        echo "Server failed to start within 10 seconds"
        exit 1
    fi
    sleep 1
done
```

This catches startup failures before the script reports success.

---

### EDGE-070: ngrok static domain may expire

**Severity:** LOW
**File:** `scripts/serve.sh`

#### What it is

```bash
ngrok http --domain=lubricant-unkind-popsicle.ngrok-free.dev 8000
```

The ngrok static domain `lubricant-unkind-popsicle.ngrok-free.dev` is configured. Free ngrok domains expire after 1 hour of inactivity. The Razorpay webhook URL becomes stale.

#### Why it matters

When the ngrok domain expires, Razorpay webhook deliveries fail silently. Razorpay disables the webhook after 24 hours of consecutive failures. The operator receives an email notification but may miss it. The next mandate failure triggers a recovery attempt, which tries to update the payment link — but the webhook endpoint is dead.

This actually happened: the webhook was disabled after 24h of failures, requiring manual re-enablement.

#### When it happens

- Server is inactive for > 1 hour (free ngrok limitation)
- Weekend or overnight downtime
- Server restart after a long pause

#### Mitigation

Razorpay notifies via email when webhooks are disabled. Re-enable requires manual intervention via Razorpay dashboard.

#### Recommended fix

Use ngrok paid plan for persistent domains, or use Cloudflare Tunnel:

```bash
# Option 1: ngrok paid plan (persistent domain)
ngrok http --domain=your-paid-domain.ngrok-free.dev 8000

# Option 2: Cloudflare Tunnel (free, persistent)
cloudflared tunnel --url http://localhost:8000
```

For production, use a proper domain with TLS termination (see EDGE-075).

---

### EDGE-071: No graceful shutdown — SIGTERM kills in-flight batches

**Severity:** MEDIUM
**File:** `scripts/serve.sh`

#### What it is

```bash
# No signal handling in the script
# Killing the server process sends SIGTERM
kill $(pgrep -f uvicorn)
```

Killing the server process doesn't trigger FastAPI shutdown events. In-progress Playwright browser sessions are orphaned. The `_batch_stop` event is never set; batch state remains "running" on next start.

#### Why it matters

When the server is killed, any in-flight batch is left in a partial state:

1. Playwright browser processes are orphaned (zombie processes)
2. `_batch_stop` event is never set, so batch state remains "running"
3. On restart, `_batch_task` is `None` (new process), so `start_batch` works — but the previous batch's state is inconsistent
4. Scenarios that were mid-collection have no outcome recorded

#### When it happens

- Operator kills the server during a batch run
- System reboot during batch processing
- Container orchestration (Docker stop, Kubernetes SIGTERM)

#### Mitigation

On restart, `_batch_task` is `None` (new process), so `start_batch` works. The batch state inconsistency is limited to the dashboard display.

#### Recommended fix

Trap SIGTERM, set `_batch_stop`, wait for in-flight scenarios:

```python
import signal

async def _shutdown_handler():
    if _batch_stop is not None:
        _batch_stop.set()
    # Wait for in-flight workers to finish (with timeout)
    await asyncio.sleep(5)  # grace period

# In FastAPI lifespan:
@app.on_event("shutdown")
async def shutdown():
    await _shutdown_handler()
```

For the shell script, use `trap` to forward signals:

```bash
trap 'kill $(jobs -p); exit' SIGTERM SIGINT
```

---

## pyproject.toml Edge Cases

### EDGE-072: mypy --strict may miss runtime type errors

**Severity:** LOW
**File:** `pyproject.toml`

#### What it is

```toml
[tool.mypy]
strict = true
```

mypy strict mode catches static type issues but not runtime Pydantic validation errors. A malformed webhook payload passes mypy but raises `ValidationError` at runtime.

#### Why it matters

mypy verifies type annotations at development time. Pydantic validates data at runtime. The two are complementary but non-overlapping. A webhook payload with `amount: "not_a_number"` passes mypy (the field is annotated as `int`, but mypy doesn't check incoming JSON) but fails at runtime with a Pydantic `ValidationError`.

#### When it happens

- Malformed webhook payloads from Razorpay
- API changes that alter field types
- Missing fields in webhook data

#### Mitigation

Pydantic validates at runtime; FastAPI catches `ValidationError` and returns 422. The system doesn't crash — it returns a structured error.

#### Recommended fix

Add runtime validation tests for webhook payloads:

```python
def test_webhook_payload_validation():
    """Malformed payloads are rejected, not crash."""
    from mandate_doctor.models.webhook import WebhookEvent
    with pytest.raises(ValidationError):
        WebhookEvent.model_validate({"amount": "not_a_number"})
```

---

### EDGE-073: ruff per-file-ignores for tests — test code quality blind spot

**Severity:** LOW
**File:** `pyproject.toml`

#### What it is

```toml
[tool.ruff.per-file-ignores]
"tests/**" = ["ARG", "S", "PLR2004", ...]
```

Tests are excluded from some ruff rules (`ARG` unused arguments, `S` security checks, `PLR2004` magic numbers). Test code can have quality issues without lint warnings.

#### Why it matters

Test code is inherently more exploratory — it uses magic numbers, broad exceptions, and unused fixtures. But blanket ignores mean test code never gets cleaned up. Over time, test quality degrades: magic numbers proliferate, dead fixtures accumulate, and security patterns are not validated.

#### When it happens

- Always — test code is permanently excluded from these rules
- Accumulates over time as tests grow

#### Mitigation

Test code is inherently more exploratory. The ignored rules (`ARG`, `S`, `PLR2004`) are less relevant in test context.

#### Recommended fix

Enable more rules in tests over time. Start with security (`S`) — test code should not have hardcoded credentials or unsafe operations:

```toml
[tool.ruff.per-file-ignores]
"tests/**" = ["ARG", "PLR2004"]  # keep these ignores
# Remove "S" to enforce security checks in tests too
```

---

## Cross-Cutting Deployment Edge Cases

### EDGE-074: Single-process architecture — no horizontal scaling

**Severity:** MEDIUM
**File:** Cross-cutting

#### What it is

The entire system (API server + batch collector + model trainer + dashboard) runs in one Python process. All components share the same event loop, memory space, and database connections.

#### Why it matters

The system cannot scale to handle multiple concurrent merchants or high webhook volume. A single slow batch blocks the API server. Model training consumes CPU that would otherwise serve webhook requests. The dashboard competes for the same resources.

#### When it happens

- Multiple merchants sending webhooks simultaneously
- High webhook volume (> 100/minute)
- Batch collection + model training running concurrently
- Dashboard access during batch processing

#### Mitigation

Acceptable for test mode / single merchant. The current architecture is intentionally simple.

#### Recommended fix

Separate API server from batch worker; use a task queue:

```
# Production architecture
API Server (FastAPI)          → handles webhooks, dashboard
Task Queue (Celery/RQ)        → manages batch jobs
Batch Worker (separate process) → runs Playwright collection
Model Trainer (separate process) → trains on collected data
```

This allows horizontal scaling of the API server independently of batch workers.

---

### EDGE-075: No HTTPS termination — ngrok handles TLS

**Severity:** LOW
**File:** Cross-cutting

#### What it is

The uvicorn server runs on HTTP (`0.0.0.0:8000`). TLS is terminated by ngrok's tunnel. If ngrok is removed, traffic is plaintext.

#### Why it matters

Razorpay webhooks require HTTPS. Without ngrok (or another TLS proxy), the webhook endpoint is unreachable. In production, plaintext HTTP exposes webhook payloads to network inspection.

#### When it happens

- Running without ngrok (direct access)
- ngrok tunnel expires (see EDGE-070)
- Production deployment without TLS

#### Mitigation

Acceptable for test mode. Razorpay webhooks require HTTPS, so ngrok is mandatory in test mode anyway.

#### Recommended fix

Use nginx/caddy for TLS termination in production:

```nginx
# nginx config
server {
    listen 443 ssl;
    server_name your-domain.com;

    ssl_certificate /etc/ssl/certs/your-domain.pem;
    ssl_certificate_key /etc/ssl/private/your-domain.key;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

### EDGE-076: SQLite not suitable for production concurrency

**Severity:** HIGH
**File:** Cross-cutting

#### What it is

Both `training_data.db` and `idempotency.db` use SQLite. SQLite handles one writer at a time. Under high webhook volume, write contention causes `"database is locked"` errors.

#### Why it matters

SQLite's write lock is database-level. Two concurrent webhooks attempting to insert idempotency records will block each other. Under load, this causes timeouts and dropped requests. WAL mode helps but doesn't eliminate the bottleneck — it only allows concurrent reads while writing.

The `idempotency.db` is particularly vulnerable: every webhook creates an idempotency record, and concurrent webhooks from the same merchant (or different merchants) will contend for the write lock.

#### When it happens

- High webhook volume (> 50/minute)
- Multiple concurrent batch operations
- Idempotency record creation during batch collection

#### Mitigation

Test mode volume is low enough that contention is rare. WAL mode provides adequate performance for development.

#### Recommended fix

Use PostgreSQL for production; keep SQLite for local dev only:

```python
# Production
database_url = "postgresql://user:pass@localhost:5432/mandate_doctor"

# Local development
database_url = "sqlite:///./mandate_doctor.db"
```

For idempotency, consider Redis for high-throughput key-value storage.

---

### EDGE-077: No structured logging to external system

**Severity:** LOW
**File:** Cross-cutting

#### What it is

```python
# structlog outputs to stdout/stderr
logger.info("webhook_received", event_type=event.event)
```

structlog outputs structured JSON to stdout/stderr. No integration with ELK, Datadog, CloudWatch, or any external logging service. In production, logs are lost when the process exits.

#### Why it matters

Without external log aggregation:

- Logs are lost on container restart
- No alerting on error patterns
- No distributed tracing across services
- Debugging production issues requires SSH access to the server

#### When it happens

- Always — no external sink is configured
- Container restarts lose all logs
- No log persistence across deployments

#### Mitigation

`/tmp/md-uvicorn.log` captures stdout (see EDGE-068), but only for the current process lifetime.

#### Recommended fix

Add structured log sink to external logging service:

```python
# Option 1: File sink (simple)
import structlog

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.PrintLoggerFactory(
        file=open("/var/log/md-doctor/structured.log", "a")
    ),
)

# Option 2: CloudWatch/Datadog sink (production)
# Forward structured logs to external service via API or agent
```

---

### EDGE-078: No rate limiting on API endpoints

**Severity:** MEDIUM
**File:** `src/mandate_doctor/api/app.py`

#### What it is

```python
@app.post("/api/batch/start")
async def start_batch():
    ...

@app.post("/api/model/train")
async def train_model():
    ...

@app.get("/api/stats")
async def get_stats():
    ...
```

All API endpoints have no rate limiting. A malicious client could trigger infinite training runs or batch starts, consuming CPU and Playwright browser sessions.

#### Why it matters

Without rate limiting:

- `/api/batch/start` can start multiple concurrent batches (resource exhaustion)
- `/api/model/train` can trigger CPU-intensive training repeatedly
- `/api/stats` can be called in a tight loop (minor, but wasteful)
- The system has no protection against abuse

#### When it happens

- Single-user test mode (no external exposure)
- ngrok URL is semi-public (anyone with the URL can hit endpoints)
- No network-level rate limiting

#### Mitigation

Single-user test mode. No external exposure. The ngrok URL is semi-public but obscure.

#### Recommended fix

Add rate limiting middleware for production:

```python
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

@app.post("/api/batch/start")
@limiter.limit("1/minute")
async def start_batch():
    ...

@app.post("/api/model/train")
@limiter.limit("1/hour")
async def train_model():
    ...
```

---

### EDGE-079: No authentication on any endpoint

**Severity:** MEDIUM
**File:** `src/mandate_doctor/api/app.py`

#### What it is

```python
@app.post("/api/batch/start")
async def start_batch():
    ...

@app.post("/api/batch/stop")
async def stop_batch():
    ...

@app.post("/api/model/train")
async def train_model():
    ...
```

All endpoints (batch start/stop, model train, stats) are unauthenticated. Anyone with network access can control the system.

#### Why it matters

The ngrok URL is semi-public. Anyone who discovers the URL can:

- Start/stop batch collection (disrupting operations)
- Trigger model training (wasting CPU)
- Read system stats (information disclosure)
- In production, this is a security vulnerability

#### When it happens

- ngrok URL is shared or discovered
- Server exposed without network-level access control
- Production deployment without authentication

#### Mitigation

Localhost only. ngrok URL is semi-public but obscure. In test mode, the risk is limited to disruption, not data loss.

#### Recommended fix

Add API key authentication for production:

```python
from fastapi import Security, HTTPException
from fastapi.security import APIKeyHeader

API_KEY = os.environ.get("MD_API_KEY")
api_key_header = APIKeyHeader(name="X-API-Key")

async def verify_api_key(key: str = Security(api_key_header)):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

@app.post("/api/batch/start")
async def start_batch(_: str = Security(verify_api_key)):
    ...
```

For production, consider OAuth2 or JWT-based authentication.

---

## Summary

| Edge Case | Severity | File / Location | Core Issue |
|-----------|----------|-----------------|------------|
| EDGE-064 | HIGH | config.py:19-22 | Empty API keys raise on first call, not startup |
| EDGE-065 | LOW | config.py:43 | Extra env vars silently ignored (typos) |
| EDGE-066 | MEDIUM | config.py:22 | auto_recover=False → audit-only but dashboard says "executed" |
| EDGE-067 | LOW | config.py:31 | database_url setting is unused — red herring |
| EDGE-068 | MEDIUM | scripts/serve.sh | Log files grow unbounded in /tmp |
| EDGE-069 | MEDIUM | scripts/serve.sh | No health check after startup — silent failures |
| EDGE-070 | LOW | scripts/serve.sh | ngrok free domain expires after 1h inactivity |
| EDGE-071 | MEDIUM | scripts/serve.sh | SIGTERM kills in-flight batches — no graceful shutdown |
| EDGE-072 | LOW | pyproject.toml | mypy strict misses runtime Pydantic validation errors |
| EDGE-073 | LOW | pyproject.toml | Test code excluded from lint rules — quality blind spot |
| EDGE-074 | MEDIUM | Cross-cutting | Single-process architecture — no horizontal scaling |
| EDGE-075 | LOW | Cross-cutting | No HTTPS termination — ngrok handles TLS |
| EDGE-076 | HIGH | Cross-cutting | SQLite write contention under production concurrency |
| EDGE-077 | LOW | Cross-cutting | Logs lost on process exit — no external sink |
| EDGE-078 | MEDIUM | Cross-cutting | No rate limiting on API endpoints |
| EDGE-079 | MEDIUM | Cross-cutting | No authentication on any endpoint |

**HIGH:** 2 — startup validation (EDGE-064) and database concurrency (EDGE-076) are the most critical for production readiness.
**MEDIUM:** 8 — affect operational resilience, security, and scalability.
**LOW:** 6 — cleanup items that improve code quality and documentation.

### Next Steps

- [Chapter 7: Policy Engine Edge Cases](./07-policy-engine.md) — covers retry budget exhaustion, fail-closed gate, and decision logging.
- [Chapter 8: Idempotency & Recovery Edge Cases](./08-idempotency-recovery.md) — covers SQLite contention, race conditions, and exactly-once guarantees.
