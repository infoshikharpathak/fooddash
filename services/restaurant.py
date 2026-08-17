from __future__ import annotations

from fastapi import APIRouter, HTTPException

from core.exceptions import RestaurantOfflineError
from core.failure_injection import random_failure
from core.logging_config import get_logger
from core.models import MenuItem, Restaurant

logger = get_logger("restaurant")

router = APIRouter(prefix="/restaurants", tags=["restaurant"])

_RESTAURANTS: dict[str, Restaurant] = {
    r.id: r
    for r in [
        Restaurant(
            id="rest_1",
            name="Golden Dragon",
            cuisine="Chinese",
            menu=[
                MenuItem(id="item_1", name="Kung Pao Chicken", price=12.5),
                MenuItem(id="item_2", name="Spring Rolls", price=6.0),
                MenuItem(id="item_3", name="Fried Rice", price=8.0),
            ],
        ),
        Restaurant(
            id="rest_2",
            name="Pasta Palace",
            cuisine="Italian",
            menu=[
                MenuItem(id="item_4", name="Spaghetti Carbonara", price=14.0),
                MenuItem(id="item_5", name="Margherita Pizza", price=11.0),
                MenuItem(id="item_6", name="Tiramisu", price=7.5),
            ],
        ),
        Restaurant(
            id="rest_3",
            name="Taco Fiesta",
            cuisine="Mexican",
            menu=[
                MenuItem(id="item_7", name="Beef Tacos", price=9.0),
                MenuItem(id="item_8", name="Guacamole", price=5.0),
                MenuItem(id="item_9", name="Burrito Bowl", price=10.5, available=False),
            ],
        ),
        Restaurant(
            id="rest_4",
            name="Sushi Central",
            cuisine="Japanese",
            menu=[
                MenuItem(id="item_10", name="California Roll", price=8.5),
                MenuItem(id="item_11", name="Salmon Nigiri", price=10.0),
                MenuItem(id="item_12", name="Miso Soup", price=4.0),
            ],
        ),
        Restaurant(
            id="rest_5",
            name="Burger Barn",
            cuisine="American",
            menu=[
                MenuItem(id="item_13", name="Classic Cheeseburger", price=9.5),
                MenuItem(id="item_14", name="Fries", price=3.5),
                MenuItem(id="item_15", name="Milkshake", price=5.5),
            ],
        ),
        Restaurant(
            id="rest_6",
            name="Curry House",
            cuisine="Indian",
            menu=[
                MenuItem(id="item_16", name="Butter Chicken", price=13.0),
                MenuItem(id="item_17", name="Garlic Naan", price=3.0),
                MenuItem(id="item_18", name="Samosas", price=4.5),
            ],
        ),
    ]
}


@router.get("")
@random_failure(probability=0.05, failure_type="slow_response")
async def list_restaurants() -> list[Restaurant]:
    logger.info("Listing restaurants", extra={"endpoint": "/restaurants"})
    return [r for r in _RESTAURANTS.values() if r.is_online]


@router.get("/{restaurant_id}/menu")
@random_failure(probability=0.08, failure_type="connection_error", exception=RestaurantOfflineError)
@random_failure(probability=0.04, failure_type="slow_response")
async def get_menu(restaurant_id: str) -> list[MenuItem]:
    restaurant = _RESTAURANTS.get(restaurant_id)
    if restaurant is None:
        logger.warning(
            f"Menu requested for unknown restaurant {restaurant_id}",
            extra={"endpoint": "/restaurants/{id}/menu"},
        )
        raise HTTPException(status_code=404, detail="Restaurant not found")

    logger.info(f"Fetching menu for {restaurant.name}", extra={"endpoint": "/restaurants/{id}/menu"})
    return restaurant.menu
