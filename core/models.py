from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


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
    quantity: int = 1


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


class OrderCreateRequest(BaseModel):
    restaurant_id: str
    items: list[OrderItem]
    user_id: str


class Order(BaseModel):
    id: str
    restaurant_id: str
    user_id: str
    items: list[OrderItem]
    total: float
    status: OrderStatus = OrderStatus.CREATED
    payment_status: PaymentStatus = PaymentStatus.PENDING
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
