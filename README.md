# Mandate Doctor

**A bounded recovery system for failed UPI AutoPay and e-NACH recurring payments.**

**Razorpay AI Buildathon - Track 03: AI Revenue Recovery**

## Status

The end-to-end recovery system is implemented and running against Razorpay test mode.

| Component | Status |
|---|---|
| FastAPI webhook receiver — HMAC-SHA256, fail-closed | ✅ |
| React 18 live pipeline dashboard (React Flow + Chart.js, WebSocket) | ✅ |
| Idempotent recovery engine — SQLite WAL, exactly-once claim/decide/execute | ✅ |
| Deterministic safety gate — terminal codes, amount ceiling, rate limit, budget | ✅ |
| ML scorer — GradientBoosting pipeline trained on 140 API-verified outcomes | ✅ |
| SHAP explainability endpoint `/api/model/explain` | ✅ |
| Playwright checkout bot — real Razorpay test-mode netbanking automation | ✅ |
| 3-arm evaluation harness — natural / control / treatment + bootstrap 95% CI | ✅ |
| NPCI-calibrated batch generator — 4 scenario profiles | ✅ |
| Streamlit policy-comparison dashboard | ✅ |
| Periodic incremental model retraining (every 15 min, FROZEN guard) | ✅ |
| Unit tests — classifier + policy (63 tests) | ✅ |

The system does not claim production recovery uplift without a randomised merchant experiment.

## Problem

Recurring-payment failures have different possible remedies. A payment can fail because of insufficient funds, a transient bank or network issue, an expired payment method, a revoked mandate, a risk decision, or an unknown condition. A fixed retry schedule cannot reliably choose the correct response when the available error information is incomplete.

Razorpay's public subscription documentation describes a failure flow that moves a subscription to `pending`, retries on the following day, and eventually moves it to `halted`. It also documents payment-method update and manual-charge flows.

The buildathon asks Track 03 projects to detect revenue at risk, determine an intervention, execute a bounded recovery workflow, measure recovered money across a batch, apply stopping rules and keep an audit trail.

Sources:

- [Razorpay AI Buildathon](https://razorpay.com/buildathon/)
- [Razorpay Subscription Payment Retries](https://razorpay.com/docs/payments/subscriptions/payment-retries/)

## Problem Evidence

These sources establish that recurring-payment failures and recovery are material payment-operations problems. They do not prove that this prototype will improve Razorpay's production recovery rate.

| Evidence | Source |
|---|---|
| NPCI-sourced reporting found large differences in AutoPay approval rates: SBI 36.14% and Airtel Payments Bank 10.49% for the reported month | [Mint, Oct 2025](https://www.livemint.com/companies/start-ups/upi-autopay-failures-recurring-payments-india-11759999218161.html) |
| Recurring UPI payments reached approximately one billion per month according to reporting citing EY India | [Mint, Feb 2026](https://www.livemint.com/industry/banking/rbi-npci-upi-autopay-debits-complaints-mandates-recurring-payments-11771480657742.html) |
| Razorpay documents payment failures, pending/halted states and fixed retry behavior | [Razorpay documentation](https://razorpay.com/docs/payments/subscriptions/payment-retries/) |
| Razorpay documents error fields such as code, source, step and reason | [Razorpay error documentation](https://razorpay.com/docs/errors/payments/list/) |
| Public engineering case studies report that payment timing can affect subscription recovery | [Stripe Smart Retries](https://stripe.com/blog/how-we-built-it-smart-retries), [Dropbox payment ML](https://dropbox.tech/machine-learning/optimizing-payments-with-machine-learning) |
| NPCI publishes bank-level Business Decline, Technical Decline statistics for AutoPay mandate execution | [NPCI UPI AutoPay Ecosystem Statistics](https://www.npci.org.in/product/ecosystem-statistics/autopay) |

The public evidence supports testing a context-aware recovery policy. It does not justify claiming a guaranteed percentage increase.

## System Architecture & Mandate Ecosystem Flow

A 1-glance view of how **Mandate Doctor** intercepts payment failures and recovers revenue:

```mermaid
graph LR
    subgraph ECOSYSTEM ["Payment Flow"]
        Debit["Mandate Debit"] --> Gateway["Razorpay / NPCI"]
        Gateway -->|"Success (~30%)"| Paid["Revenue Recovered"]
        Gateway -->|"Failure (~70%)"| Doctor["Mandate Doctor Engine"]
    end

    subgraph SYSTEM ["Mandate Doctor (Our System)"]
        Doctor --> Gate["Safety Gate & ML Scorer<br/>Enforces NPCI 1+3 Cap"]
        Gate -->|"Low Balance / TD"| R1["Off-Peak Smart Retry"]
        Gate -->|"Auth Failure"| R2["Instant Payment Link"]
        Gate -->|"Mandate Revoked"| R3["Re-Consent Request"]
    end

    R1 -->|"Recovered Debit"| Paid
    R2 -->|"Customer Paid"| Paid

    style SYSTEM fill:#1e1e24,stroke:#6366f1,stroke-width:2px,color:#fff
    style ECOSYSTEM fill:#18181b,stroke:#3f3f46,stroke-width:1px,color:#fff
```

## Proposed System

Mandate Doctor does not guess the hidden bank reason behind a generic `payment_declined`. It retrieves available evidence, estimates which permitted recovery action is most useful, applies deterministic safety constraints, executes the action, and records the outcome.

```mermaid
flowchart TD
    R[Razorpay Test Mode] -->|payment.failed or subscription event| I[Event Ingest]
    I -->|verified normalized event| C[Context Aggregator]
    C -->|history, state, budget and cohort signals| E[Recovery Estimator]
    E -->|action scores and evidence references| A[Recovery Agent]
    A -->|structured plan| G[Deterministic Safety Gate]
    G -->|approved| X[Action Executor]
    G -->|uncertain or prohibited| H[Hold for Review]
    X -->|retry or payment link| R
    R -->|result event| O[Outcome Observer]
    O --> S[State Store]
    S --> C
    I --> L[Audit Log]
    C --> L
    A --> L
    G --> L
    X --> L
    O --> L
    L --> D[Dashboard and Evaluation]
```

## What The Recovery Agent Knows

The system can use only data that is actually available:

- Razorpay event type and event ID
- Error code, description, source, step, reason and metadata
- Mandate or subscription status
- Payment method, amount and timestamps
- Previous attempts and their observed outcomes
- Retry budget already consumed
- Merchant-level aggregate outcomes, when available

The system does not assume access to:

- A customer's current bank balance
- A customer's salary date
- A bank's private risk score
- A hidden bank-side decline reason when Razorpay does not return one

When evidence is insufficient, the correct result is abstention:

```json
{
  "selected_action": "hold_for_review",
  "abstain": true,
  "reason": "Available evidence cannot distinguish a retryable failure from a risk or mandate failure"
}
```

## AI And Agent Responsibilities

The system has separate reasoning and safety responsibilities.

### Recovery estimator

Estimates the probability that each permitted action will recover the payment. It does not assert the true cause of the original failure.

Possible implementations are:

- A transparent probability baseline for the first vertical slice
- A logistic or gradient-boosted model trained on a training split
- A ranking model for candidate retry windows
- A contextual-bandit policy after sufficient outcome data exists

### Recovery agent

The tool-using loop can:

- Retrieve additional read-only context
- Compare retry, payment-link and review options
- Select a candidate window returned by the estimator
- Explain a plan using references to observed fields
- Abstain when no action has sufficient evidence
- Observe and persist the resulting outcome

### Deterministic safety gate

The safety gate always runs before a write action. It enforces:

- Per-rail and per-cycle retry limits
- No retry for revoked, expired, closed or explicitly blocked mandates
- Idempotency keys for write actions
- Human approval for configured high-value actions
- Required evidence references

No model output can bypass the safety gate.

## Razorpay Integration Boundary

### Real test-mode path

The implementation will validate:

1. Test-mode subscription or payment creation
2. Real test-mode webhook delivery
3. `X-Razorpay-Signature` verification
4. Error-payload normalization
5. Permitted test-mode retry or payment-link calls
6. Correlation of the resulting event to the original action

### Test-mode limitation

If UPI test mode exposes only binary success/failure behavior, it cannot provide a trustworthy distribution of specific bank causes. Any enriched UPI cause used by the evaluation must be marked synthetic.

Documented card fixtures can provide additional error responses. Each fixture will record whether it came from a real Razorpay test response or from the evaluation environment.

## How The Result Is Verified

There are three different levels of verification.

### 1. Integration verification

This proves that the implementation works with Razorpay's test-mode contract. It does not prove business uplift.

### 2. Controlled policy evaluation

The evaluation environment creates scenarios with a hidden state and independent potential outcomes. The recovery system cannot read the hidden state.

```mermaid
flowchart LR
    S[Scenario Generator] --> P[Potential Outcomes]
    S --> C[Control: fixed retry policy]
    S --> T[Treatment: context-aware policy]
    P --> C
    P --> T
    C --> M[Comparison Metrics]
    T --> M
    M --> R[Reproducible Report]
```

The control and treatment receive the same scenarios and outcome table. The outcome environment is independent of the selected policy, so the treatment cannot receive favorable outcomes by construction.

### 3. Production experiment

Actual production uplift requires consented real merchant traffic and random assignment:

```text
eligible failed mandate cycles
  -> control or treatment assignment
  -> same eligibility and communication rules
  -> measure recovered cycle and amount
  -> monitor safety and customer-impact guardrails
```

The buildathon prototype will not claim this level of proof without such an experiment.

## Evaluation Protocol

The evaluation must follow these rules:

- Use a fixed eligible-cycle denominator
- Compare against the documented fixed retry policy, not an invented weak baseline
- Use the same scenarios, amounts and initial histories for both policies
- Keep hidden scenario labels inaccessible to the system
- Separate training, development and holdout seeds
- Run multiple random seeds
- Test balanced, technical-heavy, low-balance-heavy and hard-stop-heavy distributions
- Include generic declines with no description or useful context
- Run sensitivity analysis over assumed recovery probabilities
- Report confidence intervals
- Preserve every failed or inconclusive result

### Primary metric

```text
cycle recovery rate = recovered eligible mandate cycles / eligible failed mandate cycles
```

### Secondary metrics

| Metric | Definition |
|---|---|
| Recovered amount | Sum of confirmed recovered amounts |
| Absolute lift | Treatment rate minus control rate |
| Relative lift | Absolute lift divided by control rate |
| Attempts per recovered cycle | Total retry attempts divided by recovered cycles |
| Hard-stop violations | Automated actions on prohibited mandates; target zero |
| Budget violations | Actions beyond the configured cap; target zero |
| Abstention precision | Held cases that should not have been automated |
| Audit completeness | Plans with evidence, policy result, idempotency key and outcome |
| Time to resolution | Failure timestamp to recovered or final state |

No recovery target is hardcoded before measurement. If the confidence interval includes zero, the result is inconclusive. If treatment performs worse, that result remains in the report.

## Synthetic Data Contract

The batch generator reads a frozen NPCI calibration snapshot from `data/npci-autopay-execution-2026-07.csv`. It does not fetch live data at build time.

### Why these parameters

| Parameter | Value | Source | Retrieved |
|---|---|---|---|
| Weighted Approved % (top 50 remitter banks) | 22.95% | NPCI AutoPay Mandate Execution, Jul 2026 | 2026-08-23 |
| Weighted BD % | 76.15% | Same | Same |
| Weighted TD % | 0.90% | Same | Same |
| Total execution volume (top 50) | 2,481.40 Mn | Same | Same |

Source: [NPCI UPI AutoPay Ecosystem Statistics](https://www.npci.org.in/product/ecosystem-statistics/autopay), backed by the public JSON endpoint `GET /api/ecosystem-statistics/get-statistics?product_name=Autopay&tab_name=top50-remitter&type_name=execution&year=2026&month=Jul&page_no=1&sort_by=asc&size=50&locale=en`. Full provenance in [`data/README.md`](data/README.md).

### What is calibrated vs assumed

| Aspect | Calibrated from real data? | Detail |
|---|---|---|
| BD vs TD split per bank | Yes | Each bank's published Approved%, BD%, TD% drives the failure type |
| Bank selection probability | Yes | Proportional to actual execution volume |
| BD sub-composition (balance vs PIN vs closed) | No — evaluation assumption | NPCI publishes BD as one aggregate; sub-split is scenario-dependent |
| Recovery probability after retry | No — evaluation assumption | Not published by any public source |
| Regulatory constraints (retry cap, windows, limits) | Yes | OC-215A, RBI E-Mandate Framework 2026 |

Four evaluation profiles test policy robustness across different BD compositions:

| Profile | low_balance | ambiguous | stop_terminal | Purpose |
|---|---:|---:|---:|---|
| balanced | 55% | 30% | 15% | Baseline population estimate |
| low_balance_heavy | 75% | 15% | 10% | Consumer-subscription-heavy merchant |
| stop_heavy | 25% | 20% | 55% | High-churn or aged mandate book |
| adversarial_generic | 40% | 35% | 25% | All codes are generic `payment_declined` with empty description; tests abstention behavior |

The hidden ground truth label exists only inside the evaluation environment. It is never exposed to the recovery system.

Each evaluation scenario will contain:

```json
{
  "scenario_id": "sc_0001",
  "observed_event": {},
  "hidden_label_for_evaluation_only": "technical",
  "potential_outcomes": {},
  "source_basis": [
    "NPCI AutoPay Mandate Execution Jul 2026",
    "Razorpay error schema",
    "NPCI decline categories"
  ],
  "assumptions": [
    "technical failures have a non-zero recovery probability after delay"
  ]
}
```

The hidden label and potential outcomes are simulator assumptions. They are not real bank diagnoses or real merchant revenue.

## Repository Structure

```text
mandate-doctor/
├── src/mandate_doctor/
│   ├── core/
│   │   ├── models.py          — Pydantic domain models (Mandate, DebitAttempt, Decision)
│   │   ├── codes.py           — Razorpay error-code → failure-bucket lookup
│   │   ├── classifier.py      — 3-layer classifier (deterministic → pattern → LLM)
│   │   ├── policy.py          — NPCI OC-215A retry budget, cycle-scoped
│   │   ├── idempotency.py     — SQLite WAL exactly-once claim/decide/execute
│   │   └── scorer.py          — GradientBoosting ML scorer (joblib pipeline)
│   ├── services/
│   │   ├── razorpay.py        — Razorpay test-mode API client (orders, links, fetch)
│   │   └── llm.py             — OpenAI-compatible LLM client (advisory classifier)
│   └── api/
│       ├── app.py             — FastAPI: webhook, batch control, ML endpoints, WS
│       ├── events.py          — In-process WebSocket event bus
│       └── static/index.html  — React 18 live pipeline dashboard
├── eval/
│   ├── generate_batch.py      — NPCI-calibrated scenario generator (4 profiles)
│   ├── outcome_environment.py — Hidden-label potential-outcome table (evaluator only)
│   ├── harness.py             — 3-arm harness: natural / control / treatment + CI
│   ├── data_collector.py      — Real Razorpay test-mode data collection loop
│   ├── checkout_bot.py        — Playwright automation for hosted checkout
│   ├── train_model.py         — GradientBoosting pipeline trainer + incremental retraining
│   └── model_comparison.py    — 43-model benchmark (LDA, LogReg, KNN, SVM, GBM, ...)
├── dashboard/
│   └── app.py                 — Streamlit policy-comparison dashboard
├── models/
│   ├── recovery_pipeline.joblib — Fitted GradientBoosting pipeline (FROZEN)
│   ├── recovery_model.json      — Model metadata, metrics, feature importances
│   └── FROZEN                   — Guard: prevents accidental overwrite of tuned model
├── data/
│   ├── README.md              — Calibration source provenance
│   ├── training_data.db       — API-verified outcomes (140 clean rows, design_version=2)
│   └── npci-autopay-execution-2026-07.csv — Frozen NPCI Jul-2026 remitter snapshot
├── tests/
│   └── unit/                  — 63 tests: classifier + policy
├── docs/
│   ├── architecture.md
│   └── architecture.pdf
├── .env.example
├── pyproject.toml
└── README.md
```

## How to Run

### Prerequisites

- Python 3.11+
- A Razorpay test-mode account — [console.razorpay.com](https://dashboard.razorpay.com)
- (Optional) An OpenAI-compatible API key for the LLM classifier layer

### Install

```bash
git clone <repo-url> && cd mandate-doctor
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[eval,dashboard,dev]"
playwright install chromium          # for the checkout bot
cp .env.example .env                 # fill in RAZORPAY_KEY_ID, KEY_SECRET, WEBHOOK_SECRET
```

### Run the recovery pipeline + React dashboard

```bash
uvicorn mandate_doctor.api.app:app --host 0.0.0.0 --port 8000
# Open http://localhost:8000
```

### Trigger a recovery batch

```bash
curl -X POST http://localhost:8000/api/batch/start \
  -H "Content-Type: application/json" \
  -d '{"n": 6, "workers": 1}'
```

Watch the React Flow graph animate as the Playwright bot drives each scenario through the real Razorpay hosted checkout.

### Prove idempotency (audit trail)

```bash
# Fire 10 identical payment.failed webhooks concurrently
curl -X POST http://localhost:8000/api/demo/duplicate-webhooks | jq .
# → { "webhooks_fired": 10, "executed": 1, "deduplicated": 9, "verdict": "exactly-once holds" }
```

### Score a payment with the ML model

```bash
curl -X POST http://localhost:8000/api/model/predict \
  -H "Content-Type: application/json" \
  -d '{"npci_bank":"Canara Bank","error_class":"bd","amount_paise":19900,"regime":"optimistic","retry_prior":0.27}' \
  | jq .
```

### SHAP explainability

```bash
curl -X POST http://localhost:8000/api/model/explain \
  -H "Content-Type: application/json" \
  -d '{"npci_bank":"HDFC Bank","error_class":"td","amount_paise":49900,"regime":"pessimistic","retry_prior":0.06}' \
  | jq .top_contributions
```

### Run the evaluation harness (3-arm policy comparison)

```bash
python -m eval.harness
# Prints natural / control / treatment recovery rates + 95% CI for all 4 profiles
```

### Open the policy-comparison dashboard

```bash
streamlit run dashboard/app.py
```

### Run tests

```bash
pytest tests/ -v
```

## License

MIT
