"""Paying for orders through Ironclad's merchant payments API.

FoodDash is a registered Ironclad merchant. When payments are enabled and
the ordering user has an Ironclad account (USER_MAP), an order starts with
payment_status "pending"; POST /orders/{id}/pay asks Ironclad for a payment
request, and the user approves it with the OTP in Ironclad. A background
task polls the request until it's final: confirmed -> paid and the delivery
starts; cancelled or expired -> failed and the order is cancelled.

Everyone else (payments disabled, or an unmapped user such as the traffic
simulator's user_5..user_20) keeps the simulated payment: paid at once, so
FoodDash runs on its own and the simulator never creates an OTP.

Config (read at call time):
    IRONCLAD_PAYMENTS_ENABLED   "true" to use Ironclad (default false)
    IRONCLAD_BASE_URL           default http://localhost:8001
    IRONCLAD_MERCHANT_KEY       FoodDash's mk_ key from Ironclad
    IRONCLAD_POLL_SECONDS       how often to poll a pending request (default 3)

In production Ironclad would call a FoodDash webhook when a payment
settles; polling stands in for that here.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

from core.logging_config import get_logger

logger = get_logger("payments")

# FoodDash user -> Ironclad payer. Only mapped users pay for real.
USER_MAP: dict[str, str] = {"user_1": "demo_user"}

FINAL_STATUSES = ("confirmed", "cancelled", "expired")

# Test seam: an httpx transport for reaching Ironclad (tests stub Ironclad with it).
_transport: httpx.AsyncBaseTransport | None = None


class IroncladError(Exception):
    """Ironclad refused the request (status_code 4xx) or couldn't be reached (None)."""

    def __init__(self, status_code: int | None, code: str, message: str):
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


def enabled() -> bool:
    return os.getenv("IRONCLAD_PAYMENTS_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def ironclad_user(fooddash_user_id: str) -> str | None:
    """The Ironclad payer for this user, or None if they use the simulated payment."""
    return USER_MAP.get(fooddash_user_id) if enabled() else None


def _base_url() -> str:
    return os.getenv("IRONCLAD_BASE_URL", "http://localhost:8001").rstrip("/")


def _headers(request_id: str) -> dict[str, str]:
    key = os.getenv("IRONCLAD_MERCHANT_KEY", "")
    if not key:
        raise IroncladError(None, "MERCHANT_KEY_MISSING", "IRONCLAD_MERCHANT_KEY is not set.")
    return {"Authorization": f"Bearer {key}", "X-Request-ID": request_id}


def poll_seconds() -> float:
    return float(os.getenv("IRONCLAD_POLL_SECONDS", "3"))


async def _call(method: str, path: str, *, request_id: str, order_id: str, json: dict | None = None,
                payment_request_id: str | None = None) -> dict[str, Any]:
    """One logged call to Ironclad. Raises IroncladError on any failure."""
    started = time.perf_counter()
    status_code: int | None = None
    try:
        async with httpx.AsyncClient(base_url=_base_url(), timeout=10, transport=_transport) as client:
            resp = await client.request(method, path, json=json, headers=_headers(request_id))
        status_code = resp.status_code
        body = resp.json() if resp.content else {}
    except IroncladError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        _log(method, path, request_id, order_id, payment_request_id, None, started, error=type(exc).__name__)
        raise IroncladError(None, "IRONCLAD_UNREACHABLE", f"Ironclad request failed: {exc}") from exc

    payment_request_id = payment_request_id or body.get("payment_request_id")
    if status_code >= 400:
        code = body.get("error", "IRONCLAD_ERROR")
        _log(method, path, request_id, order_id, payment_request_id, status_code, started, error=code)
        raise IroncladError(status_code, code, body.get("message") or f"Ironclad returned {status_code}")
    _log(method, path, request_id, order_id, payment_request_id, status_code, started, status=body.get("status"))
    return body


def _log(method, path, request_id, order_id, payment_request_id, status_code, started, *, status=None, error=None):
    message = f"Ironclad {method} {path} -> {status_code if status_code is not None else 'unreachable'}"
    extra = {
        "request_id": request_id, "endpoint": "ironclad" + ("/payments/requests/{id}" if method == "GET" else path),
        "duration_ms": int((time.perf_counter() - started) * 1000), "order_id": order_id,
        "payment_request_id": payment_request_id, "status": status, "error_type": error,
    }
    (logger.error if error else logger.info)(message, extra=extra)


async def request_payment(*, order_id: str, amount: float, payer: str, channel: str, request_id: str) -> dict:
    """Ask Ironclad for a payment request; the user approves it with the OTP."""
    return await _call("POST", "/payments/requests", request_id=request_id, order_id=order_id, json={
        "order_ref": order_id,
        "amount": round(amount, 2),
        "user_id": payer,
        "channel": channel,
        "idempotency_key": f"fooddash-order-{order_id}",
        "description": f"FoodDash order {order_id[:8]}",
    })


async def payment_status(payment_request_id: str, *, order_id: str, request_id: str) -> dict:
    return await _call("GET", f"/payments/requests/{payment_request_id}", request_id=request_id,
                       order_id=order_id, payment_request_id=payment_request_id)


async def wait_until_final(payment_request_id: str, *, order_id: str, request_id: str) -> str:
    """Poll until the request is confirmed, cancelled or expired. Ironclad
    expires an unconfirmed request after its OTP TTL, so this ends."""
    while True:
        await asyncio.sleep(poll_seconds())
        try:
            status = (await payment_status(payment_request_id, order_id=order_id, request_id=request_id))["status"]
        except IroncladError as exc:
            if exc.status_code == 404:
                return "cancelled"  # the request is gone: nothing will ever pay this order
            # Anything else (unreachable, 5xx, a revoked key) is logged by
            # _call; keep polling rather than cancel an order the user might
            # still pay for in Ironclad.
            continue
        if status in FINAL_STATUSES:
            return status
