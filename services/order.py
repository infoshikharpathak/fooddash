from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.exceptions import (
    DuplicateOrderError,
    InventoryConflictError,
    RestaurantOfflineError,
)
from core.background import spawn_background
from core.internal_client import internal_client
from core.logging_config import get_logger
from core.models import (
    DeliveryStatus,
    Order,
    OrderCreateRequest,
    OrderStatus,
    PayRequest,
    PayResponse,
    PaymentStatus,
)
from services import delivery, payments, users

logger = get_logger("order")

router = APIRouter(prefix="/orders", tags=["order"])

# The delivery service reports progress in its own vocabulary (DeliveryStatus),
# which doesn't line up 1:1 with OrderStatus (no "in_transit" order state) — map
# it rather than accepting DeliveryStatus values as if they were OrderStatus.
_DELIVERY_TO_ORDER_STATUS: dict[DeliveryStatus, OrderStatus] = {
    DeliveryStatus.ASSIGNED: OrderStatus.READY,
    DeliveryStatus.PICKED_UP: OrderStatus.PICKED_UP,
    DeliveryStatus.IN_TRANSIT: OrderStatus.PICKED_UP,
    DeliveryStatus.DELIVERED: OrderStatus.DELIVERED,
    DeliveryStatus.FAILED: OrderStatus.CANCELLED,
}

_ORDERS: dict[str, Order] = {}

# Duplicate-order detection: same user + restaurant + items within this window = a
# double-click/double-submit, not two legitimately separate orders.
_DUPLICATE_WINDOW_SECONDS = 5.0
_recent_signatures: dict[tuple[str, str, str], float] = {}


def _order_signature(payload: OrderCreateRequest) -> tuple[str, str, str]:
    items_key = ",".join(sorted(f"{i.menu_item_id}:{i.quantity}" for i in payload.items))
    return (payload.user_id, payload.restaurant_id, items_key)


async def _fetch_menu(restaurant_id: str, request_id: str) -> dict[str, dict]:
    async with internal_client(timeout=10) as client:
        try:
            response = await client.get(
                f"/restaurants/{restaurant_id}/menu",
                headers={"X-Request-ID": request_id},
            )
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail="Restaurant not found")
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # The restaurant service call itself failed — surface as a clean, logged
            # FoodDashError instead of letting a raw httpx exception crash the ASGI app.
            logger.error(
                f"Restaurant service call failed for {restaurant_id}: {exc}",
                extra={"request_id": request_id, "endpoint": "/orders", "error_type": "RestaurantOfflineError"},
            )
            raise RestaurantOfflineError(f"Restaurant {restaurant_id} is unavailable") from exc
        except httpx.RequestError as exc:
            logger.error(
                f"Restaurant service unreachable for {restaurant_id}: {exc}",
                extra={"request_id": request_id, "endpoint": "/orders", "error_type": "RestaurantOfflineError"},
            )
            raise RestaurantOfflineError(f"Restaurant {restaurant_id} is unreachable") from exc

    return {item["id"]: item for item in response.json()}


@router.post("", status_code=201)
async def create_order(payload: OrderCreateRequest, request: Request) -> Order:
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))

    user = users.get_user_or_none(payload.user_id)
    if user is None:
        logger.warning(
            f"Order attempted for unknown user {payload.user_id}",
            extra={"request_id": request_id, "endpoint": "/orders", "user_id": payload.user_id},
        )
        raise HTTPException(status_code=404, detail="User not found")

    signature = _order_signature(payload)
    now = time.monotonic()
    last_seen = _recent_signatures.get(signature)
    if last_seen is not None and now - last_seen < _DUPLICATE_WINDOW_SECONDS:
        logger.warning(
            f"Duplicate order detected for user {payload.user_id}",
            extra={"request_id": request_id, "endpoint": "/orders", "error_type": "DuplicateOrderError"},
        )
        raise DuplicateOrderError(f"Duplicate order for user {payload.user_id}")
    _recent_signatures[signature] = now

    menu = await _fetch_menu(payload.restaurant_id, request_id)

    total = 0.0
    for line in payload.items:
        menu_item = menu.get(line.menu_item_id)
        if menu_item is None or not menu_item.get("available", True):
            logger.warning(
                f"Inventory conflict for item {line.menu_item_id}",
                extra={"request_id": request_id, "endpoint": "/orders", "error_type": "InventoryConflictError"},
            )
            raise InventoryConflictError(f"{line.menu_item_id} is unavailable")
        total += menu_item["price"] * line.quantity

    real_payment = payments.ironclad_user(payload.user_id) is not None
    order = Order(
        id=str(uuid.uuid4()),
        restaurant_id=payload.restaurant_id,
        user_id=payload.user_id,
        items=payload.items,
        total=round(total, 2),
        status=OrderStatus.CONFIRMED,
        # Real payment (Ironclad) for mapped users when enabled: pending until
        # POST /orders/{id}/pay is approved with the OTP. Everyone else gets
        # the simulated payment: paid at once.
        payment_status=PaymentStatus.PENDING if real_payment else PaymentStatus.PAID,
        # Snapshot, not a reference — a later address change doesn't reroute it.
        delivery_address=user.address,
        created_at=datetime.now(timezone.utc).isoformat(),
        request_id=request_id,
    )
    _ORDERS[order.id] = order

    if real_payment:
        # Delivery starts only once the payment is confirmed (see _settle_payment).
        logger.info(
            f"Order {order.id} confirmed, total ${order.total}, awaiting payment",
            extra={"request_id": request_id, "endpoint": "/orders", "user_id": payload.user_id},
        )
        return order

    spawn_background(delivery.run_delivery_lifecycle(order.id))

    logger.info(
        f"Order {order.id} confirmed, total ${order.total}",
        extra={"request_id": request_id, "endpoint": "/orders", "user_id": payload.user_id},
    )
    return order


def _pay_response(order: Order, channel: str, *, message: str, ironclad: dict | None = None) -> PayResponse:
    ironclad = ironclad or {}
    return PayResponse(
        order_id=order.id, payment_status=order.payment_status,
        payment_request_id=order.payment_request_id, amount=order.total, channel=channel,
        ironclad_status=ironclad.get("status"), merchant_display_name=ironclad.get("merchant_display_name"),
        expires_at=ironclad.get("expires_at"), message=message,
    )


@router.post("/{order_id}/pay")
async def pay_for_order(order_id: str, request: Request, body: PayRequest | None = None) -> PayResponse:
    """Ask Ironclad to collect this order's total from the user. Nothing is
    paid until the user approves it with the OTP in Ironclad.

    If Ironclad refuses, its status code is passed on (e.g. 403 for
    AGENT_PAYMENTS_NOT_ALLOWED) with {"detail": {"error", "message"}}."""
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    channel = (body or PayRequest()).channel
    order = _ORDERS.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    payer = payments.ironclad_user(order.user_id)
    if payer is None:
        raise HTTPException(status_code=409, detail="This order doesn't take an Ironclad payment "
                                                    "(payments are disabled or the user has no Ironclad account).")
    if order.payment_status == PaymentStatus.PAID:
        return _pay_response(order, channel, message="This order is already paid.")
    if order.status == OrderStatus.CANCELLED or order.payment_status == PaymentStatus.FAILED:
        raise HTTPException(status_code=409, detail=f"Order is {order.status.value} (payment "
                                                    f"{order.payment_status.value}); it can't be paid.")
    if order.payment_request_id is not None:
        return _pay_response(order, channel, message=(
            "A payment request for this order is already waiting for the user to approve it with the OTP "
            "in Ironclad."))

    try:
        result = await payments.request_payment(order_id=order.id, amount=order.total, payer=payer,
                                                channel=channel, request_id=request_id)
    except payments.IroncladError as exc:
        if exc.status_code is None or exc.status_code >= 500:
            raise HTTPException(status_code=502, detail={
                "error": "IRONCLAD_UNAVAILABLE", "message": f"Ironclad is unavailable: {exc.message}"}) from exc
        logger.warning(
            f"Ironclad refused payment for order {order.id}: {exc.code}",
            extra={"request_id": request_id, "endpoint": "/orders/{id}/pay", "error_type": exc.code},
        )
        # 401 means FoodDash's own key is wrong: that's our problem, not the caller's.
        status = 502 if exc.status_code == 401 else exc.status_code
        raise HTTPException(status_code=status, detail={
            "error": exc.code, "message": f"Ironclad refused the payment: {exc.message}"}) from exc

    order.payment_request_id = result["payment_request_id"]
    spawn_background(_settle_payment(order.id, order.payment_request_id, request_id))
    return _pay_response(order, channel, ironclad=result, message=(
        f"Payment of ${order.total:.2f} requested. The user must approve it with the OTP in Ironclad; "
        "the order is not paid until then."))


class FlagPaymentRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.post("/{order_id}/payment/flag")
async def flag_order_payment(order_id: str, body: FlagPaymentRequest, request: Request) -> dict:
    """Flag this order's pending Ironclad payment request: the ordering
    assistant's reply about it was blocked, or pushed an action the user
    didn't ask for. It stays pending; the user's OTP modal shows a warning."""
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    order = _ORDERS.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.payment_request_id is None:
        raise HTTPException(status_code=409, detail="This order has no Ironclad payment request to flag.")
    try:
        result = await payments.flag_payment(order.payment_request_id, reason=body.reason, order_id=order.id,
                                             request_id=request_id)
    except payments.IroncladError as exc:
        status = 502 if exc.status_code is None or exc.status_code >= 500 or exc.status_code == 401 else exc.status_code
        raise HTTPException(status_code=status, detail={"error": exc.code, "message": exc.message}) from exc
    return {"order_id": order.id, "payment_request_id": order.payment_request_id,
            "flagged_by_output_scan": bool(result.get("flagged_by_output_scan"))}


async def _settle_payment(order_id: str, payment_request_id: str, request_id: str) -> None:
    """Background: wait for the user's decision in Ironclad, then pay or cancel."""
    outcome = await payments.wait_until_final(payment_request_id, order_id=order_id, request_id=request_id)
    order = _ORDERS.get(order_id)
    if order is None:
        return
    extra = {"request_id": request_id, "endpoint": "/orders/{id}/pay", "user_id": order.user_id}
    if outcome == "confirmed":
        order.payment_status = PaymentStatus.PAID
        if order.status == OrderStatus.CANCELLED:
            # Paid after it was cancelled here: the money moved, so this needs
            # a refund (Ironclad has no merchant refund API yet).
            logger.error(f"Order {order_id} was paid after it was cancelled; refund needed",
                         extra={**extra, "error_type": "PaidAfterCancel"})
            return
        logger.info(f"Order {order_id} paid", extra=extra)
        spawn_background(delivery.run_delivery_lifecycle(order_id))
        return
    order.payment_status = PaymentStatus.FAILED
    if order.status == OrderStatus.CANCELLED:  # we cancelled it (and withdrew the request)
        logger.info(f"Payment {outcome} for cancelled order {order_id}", extra=extra)
        return
    if order.status not in (OrderStatus.PICKED_UP, OrderStatus.DELIVERED):
        order.status = OrderStatus.CANCELLED
    logger.warning(f"Payment {outcome} for order {order_id}; order cancelled",
                   extra={**extra, "error_type": f"Payment{outcome.capitalize()}"})


def unpaid_timeout_seconds() -> float:
    return float(os.getenv("UNPAID_ORDER_TIMEOUT_SECONDS", "900"))


def cancel_unpaid_orders(now: datetime | None = None) -> list[str]:
    """Cancel orders still waiting for payment with no payment request after
    UNPAID_ORDER_TIMEOUT_SECONDS (default 15 minutes), e.g. ones another
    app's agent placed and the user never paid in FoodDash. Orders with a
    payment request are settled by _settle_payment instead (Ironclad expires
    unapproved requests)."""
    now = now or datetime.now(timezone.utc)
    limit = unpaid_timeout_seconds()
    cancelled = []
    for order in list(_ORDERS.values()):
        if (order.payment_status != PaymentStatus.PENDING or order.payment_request_id is not None
                or order.status in (OrderStatus.CANCELLED, OrderStatus.PICKED_UP, OrderStatus.DELIVERED)):
            continue
        if (now - datetime.fromisoformat(order.created_at)).total_seconds() < limit:
            continue
        order.status = OrderStatus.CANCELLED
        order.payment_status = PaymentStatus.FAILED
        cancelled.append(order.id)
        logger.warning(f"Order {order.id} cancelled: unpaid after {limit:.0f}s",
                       extra={"endpoint": "unpaid-sweeper", "user_id": order.user_id, "error_type": "UnpaidTimeout"})
    return cancelled


async def run_unpaid_order_sweeper(interval: float | None = None) -> None:
    """Background: cancel unpaid orders every *interval* seconds (default 60)."""
    import asyncio

    while True:
        await asyncio.sleep(interval or float(os.getenv("UNPAID_ORDER_SWEEP_SECONDS", "60")))
        try:
            cancel_unpaid_orders()
        except Exception:  # noqa: BLE001  never let the sweeper die
            logger.exception("Unpaid-order sweep failed", extra={"endpoint": "unpaid-sweeper"})


@router.get("")
async def list_orders(limit: int = 100) -> list[Order]:
    """Read-only listing for the dashboard — most recent first."""
    return sorted(_ORDERS.values(), key=lambda o: o.created_at, reverse=True)[:limit]


def order_status_counts() -> dict[str, int]:
    """Plain function (not an endpoint) — used by main.py's /stats aggregation."""
    counts: dict[str, int] = {}
    for order in _ORDERS.values():
        counts[order.status.value] = counts.get(order.status.value, 0) + 1
    return counts


@router.get("/{order_id}")
async def get_order(order_id: str) -> Order:
    order = _ORDERS.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.put("/{order_id}/cancel")
async def cancel_order(order_id: str, request: Request) -> Order:
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    order = _ORDERS.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.status in (OrderStatus.PICKED_UP, OrderStatus.DELIVERED, OrderStatus.CANCELLED):
        logger.warning(
            f"Cannot cancel order {order_id} in status {order.status.value}",
            extra={"request_id": request_id, "endpoint": "/orders/{id}/cancel"},
        )
        raise HTTPException(status_code=400, detail=f"Order already {order.status.value}, cannot cancel")

    order.status = OrderStatus.CANCELLED
    logger.info(f"Order {order_id} cancelled", extra={"request_id": request_id, "endpoint": "/orders/{id}/cancel"})
    if order.payment_request_id is not None and order.payment_status == PaymentStatus.PENDING:
        await _withdraw_payment(order, request_id)
    return order


async def _withdraw_payment(order: Order, request_id: str) -> None:
    """The order was cancelled while its payment waits for the user's OTP:
    withdraw the request in Ironclad so it can't be paid any more."""
    extra = {"request_id": request_id, "endpoint": "/orders/{id}/cancel", "user_id": order.user_id}
    try:
        result = await payments.cancel_payment(order.payment_request_id, order_id=order.id, request_id=request_id)
    except payments.IroncladError as exc:
        if exc.code == "PAYMENT_ALREADY_CONFIRMED":
            # The user paid just before the cancel: the money moved.
            order.payment_status = PaymentStatus.PAID
            logger.error(f"Order {order.id} was paid after it was cancelled; refund needed",
                         extra={**extra, "error_type": "PaidAfterCancel"})
            return
        # Unreachable or refused: the poller still watches the request, and
        # logs a refund if the user pays it anyway.
        logger.error(f"Couldn't withdraw payment {order.payment_request_id} for cancelled order {order.id}: "
                     f"{exc.code}", extra={**extra, "error_type": exc.code})
        return
    if result.get("status") == "confirmed":  # defensive: Ironclad answers 409 for this
        order.payment_status = PaymentStatus.PAID
        return
    order.payment_status = PaymentStatus.FAILED


@router.put("/{order_id}/status")
async def update_order_status(order_id: str, status: DeliveryStatus) -> Order:
    """Called by the delivery service over HTTP as a delivery progresses through its lifecycle."""
    order = _ORDERS.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    # Cancellation is terminal: a delivery still in flight must not revive the order.
    if order.status != OrderStatus.CANCELLED:
        order.status = _DELIVERY_TO_ORDER_STATUS.get(status, order.status)
    return order
