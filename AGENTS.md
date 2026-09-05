# AGENTS.md — Mandate Doctor

## Stack & Layout
- Python 3.11+ (pyproject `requires-python >=3.11`, system `python3` is 3.9 — use `python3.11`/`python3.12`).
- Hatchling build, FastAPI + Uvicorn + Pydantic, SQLite WAL, structlog, httpx, Playwright.
- `src/mandate_doctor/` — app code; `eval/` — top-level package included in wheel (`tool.hatch.build.targets.wheel.packages = ["src/mandate_doctor","eval"]`), not under `src/`; `models/` — frozen GBM pipeline; `data/` — frozen NPCI CSV + SQLite DBs (`*.db` is gitignored); `tests/unit/` + `tests/integration/`; `scripts/serve.sh` — detached stack.
- No CI workflows, no `opencode.json`/`AGENTS.md` pre-exists. Single `main` branch.

## Setup
```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[eval,dashboard,dev]"   # eval=sklearn/joblib/pandas/shap/playwright, dashboard=streamlit/plotly, dev=pytest/ruff/mypy
playwright install chromium              # required for eval/checkout_bot.py
cp .env.example .env                     # then fill RAZORPAY_KEY_ID (must start rzp_test_), RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET
```
- `src/mandate_doctor/config.py:40-44` loads `.env` via `pydantic-settings` (`extra="ignore"`), `PROJECT_ROOT = Path(__file__).parents[2]`. `src/mandate_doctor/services/razorpay.py:46-47` rejects non-`rzp_test_` keys.
- `DATABASE_URL` defaults to `sqlite:///./mandate_doctor.db`; runtime code uses `settings.project_root / "data"/"idempotency.db"` and `"data/training_data.db"` directly.

## Run & Verify
```bash
uvicorn mandate_doctor.api.app:app --host 0.0.0.0 --port 8000  # dashboard at http://localhost:8000, health at /health, WS at /ws
# detached (kills prior uvicorn/ngrok, survives terminal close, logs /tmp/md-*.log):
bash scripts/serve.sh

# single verification steps
pytest tests/unit -q                 # 63 tests (classifier + policy); see also pytest tests/ -v
pytest tests/unit/test_policy.py -v  # single file; -k <name> for single test
ruff check . && ruff format --check .
mypy src/                            # strict=true but tool.mypy.python_version=3.12 ≠ pyproject py311
python -m eval.harness               # 3-arm (natural/control/treatment) + bootstrap 95% CI across 4 profiles; no creds needed
python -m eval.generate_batch        # synthetic NPCI-calibrated batch (needs data/npci-*.csv)
streamlit run dashboard/app.py       # policy-comparison dashboard (file is gitignored)
curl -X POST http://localhost:8000/api/batch/start -H "Content-Type: application/json" -d '{"n":6,"workers":1}'
curl -X POST http://localhost:8000/api/demo/duplicate-webhooks | jq .  # exactly-once proof: 10 → 1 executed
```
- `eval/data_collector.py:run_batch` and `eval/checkout_bot.py` require live Razorpay test keys **and** a running API server (polls `http://localhost:8000/api/bounce/{ref}` and `https://api.razorpay.com/v1`). Do not run in CI without creds.
- `app.py:686-705` incremental retrainer: 90s warm-up then every 15 min; guarded by `asyncio.Lock` and `models/FROZEN` (see below).

## Architecture — Entry Points & Wiring
- **API**: `src/mandate_doctor/api/app.py:38` — FastAPI app; webhook `POST /api/webhooks/razorpay` (`_verify_signature` HMAC-SHA256, fail-closed 503/400), batch control, ML endpoints (`/api/model/predict|explain|train|status`), stats, WS event bus. `src/mandate_doctor/api/events.py` is the in-process pub/sub consumed by the React dashboard (`src/mandate_doctor/api/static/index.html`).
- **Core domain**: `src/mandate_doctor/core/models.py` — `Mandate`, `DebitAttempt` (`cycle_id` scopes the NPCI budget), `Decision`, `FailureBucket` (LOW_BALANCE/TECHNICAL/STOP/AMBIGUOUS), `Action`. `core/codes.py` — deterministic error-code → bucket map. `core/classifier.py:46` — 3-layer `classify()` (deterministic lookup → description pattern → LLM via `services/llm.py:llm_classify`, else AMBIGUOUS). `core/policy.py:34-67` — `RetryBudget` (NPCI OC-215A: 1 attempt + 3 retries/cycle, only retries counted), global singleton `retry_budget`. `core/idempotency.py:34` — `IdempotencyRepository` (SQLite WAL, `PRAGMA busy_timeout=5000`, thread-local conn, `recovery_claims` + `executions` PRIMARY KEY on `idempotency_key`). `core/scorer.py:21` — `MLScorer` lazy-loads `models/recovery_pipeline.joblib` + `recovery_model.json`.
- **Services**: `src/mandate_doctor/services/razorpay.py:65-83` — `_with_retries` exponential backoff (6 attempts, retries on 429/5xx or "too many request"), `_auth()` enforcement.
- **Eval**: `eval/generate_batch.py:63` `SCENARIO_PROFILES` (balanced/low_balance_heavy/stop_heavy/technical_heavy/adversarial_generic), `eval/outcome_environment.py` hidden-label potential-outcome table, `eval/harness.py:151` `run_treatment()` calls real `classify()`+`RetryBudget`, `eval/train_model.py:299` `train_sklearn()` GBM (200 trees, depth 5) → `recovery_model.json` + `recovery_pipeline.joblib`.
- **Import quirk**: `eval` is not under `src/`; `src/mandate_doctor/api/app.py:31-32` inserts `settings.project_root` into `sys.path`. Running `python -m eval.harness` from repo root works; running from `src/` does not.

## Conventions & Gotchas
- **Retry budget is global mutable state** (`core/policy.py:67`). `tests/conftest.py:5` resets it `autouse` per test via `reset_budget()`. Any script/harness that reuses `RetryBudget` must call `reset_budget()` or pass a fresh `RetryBudget()` to `run_treatment(budget=...)` or results leak across cycles.
- **Idempotency is load-bearing** — `idempotency.db` `PRIMARY KEY(idempotency_key)` is the exactly-once guarantee. Never change the key format (`{payment_id}:RECOVER` in `app.py:190`) or the WAL/threading setup.
- **Guardrails in `app.py:175-303` `_idempotent_recovery`**: amount ≥ ₹500 (`500_00` paise, `app.py:204`) → ESCALATE; `TERMINAL_CODES` → ESCALATE; per-customer 48h rate limit (`RATE:{ref}` claim); salary window 1-5 + peak 10-13/17-22 IST gates ML thresholds (≥0.50 / ≥0.30). These are not in `core/policy.py`.
- **Model frozen guard**: `models/FROZEN` (empty file) makes `eval/train_model.py:389-390` `train_incremental()` return `{"status":"skipped"}`. Delete the file to re-enable periodic/background retraining. `models/history/` and `models/training_log.jsonl` are the audit trail.
- **`.gitignore` hides `*.db`, `training_data_v2.csv`, `dashboard/app.py`, `plan.md`, `docs/video_script.md`** — `data/training_data.db` and `data/idempotency.db` will not show in `git status`; `dashboard/app.py` appears untracked-deleted even when present.
- **Razorpay webhook must be HMAC-verified** (`app.py:59-75`); missing/invalid `X-Razorpay-Signature` → 400, unconfigured secret → 503. `bounce_evidence` dict (`app.py:54`) is the only bridge between webhook `payment.failed` and the collector's `failed_payment_id`.
- **Ruff line-length 100, `per-file-ignores = tests/*: N806`**; `mypy` `python_version = "3.12"` disagrees with `pyproject` `py311` — prefer runtime behavior over mypy version.
- **Amounts are paise (int)** throughout (`AMOUNTS_PAISE` grids in `eval/generate_batch.py:128` and `eval/data_collector.py:65`). Threshold checks use paise, UI displays `amount_paise/100`.

## Testing
- `tool.pytest.ini_options.testpaths = ["tests"]`, `asyncio_mode = "auto"`. Unit suite is `tests/unit/` (8 files: `test_classifier|policy|idempotency|events|harness|calibration|outcome_environment|train_model`). `tests/integration/` is empty placeholder.
- Deterministic seeds matter: batch generation and harness bootstrap use `seed=42`/`seed=7`; changing seeds changes the NPCI-sampled bank mix and CI bounds.
- SHAP explain (`/api/model/explain`) imports `shap` lazily and requires `models/recovery_pipeline.joblib` + the `eval` extra; it will 500 if the model is missing — use `/api/model/status` first.
