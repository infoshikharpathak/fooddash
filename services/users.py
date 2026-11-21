from __future__ import annotations

from fastapi import APIRouter, HTTPException

from core.logging_config import get_logger
from core.models import AddressUpdateRequest, User

logger = get_logger("users")

router = APIRouter(prefix="/users", tags=["users"])

_DEMO_USERS = [
    User(id="user_1", name="Alice Carter", email="alice@example.com", address="12 Elm Street, Springfield"),
    User(id="user_2", name="Ben Okafor", email="ben@example.com", address="48 Harbor Road, Bayview"),
    User(id="user_3", name="Chloe Nguyen", email="chloe@example.com", address="7 Maple Avenue, Riverton"),
    User(id="user_4", name="Dev Mehta", email="dev@example.com", address="301 Pine Court, Lakeside"),
]

# The traffic simulator picks from user_1..user_20. Seed the rest as generic
# accounts so its journeys keep succeeding now that orders validate user_id.
_SIMULATED_USERS = [
    User(
        id=f"user_{i}",
        name=f"Simulated Customer {i}",
        email=f"customer{i}@example.com",
        address=f"{100 + i} Traffic Lane, Simville",
    )
    for i in range(len(_DEMO_USERS) + 1, 21)
]

_USERS: dict[str, User] = {u.id: u for u in _DEMO_USERS + _SIMULATED_USERS}


def get_user_or_none(user_id: str) -> User | None:
    """Plain function (not an endpoint) — used by the order service to validate user_id."""
    return _USERS.get(user_id)


@router.get("/{user_id}")
async def get_user(user_id: str) -> User:
    user = _USERS.get(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.put("/{user_id}/address")
async def update_address(user_id: str, payload: AddressUpdateRequest) -> User:
    user = _USERS.get(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.address = payload.address
    logger.info(f"Address updated for {user_id}", extra={"endpoint": "/users/{id}/address", "user_id": user_id})
    return user
