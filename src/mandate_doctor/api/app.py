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
            return await run_batch(
                n=req.n,
                workers=req.workers,
                batch_id=batch_id,
                db_path=settings.project_root / "data" / "training_data.db",
                sink=bus,
            )
        finally:
            _batch_state.update(running=False, batch_id=None)

    _batch_task = asyncio.create_task(_run())
    return {"status": "started", "batch_id": batch_id, "n": req.n, "workers": req.workers}


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
