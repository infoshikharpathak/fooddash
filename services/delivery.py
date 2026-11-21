from __future__ import annotations

import asyncio
import os
import random

import httpx
from fastapi import APIRouter, HTTPException

from core.internal_client import internal_client
from core.logging_config import get_logger
from core.models import Delivery, DeliveryStatus

logger = get_logger("delivery")

router = APIRouter(prefix="/delivery", tags=["delivery"])

_DELIVERIES: dict[str, Delivery] = {}
_LIFECYCLE_STAGES = [DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, DeliveryStatus.DELIVERED]

# Seconds to wait before each lifecycle stage, drawn uniformly from [min, max].
# Defaults give a watchable pace for the demo; tests set both to 0.
_STAGE_DELAY_MIN = float(os.environ.get("DELIVERY_STAGE_DELAY_MIN", "1"))
_STAGE_DELAY_MAX = float(os.environ.get("DELIVERY_STAGE_DELAY_MAX", "4"))


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


async def _notify_order_service(order_id: str, status: str) -> dict:
    """Report progress to the order service; returns the order as it now stands."""
    async with internal_client(timeout=5) as client:
        response = await client.put(f"/orders/{order_id}/status", params={"status": status})
    response.raise_for_status()
    return response.json()


async def run_delivery_lifecycle(order_id: str) -> None:
    """Drives a delivery through its full lifecycle with per-stage delays. Launched
    as a fire-and-forget background task by the order service whenever an order
    is confirmed, so any failure here is logged directly rather than raised to a
    nonexistent caller. Stops early if the order was cancelled meanwhile."""
    await _assign_driver(order_id)

    for stage in _LIFECYCLE_STAGES:
        await asyncio.sleep(random.uniform(_STAGE_DELAY_MIN, _STAGE_DELAY_MAX))

        delivery = _DELIVERIES.get(order_id)
        if delivery is None:
            return
        delivery.status = stage

        try:
            order = await _notify_order_service(order_id, stage.value)
        except httpx.HTTPError as exc:
            # A failure calling back to the order service —
            # this is a background task with no caller to propagate to, so it
            # must be logged and swallowed here or it leaks as an unretrieved
            # asyncio task exception instead of a readable log line.
            logger.error(
                f"Unexpected error notifying order service for {order_id}: {exc}",
                extra={"endpoint": "delivery.lifecycle", "error_type": type(exc).__name__},
            )
            continue

        if order.get("status") == "cancelled":
            delivery.status = DeliveryStatus.FAILED
            logger.info(
                f"Delivery for order {order_id} stopped: order was cancelled",
                extra={"endpoint": "delivery.lifecycle"},
            )
            return

    logger.info(f"Order {order_id} delivered", extra={"endpoint": "delivery.lifecycle"})


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
