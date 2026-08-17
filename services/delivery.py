from __future__ import annotations

import asyncio
import json
import os
import random

import httpx
import redis.asyncio as redis
from fastapi import APIRouter, HTTPException

from core.exceptions import DeliveryStatusUpdateError, NoDriversAvailableError
from core.failure_injection import random_failure
from core.logging_config import get_logger
from core.models import Delivery, DeliveryStatus

logger = get_logger("delivery")

router = APIRouter(prefix="/delivery", tags=["delivery"])

INTERNAL_BASE_URL = os.environ.get("INTERNAL_BASE_URL", "http://localhost:8000")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

_DELIVERIES: dict[str, Delivery] = {}
_LIFECYCLE_STAGES = [DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, DeliveryStatus.DELIVERED]
_TIMEOUT_PROBABILITY = 0.04


@random_failure(probability=0.05, failure_type="internal_server_error", exception=NoDriversAvailableError)
async def _assign_driver(order_id: str) -> Delivery:
    delivery = Delivery(
        order_id=order_id,
        status=DeliveryStatus.ASSIGNED,
        driver_id=f"driver_{random.randint(100, 999)}",
        eta_seconds=random.randint(3, 12),
    )
    _DELIVERIES[order_id] = delivery
    logger.info(
        f"Driver {delivery.driver_id} assigned to order {order_id}",
        extra={"endpoint": "delivery.assign"},
    )
    return delivery


@random_failure(probability=0.06, failure_type="connection_error", exception=DeliveryStatusUpdateError)
async def _notify_order_service(order_id: str, status: str) -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.put(f"{INTERNAL_BASE_URL}/orders/{order_id}/status", params={"status": status})
    response.raise_for_status()


async def run_delivery_lifecycle(order_id: str) -> None:
    """Drives a delivery through its full lifecycle with per-stage delays. Launched
    as a fire-and-forget background task whenever an order is confirmed, so any
    failure here is logged directly rather than raised to a nonexistent caller."""
    try:
        await _assign_driver(order_id)
    except NoDriversAvailableError as exc:
        logger.error(
            f"No drivers available for order {order_id}: {exc}",
            extra={"endpoint": "delivery.assign", "error_type": "NoDriversAvailableError"},
        )
        return

    for stage in _LIFECYCLE_STAGES:
        await asyncio.sleep(random.uniform(1, 4))

        if random.random() < _TIMEOUT_PROBABILITY:
            logger.error(
                f"Delivery timeout for order {order_id} approaching stage {stage.value}",
                extra={"endpoint": "delivery.lifecycle", "error_type": "DeliveryTimeoutError"},
            )
            return

        delivery = _DELIVERIES.get(order_id)
        if delivery is None:
            return
        delivery.status = stage

        try:
            await _notify_order_service(order_id, stage.value)
        except DeliveryStatusUpdateError as exc:
            logger.error(
                f"Failed to notify order service for {order_id}: {exc}",
                extra={"endpoint": "delivery.lifecycle", "error_type": "DeliveryStatusUpdateError"},
            )
        except httpx.HTTPError as exc:
            # A real (non-injected) failure calling back to the order service —
            # this is a background task with no caller to propagate to, so it
            # must be logged and swallowed here or it leaks as an unretrieved
            # asyncio task exception instead of a readable log line.
            logger.error(
                f"Unexpected error notifying order service for {order_id}: {exc}",
                extra={"endpoint": "delivery.lifecycle", "error_type": type(exc).__name__},
            )

    logger.info(f"Order {order_id} delivered", extra={"endpoint": "delivery.lifecycle"})


async def subscribe_to_orders() -> None:
    """Background task started at app startup: subscribes to confirmed orders over
    Redis pub/sub and kicks off a delivery lifecycle for each one. Retries with
    backoff so a Redis outage at boot (or mid-run) doesn't crash the whole app."""
    while True:
        try:
            redis_client = redis.from_url(REDIS_URL, decode_responses=True)
            pubsub = redis_client.pubsub()
            await pubsub.subscribe("order.confirmed")
            logger.info("Delivery service subscribed to order.confirmed", extra={"endpoint": "delivery.subscribe"})

            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    order = json.loads(message["data"])
                except json.JSONDecodeError:
                    continue
                asyncio.create_task(run_delivery_lifecycle(order["id"]))
        except Exception as exc:
            logger.error(
                f"Redis subscription lost: {exc}",
                extra={"endpoint": "delivery.subscribe", "error_type": type(exc).__name__},
            )
            await asyncio.sleep(3)


@router.put("/{order_id}/status")
async def update_status(order_id: str, status: DeliveryStatus) -> Delivery:
    delivery = _DELIVERIES.get(order_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="Delivery not found")
    delivery.status = status
    return delivery


@router.get("")
async def list_deliveries(limit: int = 100) -> list[Delivery]:
    """Read-only listing for the dashboard — most recently assigned first."""
    return list(reversed(list(_DELIVERIES.values())))[:limit]


def delivery_status_counts() -> dict[str, int]:
    """Plain function (not an endpoint) — used by main.py's /stats aggregation."""
    counts: dict[str, int] = {}
    for delivery in _DELIVERIES.values():
        counts[delivery.status.value] = counts.get(delivery.status.value, 0) + 1
    return counts


@router.get("/{order_id}")
async def get_delivery(order_id: str) -> Delivery:
    delivery = _DELIVERIES.get(order_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="Delivery not found")
    return delivery
