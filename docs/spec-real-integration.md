# Spec: Real Razorpay Integration & Learned Recovery Model

## Problem

Current evaluation uses 24+ hardcoded probability values. Every evaluation result is circular because assumed probabilities determine outcomes. A judge asking "where did 0.80 come from?" exposes the system as fabricated.

## Goal

Replace all hardcoded assumptions with values learned from observed outcomes on Razorpay test-mode APIs. Prove recovery-rate improvement with measured data, not invented numbers.

## Constraints

- Razorpay TEST MODE only — no real money
- Test cards produce deterministic success/failure, not probabilistic outcomes
- We control WHICH scenarios we create, so we can vary inputs systematically
- The model learns from OBSERVED outcomes of our own recovery attempts
- Timeline: ~10 days until Sep 5 deadline

## What We Need From You

| Item | Where to get it | Why |
|---|---|---|
| Razorpay Key ID (`rzp_test_...`) | Dashboard → Settings → API Keys → Generate Test Key | API authentication |
| Razorpay Key Secret | Same page, shown once | API authentication |
| Razorpay Webhook Secret | Dashboard → Settings → Webhooks → Create webhook | Signature verification |
| ngrok installed + running | `snap install ngrok` then `ngrok http 8000` | So Razorpay can deliver webhooks to localhost |

## Architecture

```
Phase 1: INGEST (real)
  Create subscriptions via Razorpay Subscriptions API (test mode)
  Trigger failures using error-scenario test cards
  Receive payment.failed / subscription.pending webhooks
  Verify X-Razorpay-Signature on every event

Phase 2: COLLECT (real)
  For each failure, record:
    - Features: error_code, amount_paise, bank, attempt_count,
                payment_method, days_since_failure
    - Action taken: retry | payment_link | hold
    - Outcome: recovered (yes/no) + amount + day
  Store in SQLite: features → action → outcome triples

Phase 3: LEARN (from collected data)
  Train logistic regression or gradient-boosted classifier:
    Input:  features vector
    Output: P(recovery) ∈ [0, 1]
  Model must achieve measurable accuracy on held-out test set.
  Report precision, recall, F1, AUC.

Phase 4: DECIDE (model-driven)
  New failure arrives → model predicts P(recovery)
  Policy engine applies NPCI constraints (budget, windows)
  If P(recovery) × amount > action_cost → execute
  Else → hold for review

Phase 5: PROVE
  A/B comparison on held-out batch:
    Control: fixed T+1/T+2/T+3 blind retry
    Treatment: model-driven policy
  Measure: recovery rate delta, cost per recovery, violations
  Report with confidence intervals on MEASURED results
```

## What Gets Eliminated

| Current assumption | Replaced by |
|---|---|
| P(retry success) = 0.80 for technical | Learned from observed retry outcomes |
| Retry decay = 0.85^distance | Learned from timing-vs-outcome patterns |
| Natural recovery rate = 65% technical | Measured from cases with no intervention |
| Payment link success = 0.30–0.50 | Observed from actual payment_link.paid events |
| Optimal retry day = 2–6 | Derived from outcome distribution across days |
| BD sub-composition = 55/30/15 | Not needed — model learns from features directly |
| Confidence scores 0.75–0.98 | Model-predicted confidence from trained classifier |

## Slices (in order)

### Slice 1: Razorpay client + webhook receiver
- FastAPI endpoint receiving Razorpay webhooks
- HMAC-SHA256 signature verification (raw body, timing-safe compare)
- Subscription creation via Subscriptions API
- Error-scenario test card integration
- Acceptance: real webhook received and verified end-to-end

### Slice 2: Data collection pipeline
- SQLite schema for features/actions/outcomes
- Recovery executor: creates Payment Links via API
- Outcome observer: correlates payment_link.paid back to original case
- Batch runner: generates N test scenarios, executes recoveries
- Acceptance: ≥50 labeled outcome records collected

### Slice 3: Model training
- Feature engineering from collected data
- Logistic regression baseline
- Gradient-boosted trees if data volume supports it
- Train/validation/test split (70/15/15)
- Acceptance: model beats random-guess baseline on held-out test set

### Slice 4: Model-driven policy
- Replace hardcoded scheduling with model predictions
- Decision rule: act if P(recovery) × amount > action_cost
- Integrate with existing RetryBudget and safety constraints
- Acceptance: all existing tests pass + new model-driven tests pass

### Slice 5: Honest evaluation
- Control vs treatment comparison using REAL collected outcomes
- Bootstrap CIs on measured lift
- Sensitivity analysis: report which conclusions are robust
- Acceptance: report shows measured improvement OR honestly reports no improvement

## Non-Goals

- Production deployment
- Live-mode (real money) integration
- Multi-merchant support
- Real customer data handling
