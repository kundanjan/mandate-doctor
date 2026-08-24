"""FastAPI application with Razorpay webhook receiver."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from mandate_doctor.config import settings

logger = structlog.get_logger(__name__)

app = FastAPI(title="Mandate Doctor", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8501"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

received_events: list[dict[str, str]] = []


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
    received_events.append(
        {"event_id": event.get("id", "unknown"), "type": event.get("event", "unknown")}
    )

    logger.info("webhook_received", event_type=event.get("event"), event_id=event.get("id"))
    return {"status": "ok", "event_type": event.get("event")}


@app.get("/api/events")
async def list_events() -> list[dict[str, str]]:
    return received_events


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "webhook_events": len(received_events)}
