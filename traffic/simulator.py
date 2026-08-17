from __future__ import annotations

import asyncio
import os
import random
import uuid

import httpx

from core.logging_config import get_logger

logger = get_logger("traffic_simulator")

BASE_URL = os.environ.get("INTERNAL_BASE_URL", "http://localhost:8000")
_USER_IDS = [f"user_{i}" for i in range(1, 21)]


async def _step_delay() -> None:
    await asyncio.sleep(random.uniform(1, 3))


async def _run_journey(client: httpx.AsyncClient) -> None:
    """One full customer journey: browse -> menu -> order -> (edge cases) -> track."""
    request_id = str(uuid.uuid4())
    headers = {"X-Request-ID": request_id}
    user_id = random.choice(_USER_IDS)

    try:
        resp = await client.get(f"{BASE_URL}/restaurants", headers=headers)
        resp.raise_for_status()
        restaurants = resp.json()
        if not restaurants:
            return
        restaurant = random.choice(restaurants)

        await _step_delay()

        resp = await client.get(f"{BASE_URL}/restaurants/{restaurant['id']}/menu", headers=headers)
        resp.raise_for_status()
        menu = resp.json()
        if not menu:
            return

        # ~10% of journeys deliberately try to order an unavailable item
        if random.random() < 0.10:
            candidates = [item for item in menu if not item["available"]] or menu
        else:
            candidates = [item for item in menu if item["available"]] or menu
        item = random.choice(candidates)

        await _step_delay()

        order_payload = {
            "restaurant_id": restaurant["id"],
            "items": [{"menu_item_id": item["id"], "quantity": random.randint(1, 3)}],
            "user_id": user_id,
        }
        resp = await client.post(f"{BASE_URL}/orders", json=order_payload, headers=headers)
        if resp.status_code >= 400:
            return
        order = resp.json()

        # ~8% of journeys double-submit the exact same order right after (double-click)
        if random.random() < 0.08:
            await client.post(f"{BASE_URL}/orders", json=order_payload, headers=headers)

        await _step_delay()

        # ~10% of journeys cancel mid-delivery
        if random.random() < 0.10:
            await client.put(f"{BASE_URL}/orders/{order['id']}/cancel", headers=headers)
            return

        await _step_delay()
        await client.get(f"{BASE_URL}/delivery/{order['id']}", headers=headers)

    except httpx.HTTPError as exc:
        logger.warning(
            f"Journey aborted: {exc}",
            extra={"request_id": request_id, "endpoint": "traffic.journey"},
        )


async def run_traffic_simulator() -> None:
    if os.environ.get("AUTO_TRAFFIC", "true").lower() != "true":
        return

    rps = float(os.environ.get("TRAFFIC_RPS", "3"))
    interval = 1.0 / rps if rps > 0 else 1.0

    logger.info(f"Traffic simulator starting at {rps} journeys/sec", extra={"endpoint": "traffic.simulator"})

    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            asyncio.create_task(_run_journey(client))
            await asyncio.sleep(interval)
