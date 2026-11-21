import pytest

pytestmark = pytest.mark.anyio


async def test_get_user(client):
    resp = await client.get("/users/user_1")
    assert resp.status_code == 200
    assert resp.json() == {
        "id": "user_1",
        "name": "Alice Carter",
        "email": "alice@example.com",
        "address": "12 Elm Street, Springfield",
        "balance": 500.0,
    }


async def test_get_unknown_user_404(client):
    resp = await client.get("/users/user_999")
    assert resp.status_code == 404


async def test_simulator_user_ids_exist(client):
    # The traffic simulator orders as user_1..user_20; all must resolve.
    for i in range(1, 21):
        assert (await client.get(f"/users/user_{i}")).status_code == 200


async def test_update_address(client):
    resp = await client.put("/users/user_2/address", json={"address": "99 New Road, Uptown"})
    assert resp.status_code == 200
    assert resp.json()["address"] == "99 New Road, Uptown"
    assert (await client.get("/users/user_2")).json()["address"] == "99 New Road, Uptown"


async def test_update_address_validation(client):
    assert (await client.put("/users/user_2/address", json={"address": ""})).status_code == 422
    assert (await client.put("/users/user_999/address", json={"address": "x"})).status_code == 404


async def test_order_snapshots_address_at_order_time(client):
    payload = {"user_id": "user_4", "restaurant_id": "rest_2", "items": [{"menu_item_id": "item_5", "quantity": 1}]}
    first = (await client.post("/orders", json=payload)).json()
    assert first["delivery_address"] == "301 Pine Court, Lakeside"

    await client.put("/users/user_4/address", json={"address": "1 Moved Street, Elsewhere"})
    payload["items"] = [{"menu_item_id": "item_6", "quantity": 1}]
    second = (await client.post("/orders", json=payload)).json()

    assert second["delivery_address"] == "1 Moved Street, Elsewhere"
    assert (await client.get(f"/orders/{first['id']}")).json()["delivery_address"] == "301 Pine Court, Lakeside"
