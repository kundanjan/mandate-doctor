# Mandate Doctor — Demo Guide

**Razorpay AI Buildathon · Track 03: AI Revenue Recovery**

Mandate Doctor detects failed UPI AutoPay mandates, classifies the failure cause,
scores recovery probability with a ML model, executes a bounded recovery action,
and measures money recovered — with a full audit trail and zero retry-budget violations.

---

## Prerequisites

- Python 3.11+
- A Razorpay **test-mode** account — [dashboard.razorpay.com](https://dashboard.razorpay.com)
- (Optional) An OpenAI-compatible API key for the LLM classifier layer

---

## 1. Install

```bash
git clone <repo-url> && cd mandate-doctor
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[eval,dashboard,dev]"
playwright install chromium
cp .env.example .env
# Edit .env — fill in:
#   RAZORPAY_KEY_ID     = rzp_test_...
#   RAZORPAY_KEY_SECRET = ...
#   RAZORPAY_WEBHOOK_SECRET = ...
```

---

## 2. Start the Recovery Pipeline

```bash
uvicorn mandate_doctor.api.app:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** — you'll see the React Flow pipeline dashboard with 11 nodes
(NPCI Calibration → Scenario Designer → Debit Attempt → Recovery Link → Checkout Bot →
Mock Bank → Outcome Poller → SQLite Store → Webhook → Event Bus → Batch Stats).

---

## 3. Run a Recovery Batch

In a second terminal:

```bash
curl -X POST http://localhost:8000/api/batch/start \
  -H "Content-Type: application/json" \
  -d '{"n": 6, "workers": 1}'
```

Watch the graph: nodes animate green/orange as the Playwright bot drives each scenario
through the real Razorpay hosted checkout. The status bar at the bottom shows
`done / recovered / errors` in real time via WebSocket.

Switch to the **Dashboard** tab to see recovery rate, regime breakdown, and per-bank charts.

---

## 4. Verify Webhook Signature Enforcement

```bash
# Invalid signature → 400
curl -X POST http://localhost:8000/api/webhooks/razorpay \
  -H "X-Razorpay-Signature: invalid" \
  -d '{"event":"payment.failed"}' -i

# Missing signature → 400
curl -X POST http://localhost:8000/api/webhooks/razorpay \
  -d '{"event":"payment.failed"}' -i
```

---

## 5. Prove the Audit Trail (Exactly-Once Idempotency)

Fire 10 identical `payment.failed` webhooks concurrently:

```bash
curl -X POST http://localhost:8000/api/demo/duplicate-webhooks | jq .
```

Expected output:
```json
{
  "webhooks_fired": 10,
  "executed": 1,
  "deduplicated": 9,
  "verdict": "exactly-once holds"
}
```

This proves the SQLite WAL atomic claim → decide → execute chain. No matter how many
duplicate webhook deliveries arrive, exactly one recovery action is taken.

---

## 6. ML Scoring — Score a Failed Payment

```bash
curl -X POST http://localhost:8000/api/model/predict \
  -H "Content-Type: application/json" \
  -d '{
    "npci_bank": "Canara Bank",
    "error_class": "bd",
    "amount_paise": 19900,
    "regime": "optimistic",
    "retry_prior": 0.27
  }' | jq .
```

Returns:
```json
{
  "score": 0.899,
  "decision": "RECOVERY_LINK",
  "model_available": true,
  "model_metrics": { "cv_accuracy": 0.72, "cv_auc": 0.835, "rows": 140 }
}
```

---

## 7. SHAP Explainability — Why Did the Model Decide That?

```bash
curl -X POST http://localhost:8000/api/model/explain \
  -H "Content-Type: application/json" \
  -d '{
    "npci_bank": "HDFC Bank",
    "error_class": "td",
    "amount_paise": 49900,
    "regime": "pessimistic",
    "retry_prior": 0.06
  }' | jq .top_contributions
```

Returns per-feature SHAP values showing which signals pushed toward or away from recovery.

---

## 8. Check Model Status

```bash
curl -s http://localhost:8000/api/stats | jq .model
```

---

## 9. Run the Unit Tests

```bash
pytest tests/unit/ -q
# 63 passed
```

---

## 10. Run the 3-Arm Evaluation Harness

Compares natural (do-nothing) vs control (fixed T+1/T+2/T+3) vs treatment (Mandate Doctor)
on the same frozen scenario set. Reports bootstrap 95% CI — if it includes zero, the result
is reported as INCONCLUSIVE.

```bash
python -m eval.harness
```

Output (4 profiles):

```
PROFILE: balanced
  Control  recovery rate: 38.2%
  Treatment recovery rate: 41.6%  (+3.4pp)
  Treatment incremental lift: 15.4% [12.1%, 18.8%]

PROFILE: stop_heavy
  Control hard-stop violations: 275   (blindly retries revoked mandates)
  Treatment hard-stop violations: 0   (classifies as STOP → reconsent)
  Treatment wins: +8.2pp recovery, zero violations

PROFILE: adversarial_generic
  Agent correctly abstains — all bare payment_declined, no evidence to act on
```

---

## 11. Open the Policy-Comparison Dashboard

```bash
streamlit run dashboard/app.py
```

Shows recovery rate, absolute and relative lift, bootstrap CI, hard-stop violations,
attempts per recovery, and a full limitations section. Real test-mode and simulated
evaluation data are always kept visually separate.

---

## API Reference (Quick)

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | React live pipeline dashboard |
| `/ws` | WS | Real-time event stream (WebSocket) |
| `/health` | GET | Health check |
| `/api/webhooks/razorpay` | POST | Razorpay webhook intake (HMAC-verified) |
| `/api/batch/start` | POST | Start a recovery batch |
| `/api/batch/stop` | POST | Gracefully stop a running batch |
| `/api/batch/status` | GET | Current batch state |
| `/api/model/predict` | POST | ML recovery score for a payment |
| `/api/model/explain` | POST | SHAP feature contributions |
| `/api/model/train` | POST | Trigger incremental model retraining |
| `/api/model/status` | GET | Trained-at timestamp + CV metrics |
| `/api/stats` | GET | Aggregated outcomes + model metrics |
| `/api/demo/duplicate-webhooks` | POST | Idempotency proof (10 webhooks → 1 execution) |
| `/api/idempotency/stats` | GET | Claim / executed / deduplicated counts |

---

## Regulatory Compliance

- **NPCI OC-215A/2025-26:** Max 1 original attempt + 3 retries per mandate per cycle.
  The retry budget is cycle-scoped, SQLite-backed, and enforced before any action.
- **Terminal mandate guard:** Revoked, expired, cancelled, fraud-blocked mandates
  are classified as STOP and routed to re-consent, never retried.
- **Amount ceiling:** Payments ≥ ₹500 are always escalated to human review.
- **Rate limit:** Max 1 automated recovery attempt per customer per 48 hours.
