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
from datetime import UTC
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from mandate_doctor.api.events import bus
from mandate_doctor.config import settings

logger = structlog.get_logger(__name__)

app = FastAPI(title="Mandate Doctor", version="0.2.0")

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

    await bus.publish({"type": "webhook", "event_type": event_type, "entity_id": plink_id})

    logger.info("webhook_received", event_type=event_type, event_id=event_id)
    return {"status": "ok", "event_type": event_type}


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

    return {
        "status": "ok",
        "totals": totals,
        "by_regime": by_regime,
        "by_class": by_class,
        "by_bank": by_bank,
        "by_amount": by_amount,
        "model": model_metrics,
    }


@app.get("/api/events")
async def list_events() -> list[dict[str, str]]:
    return received_events


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "webhook_events": len(received_events)}


# --------------------------------------------------------------------------
# Batch control
# --------------------------------------------------------------------------

_batch_task: asyncio.Task[dict[str, int]] | None = None
_batch_state: dict[str, Any] = {"running": False, "batch_id": None}


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
