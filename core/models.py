from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class User(BaseModel):
    id: str
    name: str
    email: str
    address: str
    balance: float = 500.0


class AddressUpdateRequest(BaseModel):
    address: str = Field(min_length=1)


class MenuItem(BaseModel):
    id: str
    name: str
    price: float
    available: bool = True


class Restaurant(BaseModel):
    id: str
    name: str
    cuisine: str
    is_online: bool = True
    menu: list[MenuItem] = Field(default_factory=list)


class OrderItem(BaseModel):
    menu_item_id: str
    quantity: int = Field(default=1, ge=1)


class OrderStatus(str, Enum):
    CREATED = "created"
    CONFIRMED = "confirmed"
    PREPARING = "preparing"
    READY = "ready"
    PICKED_UP = "picked_up"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"


class PayRequest(BaseModel):
    """Body of POST /orders/{id}/pay. The amount is never taken from the
    caller: it's the order's stored total."""

    model_config = ConfigDict(extra="forbid")
    channel: Literal["checkout", "agent"] = "checkout"


class PayResponse(BaseModel):
    order_id: str
    payment_status: PaymentStatus
    payment_request_id: str | None = None
    amount: float
    channel: str
    ironclad_status: str | None = None
    """pending_otp | confirmed | cancelled | expired, as Ironclad reports it."""
    merchant_display_name: str | None = None
    expires_at: str | None = None
    message: str


class OrderCreateRequest(BaseModel):
    restaurant_id: str
    items: list[OrderItem] = Field(min_length=1)
    user_id: str


class Order(BaseModel):
    id: str
    restaurant_id: str
    user_id: str
    items: list[OrderItem]
    total: float
    status: OrderStatus = OrderStatus.CREATED
    payment_status: PaymentStatus = PaymentStatus.PENDING
    # Ironclad payment request for this order (real payments only).
    payment_request_id: str | None = None
    delivery_address: str
    created_at: str
    request_id: str | None = None


class DeliveryStatus(str, Enum):
    ASSIGNED = "assigned"
    PICKED_UP = "picked_up"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    FAILED = "failed"


class Delivery(BaseModel):
    order_id: str
    status: DeliveryStatus = DeliveryStatus.ASSIGNED
    driver_id: str
    eta_seconds: int
