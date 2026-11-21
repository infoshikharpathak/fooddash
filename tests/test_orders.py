import pytest

from services import delivery
from tests.conftest import wait_for_background_tasks

pytestmark = pytest.mark.anyio


def order_payload(user_id="user_1", restaurant_id="rest_1", items=None):
    return {
        "user_id": user_id,
        "restaurant_id": restaurant_id,
        "items": items or [{"menu_item_id": "item_1", "quantity": 2}, {"menu_item_id": "item_2", "quantity": 1}],
    }


async def test_create_order(client):
    resp = await client.post("/orders", json=order_payload(), headers={"X-Request-ID": "req-123"})
    assert resp.status_code == 201
    order = resp.json()
    assert order["status"] == "confirmed"
    assert order["total"] == 31.0  # 2 x 12.5 + 6.0
    assert order["payment_status"] == "paid"
    assert order["delivery_address"] == "12 Elm Street, Springfield"
    assert order["request_id"] == "req-123"

    fetched = await client.get(f"/orders/{order['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == order["id"]


async def test_create_order_unknown_user_404(client):
    resp = await client.post("/orders", json=order_payload(user_id="user_999"))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "User not found"


async def test_create_order_unknown_restaurant_404(client):
    resp = await client.post("/orders", json=order_payload(restaurant_id="nope"))
    assert resp.status_code == 404


async def test_duplicate_order_rejected(client):
    first = await client.post("/orders", json=order_payload())
    assert first.status_code == 201

    second = await client.post("/orders", json=order_payload())
    assert second.status_code == 409
    assert second.json()["error"] == "DuplicateOrderError"

    # Same items from a different user is a separate, legitimate order.
    other_user = await client.post("/orders", json=order_payload(user_id="user_2"))
    assert other_user.status_code == 201


async def test_unavailable_item_rejected(client):
    # item_9 (Burrito Bowl at Taco Fiesta) is seeded as unavailable.
    resp = await client.post(
        "/orders", json=order_payload(restaurant_id="rest_3", items=[{"menu_item_id": "item_9", "quantity": 1}])
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "InventoryConflictError"
    assert (await client.get("/orders")).json() == []


async def test_cancel_order(client, monkeypatch):
    # Give the delivery lifecycle a beat before pickup so the cancel lands first.
    monkeypatch.setattr(delivery, "_STAGE_DELAY_MIN", 0.2)
    monkeypatch.setattr(delivery, "_STAGE_DELAY_MAX", 0.2)

    order = (await client.post("/orders", json=order_payload())).json()
    resp = await client.put(f"/orders/{order['id']}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # The in-flight delivery must not revive the order; it stops instead.
    await wait_for_background_tasks()
    assert (await client.get(f"/orders/{order['id']}")).json()["status"] == "cancelled"
    assert (await client.get(f"/delivery/{order['id']}")).json()["status"] == "failed"

    again = await client.put(f"/orders/{order['id']}/cancel")
    assert again.status_code == 400


async def test_cancel_unknown_order_404(client):
    resp = await client.put("/orders/nope/cancel")
    assert resp.status_code == 404
