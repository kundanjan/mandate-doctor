# Mandate Doctor — 4:30 Video Script (Razorpay Buildathon Track 03)

**Goal:** Prove *intelligent bounded recovery for failed UPI AutoPay / e-NACH* — not a dashboard. Every claim traceable to code/data.

**Setup:** 1920x1080, font 14, terminal + VS Code + Chrome. Start server: `source .venv/bin/activate && python -m uvicorn mandate_doctor.api.app:app --host 0.0.0.0 --port 8000`. Keep `http://localhost:8000` open. No cuts.

---

### 0:00-0:25 Problem & Evidence (buildathon-evidence.md)
**Show:** Mint Oct 10 2025 + Feb 20 2026 headlines, AMFI SIP 50L discontinued/month, NPCI 1+3 cap 1 Aug 2025.
**Say:** "India runs 1B AutoPay debits/month. SBI 36% approved, Airtel 10%. NPCI now caps recovery to 1 attempt + 3 retries per cycle. Most successes used to be attempt 5-9. Blind T+1/T+2/T+3 no longer works. Razorpay's own retry recovers only 8% more. Mandate Doctor replaces fixed retries with gated intelligence."

### 0:25-0:55 Data Provenance — Separate Evidence Classes
**Screen:** `data/README.md` + `data/npci-autopay-execution-2026-07.csv` head
**Commands:**
```bash
cat data/npci-autopay-execution-2026-07.csv | head -3
# Source: GET /api/ecosystem-statistics/.../Jul 2026 Retrieved 2026-08-23
sqlite3 data/training_data.db "SELECT count(*) FROM outcomes WHERE design_version=2 AND error IS NULL AND assigned_click IS NOT NULL"
# 140 frozen clean (DESIGN_VERSION=2)
cat models/recovery_model.json | grep -E 'rows|base_rate|cv_auc|provenance' | head -6
```
**Say:** "Three classes, never mixed: NPCI remitter-bank aggregate is frozen calibration only. Synthetic observed fixtures are inputs. Potential outcomes are evaluator-only hidden labels. Labels in DB are API-verified Razorpay test-mode `failed_payment_id + error_code + recovered`, retry_prior is NPCI Jul-2026 bank approval rate. No invented probabilities."

### 0:55-1:25 Architecture (docs/architecture.pdf 6 diagrams)
**Show:** Mermaid flow `Razorpay → Event Ingest → Context → Estimator → Agent → Safety Gate → Executor → Outcome Observer → State → Audit → Dashboard`
**Say:** "Reasoning and safety are separate. LLM is advisory only — never creates an executable action. Estimator scores P(recovery) per permitted action. Deterministic gate enforces rail/cycle limits, mandate status, idempotency, evidence, high-value hold. Executor only runs after gate approval."

### 1:25-2:10 Safety Gate + Classifier + Policy (live code)
**Open:** `src/mandate_doctor/core/policy.py:23` (`MAX_RETRIES_AFTER_ORIGINAL=3`, `RetryBudget` keyed `(mandate_id,cycle_id)`), `src/mandate_doctor/core/classifier.py:44` (deterministic → pattern → AMBIGUOUS 0.4), `src/mandate_doctor/api/app.py:171` (_idempotent_recovery guardrail chain), `src/mandate_doctor/core/models.py:32` (Action enum).
**Say:** "Classifier abstains on `payment_declined` with no evidence. Policy maps LOW_BALANCE→SCHEDULE_RETRY, TECHNICAL→RETRY_IMMEDIATELY, STOP→TRIGGER_RECONSENT, AMBIGUOUS→HOLD. Budget is cycle-scoped, transactional, exactly 3 retries after original. Gate checks revoked/expired/closed/fraud, window, amount, allow-list, evidence, idempotency — no model bypass."
**Prove:** `pytest tests/unit/test_policy.py tests/unit/test_classifier.py -q` → 63 passed.

### 2:10-3:00 Live Recovery Pipeline (this is the AutoPay flow)
**Browser:** `http://localhost:8000` Workflow tab. Show 11 React Flow nodes. Trigger:
```bash
curl http://localhost:8000/health
curl -X POST http://localhost:8000/api/batch/start -H "Content-Type: application/json" -d '{"n":6,"workers":1}'
```
Watch edges animate 01→08, logs `order → link → bot → mock → poll → store`, ticker `done/recovered`. Switch to Dashboard tab → 5 Chart.js charts + idempotency card. Then show webhook verification:
```bash
curl -X POST http://localhost:8000/api/webhooks/razorpay -H "X-Razorpay-Signature: invalid" -d '{"event":"payment.failed"}' # 400
curl -s http://localhost:8000/api/stats | jq .model | head -8
```
**Say:** "Webhook is fail-closed HMAC-SHA256, deduped raw-event store, normalized, idempotent claim/decide/execute. Salted retry: T+1 off-peak if score≥0.50, T+3 salary window 1-5 if 0.30-0.50, else hold. That's the intelligent T+1/T+2/T+3 — you see live via WebSocket /ws."

### 3:00-3:40 ML + Explainability
**Show:** `eval/model_comparison.py` 43 models (LDA/PCA→LDA vs LogReg, KNN, Tree, SVM, QDA), `models/tuning_results.json` Extra Trees 100 depth8 sqrt 0.835 AUC top, `models/recovery_model.json: top_features` (regime_optimistic 0.39, retry_prior 0.21). Live:
```bash
curl -s -X POST http://localhost:8000/api/model/predict -H "Content-Type: application/json" -d '{"npci_bank":"Canara Bank","error_class":"bd","amount_paise":19900,"regime":"optimistic","retry_prior":0.27}' | jq
# 0.899 → RECOVERY_LINK
curl -s -X POST http://localhost:8000/api/model/explain -H "Content-Type: application/json" -d '{"npci_bank":"HDFC Bank","error_class":"td","amount_paise":49900,"regime":"pessimistic","retry_prior":0.06}' | jq .top_contributions
```
**Say:** "GradientBoosting + LogReg ensemble, 5-fold stratified, no leakage — `recovered` dropped, one-hot + scaled inside pipeline, temporal features from created_at. SHAP TreeExplainer gives per-decision bars. Production currently link-based for labels; mandate debit `POST /v1/mandates/:id/debit` is gated behind same 24h notification + budget."

### 3:40-4:10 Evaluation — Controlled Policy Comparison
**Show:** `eval/harness.py` + `eval/outcome_environment.py` diagram `Generator → Potential Outcomes → Control (fixed T+1→T+2→T+3→halted) vs Treatment (context-aware) → Metrics` . Run:
```bash
sqlite3 data/training_data.db "SELECT regime, count(*), printf('%.0f%%',100*avg(recovered)) FROM outcomes WHERE design_version=2 GROUP BY regime"
# optimistic 57% base 34% pessimistic 12% — learnable
cat plan.md | grep -A2 "Primary metric"
# cycle recovery rate = recovered eligible cycles / eligible failed cycles; report lift, CI, hard-stop 0, budget 0
```
**Say:** "Same seed, same scenarios, same outcomes for both policies. Fixed denominator, train/dev/holdout seeds, 4 profiles (balanced, low_balance_heavy, stop_heavy, adversarial_generic), sensitivity over recovery probability, CI included. Losing results stay visible — no production uplift claimed without randomized merchant experiment."

### 4:10-4:30 Audit & Close
**Show:** `git log --oneline -3`, `ruff check` clean, `mypy --strict` clean (except sklearn stubs), `.env` gitignored, `ls models/history/recovery_model_140_frozen*` (FROZEN guard), `ls -lh docs/architecture.pdf` (6 diagrams via Playwright).
**Say:** "Every decision has evidence refs, gate result, idempotency key, outcome. Dashboard keeps real test-mode and simulated evaluation visibly separate. Mandate Doctor: bounded, explainable, measured — saves mandates without retry spam."

**End card:** Repo URL, Track 03.
