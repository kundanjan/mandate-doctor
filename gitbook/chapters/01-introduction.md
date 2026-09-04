# Mandate Doctor — Production Edge Cases Gitbook

## What This Book Covers

This gitbook documents every production edge case, failure mode, and operational hazard identified across the entire Mandate Doctor codebase. Each chapter maps to a source module and lists:

- **Failure modes** — what can go wrong
- **Edge cases** — inputs/conditions that break assumptions
- **Mitigations** — what the code already does to handle it
- **Residual risks** — what remains unprotected

## System Summary

Mandate Doctor is an event-driven recovery agent for failed recurring UPI AutoPay/e-NACH payments. It runs in Razorpay test mode.

### Architecture at a Glance

```
Razorpay webhook → HMAC verify → classify failure → policy engine → idempotency gate → execute recovery
                                                                                          ↓
                                                                              checkout bot (Playwright)
                                                                                          ↓
                                                                              training data → ML model
```

### Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12+ |
| HTTP Framework | FastAPI + uvicorn |
| API Client | httpx (async) |
| Validation | Pydantic v2 |
| State Store | SQLite (WAL mode) |
| Browser Automation | Playwright (Chromium) |
| ML Training | Pure numpy (L2 logistic regression) |
| Dashboard | React Flow + Chart.js (CDN, no build) |
| Logging | structlog |
| Quality | ruff + mypy --strict |
| Tests | pytest (63 tests) |

### Key Design Constraints

1. **Test-mode only** — refuses to use production Razorpay keys
2. **NPCI OC-215A compliant** — max 1 attempt + 3 retries per mandate per cycle
3. **At-most-once execution** — SQLite-backed idempotency layer
4. **No invented probabilities** — all outcome data from real Razorpay API responses
5. **Fail-closed safety gate** — policy engine overrides all other layers

### Codebase Map

```
src/mandate_doctor/
├── config.py              # Environment variables, paths
├── core/
│   ├── models.py          # Domain dataclasses
│   ├── classifier.py      # 3-layer error classifier
│   ├── codes.py           # NPCI error code lookup table
│   ├── policy.py          # Retry budget + decision engine
│   └── idempotency.py     # At-most-once SQLite layer
├── services/
│   └── razorpay.py        # Razorpay API client with backoff
├── api/
│   ├── app.py             # FastAPI webhooks + batch + dashboard
│   ├── events.py          # WebSocket event bus
│   └── static/index.html  # React Flow dashboard
eval/
├── checkout_bot.py        # Playwright checkout automation
├── data_collector.py      # Two-phase outcome collector
├── train_model.py         # L2 logistic regression trainer
├── harness.py             # 3-arm evaluation harness
└── outcome_environment.py # Outcome simulation
```

## How to Read This Book

Each chapter follows the same structure:

1. **Module overview** — what it does
2. **Edge cases** — numbered list with severity (CRITICAL / HIGH / MEDIUM / LOW)
3. **Code references** — exact file:line for each issue
4. **Mitigations already in place**
5. **Recommended fixes** for each residual risk

Severity levels:
- **CRITICAL** — data loss, security breach, or silent incorrect behavior in production
- **HIGH** — service degradation, missed recoveries, or incorrect decisions
- **MEDIUM** — operational friction, reduced accuracy, or resource leaks
- **LOW** — code quality, maintainability, or minor performance issues
