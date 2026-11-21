import pytest

from tests.conftest import wait_for_background_tasks

pytestmark = pytest.mark.anyio


async def test_delivery_lifecycle_runs_to_completion(client):
    payload = {"user_id": "user_3", "restaurant_id": "rest_5", "items": [{"menu_item_id": "item_13", "quantity": 1}]}
    order = (await client.post("/orders", json=payload)).json()

    await wait_for_background_tasks()

    delivery = await client.get(f"/delivery/{order['id']}")
    assert delivery.status_code == 200
    body = delivery.json()
    assert body["status"] == "delivered"
    assert body["driver_id"].startswith("driver_")

    final_order = (await client.get(f"/orders/{order['id']}")).json()
    assert final_order["status"] == "delivered"

    stats = (await client.get("/stats")).json()
    assert stats["orders_by_status"] == {"delivered": 1}
    assert stats["deliveries_by_status"] == {"delivered": 1}


async def test_unknown_delivery_404(client):
    resp = await client.get("/delivery/nope")
    assert resp.status_code == 404
