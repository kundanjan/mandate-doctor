"""Razorpay test-mode API client.

All credentials are read from environment variables via Settings.
Secrets are never logged, never included in error messages, and never
persisted beyond the process lifetime.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import structlog

from mandate_doctor.config import settings

logger = structlog.get_logger(__name__)

BASE_URL = "https://api.razorpay.com/v1"

# Rate-limit resilience: retry throttled/failed calls with exponential
# backoff. Razorpay signals throttling as BAD_REQUEST_ERROR with a
# "too many requests" description or HTTP 429.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4


class RazorpayError(Exception):
    """Raised when a Razorpay API call fails."""

    def __init__(self, status_code: int, error_code: str, description: str):
        self.status_code = status_code
        self.error_code = error_code
        self.description = description
        super().__init__(f"[{error_code}] {description}")


def _auth() -> tuple[str, str]:
    key_id = settings.razorpay_key_id
    key_secret = settings.razorpay_key_secret
    if not key_id or not key_secret:
        raise RazorpayError(0, "CONFIG_ERROR", "RAZORPAY_KEY_ID or RAZORPAY_KEY_SECRET not set")
    if not key_id.startswith("rzp_test_"):
        raise RazorpayError(
            0,
            "CONFIG_ERROR",
            "Refusing to use non-test-mode keys. This system is test-only.",
        )
    return (key_id, key_secret)


def _is_retryable(err: RazorpayError) -> bool:
    if err.status_code in _RETRYABLE_STATUS:
        return True
    return "too many request" in err.description.lower()


async def _with_retries(fn: Callable[[], Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    last_exc: RazorpayError | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return await fn(*args, **kwargs)
        except RazorpayError as exc:
            if not _is_retryable(exc) or attempt == _MAX_ATTEMPTS - 1:
                raise
            delay = (2**attempt) + random.uniform(0, 0.5)
            logger.warning(
                "razorpay_retry",
                fn=fn.__name__ if hasattr(fn, "__name__") else "call",
                attempt=attempt + 1,
                delay_s=round(delay, 2),
                code=exc.error_code,
            )
            last_exc = exc
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]  # pragma: no cover


async def create_order(
    amount_paise: int,
    receipt: str,
    currency: str = "INR",
) -> dict[str, Any]:
    """Create a Razorpay order.

    Returns the raw API response dict with keys including
    id, amount, currency, status, receipt.
    """

    async def _call() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{BASE_URL}/orders",
                auth=_auth(),
                json={
                    "amount": amount_paise,
                    "currency": currency,
                    "receipt": receipt,
                },
            )
            return _handle_response(resp)

    result: dict[str, Any] = await _with_retries(_call)
    return result


async def create_payment_link(
    amount_paise: int,
    reference_id: str,
    customer_name: str = "Test Customer",
    customer_email: str = "test@example.com",
    customer_contact: str = "+919820123456",
    description: str = "Recovery payment",
    send_sms: bool = False,
    send_email: bool = False,
) -> dict[str, Any]:
    """Create a Razorpay Payment Link for recovery.

    Returns the raw API response dict with keys including
    id, short_url, amount, status, reference_id.
    """

    async def _call() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{BASE_URL}/payment_links",
                auth=_auth(),
                json={
                    "amount": amount_paise,
                    "currency": "INR",
                    "accept_partial": False,
                    "reference_id": reference_id,
                    "description": description,
                    "customer": {
                        "name": customer_name,
                        "email": customer_email,
                        "contact": customer_contact,
                    },
                    "notify": {"sms": send_sms, "email": send_email},
                    "reminder_enable": False,
                    "notes": {
                        "source": "mandate_doctor",
                        "reference_id": reference_id,
                    },
                },
            )
            return _handle_response(resp)

    result: dict[str, Any] = await _with_retries(_call)
    return result


async def fetch_order_payments(order_id: str) -> list[dict[str, Any]]:
    """Fetch all payment attempts made against an order.

    Used to capture the REAL error code from a failed debit attempt.
    """

    async def _call() -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{BASE_URL}/orders/{order_id}/payments",
                auth=_auth(),
            )
            if resp.status_code == 200:
                items: list[dict[str, Any]] = resp.json().get("items", [])
                return items
            _handle_response(resp)
            return []  # pragma: no cover

    result: list[dict[str, Any]] = await _with_retries(_call)
    return result


async def fetch_payment_link(link_id: str) -> dict[str, Any]:
    """Fetch a Payment Link by ID to check its current status."""

    async def _call() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{BASE_URL}/payment_links/{link_id}",
                auth=_auth(),
            )
            return _handle_response(resp)

    result: dict[str, Any] = await _with_retries(_call)
    return result


async def fetch_payment(payment_id: str) -> dict[str, Any]:
    """Fetch a payment by ID to check its status."""

    async def _call() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{BASE_URL}/payments/{payment_id}",
                auth=_auth(),
            )
            return _handle_response(resp)

    result: dict[str, Any] = await _with_retries(_call)
    return result


def _handle_response(resp: httpx.Response) -> dict[str, Any]:
    """Parse a Razorpay API response or raise RazorpayError."""
    if resp.status_code == 200:
        data: dict[str, Any] = resp.json()
        return data

    try:
        body: dict[str, Any] = resp.json()
        error = body.get("error", {})
        code = error.get("code", f"HTTP_{resp.status_code}")
        desc = error.get("description", resp.text[:200])
    except Exception:
        code = f"HTTP_{resp.status_code}"
        desc = resp.text[:200]

    logger.error("razorpay_api_error", status=resp.status_code, code=code)
    raise RazorpayError(status_code=resp.status_code, error_code=code, description=desc)
