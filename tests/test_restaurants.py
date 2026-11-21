import pytest

pytestmark = pytest.mark.anyio


async def test_list_restaurants(client):
    resp = await client.get("/restaurants")
    assert resp.status_code == 200
    restaurants = resp.json()
    assert len(restaurants) == 6
    assert {"id", "name", "cuisine", "is_online", "menu"} <= restaurants[0].keys()
    assert all(r["is_online"] for r in restaurants)


async def test_get_menu(client):
    resp = await client.get("/restaurants/rest_1/menu")
    assert resp.status_code == 200
    menu = resp.json()
    assert [item["id"] for item in menu] == ["item_1", "item_2", "item_3"]
    assert menu[0] == {"id": "item_1", "name": "Kung Pao Chicken", "price": 12.5, "available": True}


async def test_menu_unknown_restaurant_404(client):
    resp = await client.get("/restaurants/nope/menu")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Restaurant not found"
