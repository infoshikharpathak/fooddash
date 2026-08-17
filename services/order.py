from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone

import httpx
import redis.asyncio as redis
from fastapi import APIRouter, HTTPException, Request

from core.exceptions import (
    DuplicateOrderError,
    InventoryConflictError,
    PaymentTimeoutError,
    RedisUnavailableError,
    RestaurantOfflineError,
)
from core.failure_injection import random_failure
from core.logging_config import get_logger
from core.models import DeliveryStatus, Order, OrderCreateRequest, OrderStatus

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

INTERNAL_BASE_URL = os.environ.get("INTERNAL_BASE_URL", "http://localhost:8000")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
ORDER_CONFIRMED_CHANNEL = "order.confirmed"

_ORDERS: dict[str, Order] = {}
_redis_client: redis.Redis | None = None

# Duplicate-order detection: same user + restaurant + items within this window = a
# double-click/double-submit, not two legitimately separate orders.
_DUPLICATE_WINDOW_SECONDS = 5.0
_recent_signatures: dict[tuple[str, str, str], float] = {}


def _get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


def _order_signature(payload: OrderCreateRequest) -> tuple[str, str, str]:
    items_key = ",".join(sorted(f"{i.menu_item_id}:{i.quantity}" for i in payload.items))
    return (payload.user_id, payload.restaurant_id, items_key)


async def _fetch_menu(restaurant_id: str, request_id: str) -> dict[str, dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(
                f"{INTERNAL_BASE_URL}/restaurants/{restaurant_id}/menu",
                headers={"X-Request-ID": request_id},
            )
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail="Restaurant not found")
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # The restaurant service call itself failed (its own injected failure
            # cascading here) — surface as a clean, logged FoodDashError instead of
            # letting a raw httpx exception crash the ASGI app.
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
@random_failure(probability=0.10, failure_type="timeout", exception=PaymentTimeoutError)
@random_failure(probability=0.06, failure_type="internal_server_error", exception=RedisUnavailableError)
async def create_order(payload: OrderCreateRequest, request: Request) -> Order:
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))

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

    order = Order(
        id=str(uuid.uuid4()),
        restaurant_id=payload.restaurant_id,
        user_id=payload.user_id,
        items=payload.items,
        total=round(total, 2),
        status=OrderStatus.CONFIRMED,
        created_at=datetime.now(timezone.utc).isoformat(),
        request_id=request_id,
    )
    _ORDERS[order.id] = order

    try:
        await _get_redis().publish(ORDER_CONFIRMED_CHANNEL, order.model_dump_json())
    except Exception as exc:
        logger.error(
            f"Failed to publish order {order.id} to Redis: {exc}",
            extra={"request_id": request_id, "endpoint": "/orders", "error_type": "RedisUnavailableError"},
        )
        raise RedisUnavailableError(str(exc)) from exc

    logger.info(
        f"Order {order.id} confirmed, total ${order.total}",
        extra={"request_id": request_id, "endpoint": "/orders", "user_id": payload.user_id},
    )
    return order


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
    return order


@router.put("/{order_id}/status")
async def update_order_status(order_id: str, status: DeliveryStatus) -> Order:
    """Called by the delivery service over HTTP as a delivery progresses through its lifecycle."""
    order = _ORDERS.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    order.status = _DELIVERY_TO_ORDER_STATUS.get(status, order.status)
    return order
