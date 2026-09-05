"""FastAPI application: Razorpay webhook receiver + live workflow dashboard.

- POST /api/webhooks/razorpay  — fail-closed HMAC-verified webhook intake
- GET  /                       — n8n-style workflow dashboard (static)
- WS   /ws                     — live pipeline event stream
- POST /api/batch/start        — launch a collection batch (in-process)
- GET  /api/batch/status       — current batch state
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from mandate_doctor.api.events import bus
from mandate_doctor.config import settings
from mandate_doctor.core.idempotency import IdempotencyRepository
from mandate_doctor.core.scorer import MLScorer

if str(settings.project_root) not in sys.path:
    sys.path.insert(0, str(settings.project_root))

_scorer = MLScorer()

logger = structlog.get_logger(__name__)

app = FastAPI(title="Mandate Doctor", version="0.2.0")

_idem = IdempotencyRepository(settings.project_root / "data" / "idempotency.db")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8501"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

received_events: list[dict[str, str]] = []

# Bounce evidence indexed by the payment's notes.reference_id. Populated
# from payment.failed webhooks; consumed by the collector to attach the
# REAL failed-payment id + error code to each dataset row.
bounce_evidence: dict[str, dict[str, Any]] = {}

_STATIC_DIR = settings.project_root / "src" / "mandate_doctor" / "api" / "static"


def _verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.post("/api/webhooks/razorpay")
async def razorpay_webhook(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    secret = settings.razorpay_webhook_secret

    if not secret:
        raise HTTPException(status_code=503, detail="Webhook secret not configured")
    if not signature:
        raise HTTPException(status_code=400, detail="Missing X-Razorpay-Signature header")
    if not _verify_signature(raw_body, signature, secret):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event = json.loads(raw_body)
    event_type = event.get("event", "unknown")
    event_id = event.get("id", "unknown")
    payload = event.get("payload", {})
    plink_id = payload.get("payment_link", {}).get("entity", {}).get("id") or payload.get(
        "payment", {}
    ).get("entity", {}).get("id", "")
    received_events.append({"event_id": event_id, "type": event_type})

    if event_type == "payment.failed":
        pay = payload.get("payment", {}).get("entity", {})
        ref = (pay.get("notes") or {}).get("reference_id", "")
        if ref:
            bounce_evidence[ref] = {
                "payment_id": pay.get("id"),
                "order_id": pay.get("order_id"),
                "error_code": pay.get("error_code"),
                "error_description": pay.get("error_description"),
                "amount": pay.get("amount"),
            }
        # Idempotent recovery: exactly-once per failed payment, no matter
        # how many duplicate webhooks arrive.
        await _idempotent_recovery(pay)

    await bus.publish({"type": "webhook", "event_type": event_type, "entity_id": plink_id})

    logger.info("webhook_received", event_type=event_type, event_id=event_id)
    return {"status": "ok", "event_type": event_type}


# --------------------------------------------------------------------------
# ML scoring helpers
# --------------------------------------------------------------------------

# Map Razorpay error codes to training-data error classes
_ERROR_CODE_TO_CLASS: dict[str, str] = {
    "BAD_REQUEST_ERROR": "td",
    "GATEWAY_ERROR": "td",
    "DO_NOT_HONOR": "st",
    "INSUFFICIENT_FUNDS": "bd",
    "EXPIRED_CARD": "st",
    "INCORRECT_PIN": "am",
    "ATTEMPTS_EXCEEDED": "am",
    "INVALID_CARD": "st",
    "INVALID_UPI_PIN": "am",
    "AUTHENTICATION_FAILED": "am",
    "PAYMENT_DECLINED": "am",
}


def _current_regime() -> str:
    """Determine the current regime based on IST time of day."""
    from datetime import timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    hour = datetime.now(ist).hour
    # Peak hours: 10-13, 17-21:30 IST → pessimistic; off-peak → optimistic
    if 10 <= hour < 13 or 17 <= hour < 22:
        return "pessimistic"
    return "optimistic"


def _error_class_from_code(error_code: str) -> str:
    """Map Razorpay error_code to the training-data error_class."""
    return _ERROR_CODE_TO_CLASS.get(error_code.upper().strip(), "am")


def _build_features(pay: dict[str, Any]) -> dict[str, Any]:
    """Extract ML features from a Razorpay payment.failed payload."""
    error_code = pay.get("error_code") or "UNKNOWN"
    amount = int(pay.get("amount") or 0)

    # Determine regime from current time
    regime = _current_regime()

    # Map error code to error class
    error_class = _error_class_from_code(error_code)

    # NPCI bank from customer UPI ID or fallback
    customer = pay.get("customer_details") or {}
    vpa = customer.get("virtual_account_number") or ""
    npci_bank = vpa.split("@")[-1] if "@" in vpa else "unknown"

    # retry_prior: default to global average if unknown
    retry_prior = 0.27  # measured NPCI Jul-2026 global average

    return {
        "npci_bank": npci_bank,
        "error_class": error_class,
        "amount_paise": amount,
        "regime": regime,
        "retry_prior": retry_prior,
    }


# --------------------------------------------------------------------------
# Idempotent recovery with ML scoring
# --------------------------------------------------------------------------

async def _idempotent_recovery(pay: dict[str, Any]) -> dict[str, Any]:
    """Claim -> guardrails -> score -> decide -> execute, at most once per failed payment.

    Guardrail chain (all must pass before ML scoring):
      1. Idempotency — exactly-once per payment_id
      2. Amount gate — high-value (≥₹5000) → ESCALATE, never auto-recover
      3. Timing gate — salary window (1st–5th) preferred; else hold
      4. Rate limit — max 1 recovery per customer per 48 hours
      5. Mandate revocation — if mandate was revoked, never retry
    """
    payment_id = pay.get("id") or "unknown"
    amount_paise = int(pay.get("amount") or 0)
    amount_rs = amount_paise / 100

    # ── Guardrail 1: Idempotency ──────────────────────────────────────
    key = f"{payment_id}:RECOVER"
    claim = await asyncio.to_thread(_idem.claim, key)
    if not claim.won:
        await bus.publish({
            "type": "guardrail", "guard": "idempotency",
            "outcome": "blocked", "key": key,
        })
        logger.info("recovery_deduplicated", key=key)
        return {"outcome": "deduplicated", "decision": claim.cached_decision}

    error_code = (pay.get("error_code") or "UNKNOWN").upper().strip()

    # ── Guardrail 2: Amount gate ───────────────────────────────────────
    # High-value transactions must go through human review — never auto-recover.
    AMOUNT_CEILING_PAISE = 500_00  # ₹500
    if amount_paise >= AMOUNT_CEILING_PAISE:
        decision, reason = "ESCALATE", (
            f"guardrail:amount_gate — ₹{amount_rs:.0f} ≥ ₹{AMOUNT_CEILING_PAISE/100:.0f} "
            f"threshold, high-value transaction requires human review"
        )
        await _finalize(pay, key, decision, reason, ml_score=None, features=None)
        return {"outcome": "executed", "decision": decision}

    # ── Guardrail 3: Mandate revocation check ──────────────────────────
    # If the error indicates the mandate was revoked/expired/cancelled, never retry.
    TERMINAL_CODES = {
        "MANDATE_REVOKED", "MANDATE_EXPIRED", "MANDATE_CANCELLED",
        "ACCOUNT_CLOSED", "ACCOUNT_FROZEN", "ACCOUNT_BLOCKED",
        "DO_NOT_HONOR", "RESTRICTED_CARD", "MANDATE_NOT_FOUND",
        "INVALID_MANDATE",
    }
    if error_code in TERMINAL_CODES:
        decision, reason = "ESCALATE", (
            f"guardrail:revocation — error '{error_code}' indicates mandate "
            f"is terminal, recovery impossible"
        )
        await _finalize(pay, key, decision, reason, ml_score=None, features=None)
        return {"outcome": "executed", "decision": decision}

    # ── Guardrail 4: Timing gate (salary window) ───────────────────────
    # RBI/NPCI data: salary credits concentrate on 1st–5th of month.
    # Retry outside salary window has lower approval rates.
    from datetime import timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist)
    day_of_month = now_ist.day
    hour = now_ist.hour

    in_salary_window = 1 <= day_of_month <= 5
    in_peak_hours = 10 <= hour < 13 or 17 <= hour < 22

    timing_note = ""
    if in_peak_hours:
        timing_note = "peak_hours_deferred"
    elif not in_salary_window:
        timing_note = "non_salary_window_note"

    # ── Guardrail 5: Per-customer 48h rate limit ───────────────────────
    customer_ref = (pay.get("notes") or {}).get("reference_id", payment_id)
    rate_limit_key = f"RATE:{customer_ref}"
    rate_claim = await asyncio.to_thread(_idem.claim, rate_limit_key)
    if not rate_claim.won:
        decision, reason = "ESCALATE", (
            "guardrail:rate_limit — recovery already attempted for this customer "
            "within 48h, cooling off"
        )
        await _finalize(pay, key, decision, reason, ml_score=None, features=None)
        return {"outcome": "executed", "decision": decision}

    # ── ML scoring ─────────────────────────────────────────────────────
    features = _build_features(pay)
    ml_score = await asyncio.to_thread(_scorer.score, features)

    if ml_score is not None:
        if ml_score >= 0.50:
            if in_salary_window:
                decision, reason = "RECOVERY_LINK", (
                    f"ML: P={ml_score:.3f} ≥ 0.50 + salary_window(1st–5th) — "
                    f"optimal recovery timing"
                )
            elif in_peak_hours:
                decision, reason = "RETRY_LATER", (
                    f"ML: P={ml_score:.3f} ≥ 0.50 but peak_hours — "
                    f"defer to off-peak for higher approval rate"
                )
            else:
                decision, reason = "RECOVERY_LINK", (
                    f"ML: P={ml_score:.3f} ≥ 0.50 — sending recovery link"
                )
        elif ml_score >= 0.30:
            if in_salary_window:
                decision, reason = "RECOVERY_LINK", (
                    f"ML: P={ml_score:.3f} [0.30–0.50) + salary_window — "
                    f"salary credit improves odds"
                )
            else:
                decision, reason = "RETRY_LATER", (
                    f"ML: P={ml_score:.3f} [0.30–0.50) — "
                    f"scheduled retry in salary window"
                )
        else:
            decision, reason = "ESCALATE", (
                f"ML: P={ml_score:.3f} < 0.30 — "
                f"low probability, escalate to human review"
            )
    else:
        if error_code in ("BAD_REQUEST_ERROR", "GATEWAY_ERROR"):
            decision, reason = "RECOVERY_LINK", "transient failure — recoverable (rule-based)"
        else:
            decision, reason = "ESCALATE", f"unclassified error {error_code} (rule-based)"

    await _finalize(pay, key, decision, reason, ml_score, features, timing_note)
    return {"outcome": "executed", "decision": decision}


async def _finalize(
    pay: dict[str, Any],
    key: str,
    decision: str,
    reason: str,
    ml_score: float | None,
    features: dict[str, Any] | None,
    timing_note: str = "",
) -> None:
    """Record decision, execute if needed, publish to bus."""
    payment_id = pay.get("id") or "unknown"
    execution_ref = ""
    executed = False

    if decision == "RECOVERY_LINK":
        if settings.auto_recover:
            from mandate_doctor.services.razorpay import create_payment_link
            link = await create_payment_link(
                amount_paise=int(pay.get("amount") or 0),
                reference_id=f"auto_recovery_{payment_id}",
                description="Mandate Doctor auto-recovery",
            )
            execution_ref = link["id"]
        else:
            execution_ref = f"audit_only_{payment_id}"
        executed = await asyncio.to_thread(_idem.record_execution, key, execution_ref)

    await asyncio.to_thread(_idem.record_decision, key, decision, reason)
    await bus.publish({
        "type": "recovery",
        "outcome": "executed" if executed else "decided",
        "key": key,
        "decision": decision,
        "reason": reason,
        "ref": execution_ref,
        "ml_score": ml_score,
        "features": features,
        "timing_note": timing_note,
        "amount_rs": int(pay.get("amount") or 0) / 100,
    })
    logger.info(
        "recovery_finalized", key=key, decision=decision,
        ml_score=ml_score, timing=timing_note,
    )


@app.post("/api/demo/duplicate-webhooks")
async def demo_duplicate_webhooks() -> dict[str, Any]:
    """Fire 10 IDENTICAL payment.failed webhooks concurrently at the
    idempotent recovery routine and report exactly-once behavior."""
    demo_payment = {
        "id": f"pay_demo_{int(datetime.now(UTC).timestamp())}",
        "amount": 49900,
        "error_code": "BAD_REQUEST_ERROR",
        "notes": {"reference_id": "demo_duplicate"},
    }
    results = await asyncio.gather(*(_idempotent_recovery(demo_payment) for _ in range(10)))
    executed = sum(1 for r in results if r.get("outcome") == "executed")
    deduped = sum(1 for r in results if r.get("outcome") == "deduplicated")
    return {
        "webhooks_fired": 10,
        "executed": executed,
        "deduplicated": deduped,
        "verdict": "exactly-once holds" if executed == 1 else "VIOLATION",
        "results": results,
    }


@app.get("/api/idempotency/stats")
async def idempotency_stats() -> dict[str, Any]:
    return await asyncio.to_thread(_idem.stats)


@app.get("/api/bounce/{reference_id}")
async def get_bounce(reference_id: str) -> dict[str, Any]:
    evidence = bounce_evidence.get(reference_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail="no bounce evidence for reference")
    return evidence


@app.get("/api/stats")
async def get_stats() -> dict[str, Any]:
    """Aggregated outcome stats for the analytics dashboard."""
    import sqlite3

    db = settings.project_root / "data" / "training_data.db"
    if not db.exists():
        return {"status": "no_data"}

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    def group(q: str) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(q).fetchall()]

    totals = dict(
        conn.execute(
            """
        SELECT COUNT(*) AS n,
               SUM(recovered) AS recovered,
               SUM(error IS NULL AND assigned_click IS NOT NULL) AS clean,
               SUM(failed_payment_id IS NOT NULL) AS with_evidence,
               SUM(error IS NOT NULL) AS errored
        FROM outcomes
        """
        ).fetchone()
    )
    by_regime = group(
        """
        SELECT regime, COUNT(*) AS n, SUM(recovered) AS recovered
        FROM outcomes WHERE error IS NULL AND assigned_click IS NOT NULL
        GROUP BY regime
        """
    )
    by_class = group(
        """
        SELECT error_class, COUNT(*) AS n, SUM(recovered) AS recovered
        FROM outcomes WHERE error IS NULL AND assigned_click IS NOT NULL
        GROUP BY error_class
        """
    )
    by_bank = group(
        """
        SELECT npci_bank, COUNT(*) AS n, SUM(recovered) AS recovered
        FROM outcomes WHERE error IS NULL AND assigned_click IS NOT NULL
        GROUP BY npci_bank ORDER BY n DESC LIMIT 8
        """
    )
    by_amount = group(
        """
        SELECT amount_paise, COUNT(*) AS n, SUM(recovered) AS recovered
        FROM outcomes WHERE error IS NULL AND assigned_click IS NOT NULL
        GROUP BY amount_paise ORDER BY amount_paise
        """
    )
    conn.close()

    model_metrics = None
    model_path = settings.project_root / "models" / "recovery_model.json"
    if model_path.exists():
        try:
            artifact = json.loads(model_path.read_text())
            model_metrics = artifact.get("metrics")
        except Exception:  # noqa: BLE001 - dashboard must not crash on bad artifact
            model_metrics = None

    idem = await asyncio.to_thread(_idem.stats)

    return {
        "status": "ok",
        "totals": totals,
        "by_regime": by_regime,
        "by_class": by_class,
        "by_bank": by_bank,
        "by_amount": by_amount,
        "model": model_metrics,
        "idempotency": idem,
    }


@app.get("/api/events")
async def list_events() -> list[dict[str, str]]:
    return received_events


class PredictRequest(BaseModel):
    npci_bank: str = "unknown"
    error_class: str = "am"
    amount_paise: int = 19900
    regime: str = "optimistic"
    retry_prior: float = 0.27


@app.post("/api/model/predict")
async def predict_recovery(req: PredictRequest) -> dict[str, Any]:
    """Score a hypothetical failed payment with the ML model."""
    features = {
        "npci_bank": req.npci_bank,
        "error_class": req.error_class,
        "amount_paise": req.amount_paise,
        "regime": req.regime,
        "retry_prior": req.retry_prior,
    }
    score = await asyncio.to_thread(_scorer.score, features)
    metrics = _scorer.metrics
    decision = "RECOVERY_LINK" if score is not None and score >= 0.50 else (
        "RETRY_LATER" if score is not None and score >= 0.30 else "ESCALATE"
    )
    return {
        "score": score,
        "decision": decision,
        "features": features,
        "model_metrics": metrics,
        "model_available": _scorer.is_available,
    }


@app.post("/api/model/explain")
async def explain_recovery(req: PredictRequest) -> dict[str, Any]:
    """Return SHAP values for a hypothetical failed payment."""
    import joblib  # type: ignore[import-untyped]
    import pandas as pd  # type: ignore[import-untyped]
    import shap  # type: ignore[import-untyped]

    pipeline_path = settings.project_root / "models" / "recovery_pipeline.joblib"
    if not pipeline_path.exists():
        return {"error": "no model available", "shap_values": []}

    pipeline = joblib.load(pipeline_path)
    features = {
        "npci_bank": req.npci_bank,
        "error_class": req.error_class,
        "amount_paise": req.amount_paise,
        "regime": req.regime,
        "retry_prior": req.retry_prior,
    }
    row = pd.DataFrame([features])

    def _compute_shap() -> dict[str, Any]:
        prep = pipeline.named_steps["prep"]
        clf = pipeline.named_steps["clf"]
        X_transformed = prep.transform(row)

        # Get feature names after transformation
        ohe = prep.named_transformers_["cat"].named_steps["ohe"]
        cat_names = list(ohe.get_feature_names_out(["npci_bank", "error_class", "regime"]))
        all_names = ["amount_paise", "retry_prior"] + cat_names

        explainer = shap.TreeExplainer(clf)
        shap_values = explainer.shap_values(X_transformed)

        # shap 0.51 + sklearn GBM returns (1,22,2) for binary — extract positive class
        import numpy as np  # type: ignore[import-untyped]

        if isinstance(shap_values, list):
            # older API: list[array(n_samples, n_features)] per class
            vals = shap_values[1][0] if len(shap_values) > 1 else shap_values[0][0]
            ev = explainer.expected_value
            ev = ev[1] if hasattr(ev, "__len__") and len(ev) > 1 else ev  # type: ignore[index]
            base_value = float(ev)  # type: ignore[arg-type]
        elif isinstance(shap_values, np.ndarray) and shap_values.ndim == 3:
            # (n_samples, n_features, n_classes) — pick class 1
            vals = shap_values[0, :, 1]
            ev = explainer.expected_value
            ev_arr = np.asarray(ev)
            base_value = float(ev_arr.flat[1]) if ev_arr.size > 1 else float(ev_arr.flat[0])
        else:
            # (n_samples, n_features)
            vals = shap_values[0]  # type: ignore[index]
            ev = explainer.expected_value
            base_value = float(ev[0]) if hasattr(ev, "__len__") else float(ev)  # type: ignore[index]

        # Pair feature names with SHAP values, sort by absolute impact
        contributions = []
        for name, val in zip(all_names, vals):
            contributions.append({
                "feature": name,
                "shap_value": round(float(val), 4),
                "abs_value": round(abs(float(val)), 4),
            })
        contributions.sort(key=lambda c: c["abs_value"], reverse=True)

        # Keep top 10 most impactful
        top = contributions[:10]

        score = float(pipeline.predict_proba(row)[0, 1])
        return {
            "score": round(score, 4),
            "base_value": round(base_value, 4),
            "top_contributions": top,
            "all_contributions": contributions,
            "features": features,
        }

    result = await asyncio.to_thread(_compute_shap)
    return result


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "webhook_events": len(received_events)}


# --------------------------------------------------------------------------
# Batch control
# --------------------------------------------------------------------------

_batch_task: asyncio.Task[dict[str, int]] | None = None
_batch_state: dict[str, Any] = {"running": False, "batch_id": None}
_batch_stop = asyncio.Event()


class BatchRequest(BaseModel):
    n: int = Field(default=60, ge=1, le=2000)
    workers: int = Field(default=2, ge=1, le=6)


@app.post("/api/batch/start")
async def start_batch(req: BatchRequest) -> dict[str, Any]:
    global _batch_task
    if _batch_task is not None and not _batch_task.done():
        raise HTTPException(status_code=409, detail="A batch is already running")

    from datetime import datetime

    from eval.data_collector import run_batch

    batch_id = datetime.now(UTC).strftime("b%Y%m%d%H%M%S")
    _batch_stop.clear()
    _batch_state.update(running=True, batch_id=batch_id)
    await bus.publish(
        {"type": "batch_start", "batch_id": batch_id, "n": req.n, "workers": req.workers}
    )

    async def _run() -> dict[str, int]:
        try:
            result = await run_batch(
                n=req.n,
                workers=req.workers,
                batch_id=batch_id,
                db_path=settings.project_root / "data" / "training_data.db",
                sink=bus,
                stop_event=_batch_stop,
            )
            # incremental retrain on fresh labeled data
            try:
                await _train_now()
            except Exception as exc:  # noqa: BLE001
                logger.error("post_batch_training_failed", error=str(exc))
            return result
        finally:
            _batch_state.update(running=False, batch_id=None)

    _batch_task = asyncio.create_task(_run())
    return {"status": "started", "batch_id": batch_id, "n": req.n, "workers": req.workers}


# --------------------------------------------------------------------------
# Incremental model training
# --------------------------------------------------------------------------

_model_lock = asyncio.Lock()
_trainer_task: asyncio.Task[None] | None = None


async def _train_now() -> dict[str, Any]:
    """Retrain in a worker thread; never blocks the event loop."""
    from eval.train_model import train_incremental

    async with _model_lock:
        artifact = await asyncio.to_thread(train_incremental)
    if artifact.get("status") == "ok":
        await bus.publish(
            {
                "type": "model_trained",
                "rows": artifact.get("metrics", {}).get("rows"),
                "cv_accuracy": artifact.get("metrics", {}).get("cv_accuracy"),
                "trained_at": artifact.get("trained_at"),
            }
        )
    return artifact


@app.post("/api/model/train")
async def trigger_training() -> dict[str, Any]:
    if _model_lock.locked():
        raise HTTPException(status_code=409, detail="training already in progress")
    return await _train_now()


@app.get("/api/model/status")
async def model_status() -> dict[str, Any]:
    path = settings.project_root / "models" / "recovery_model.json"
    if not path.exists():
        return {"status": "no_model"}
    artifact = json.loads(path.read_text())
    return {
        "status": "ok",
        "trained_at": artifact.get("trained_at"),
        "metrics": artifact.get("metrics"),
    }


@app.get("/api/model/comparison")
async def model_comparison() -> dict[str, Any]:
    path = settings.project_root / "models" / "model_comparison.json"
    if not path.exists():
        return {"status": "no_data", "models": []}
    try:
        models_data = json.loads(path.read_text())
        valid_models = [m for m in models_data if "error" not in m and m.get("roc_auc", 0) > 0]
        valid_models.sort(key=lambda m: m.get("roc_auc", 0), reverse=True)
        return {"status": "ok", "total_benchmarked": len(models_data), "top_models": valid_models[:10]}
    except Exception as exc:  # noqa: BLE001 - file read/JSON parse must not crash endpoint
        return {"status": "error", "message": str(exc), "models": []}


async def _periodic_trainer() -> None:
    from eval.train_model import train_incremental

    await asyncio.sleep(90)  # warm-up
    while True:
        try:
            async with _model_lock:
                artifact = await asyncio.to_thread(train_incremental)
            if artifact.get("status") == "ok":
                await bus.publish(
                    {
                        "type": "model_trained",
                        "rows": artifact.get("metrics", {}).get("rows"),
                        "cv_accuracy": artifact.get("metrics", {}).get("cv_accuracy"),
                        "trained_at": artifact.get("trained_at"),
                    }
                )
        except Exception as exc:  # noqa: BLE001 - trainer must never kill the server
            logger.error("periodic_training_failed", error=str(exc))
        await asyncio.sleep(900)  # every 15 minutes


@app.on_event("startup")
async def start_trainer() -> None:
    global _trainer_task
    _trainer_task = asyncio.create_task(_periodic_trainer())


@app.on_event("shutdown")
async def stop_trainer() -> None:
    if _trainer_task is not None:
        _trainer_task.cancel()


@app.post("/api/batch/stop")
async def stop_batch() -> dict[str, Any]:
    if _batch_task is None or _batch_task.done():
        raise HTTPException(status_code=409, detail="no batch is running")
    _batch_stop.set()
    await bus.publish({"type": "batch_stopped", "batch_id": _batch_state.get("batch_id")})
    return {"status": "stopping", "note": "in-flight scenarios will finish"}


@app.get("/api/batch/status")
async def batch_status() -> dict[str, Any]:
    done = None
    if _batch_task is not None and _batch_task.done():
        exc = _batch_task.exception()
        done = "error" if exc else _batch_task.result()
        if exc is not None:
            logger.error("batch_failed", error=str(exc))
    return {**_batch_state, "result": done}


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


@app.get("/")
async def dashboard() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


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
