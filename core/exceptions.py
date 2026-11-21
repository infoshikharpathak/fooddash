from __future__ import annotations


class FoodDashError(Exception):
    """Base for every FoodDash-specific business-rule failure."""


# Domain failures raised by business logic
class RestaurantOfflineError(FoodDashError):
    pass


class MenuItemUnavailableError(FoodDashError):
    pass


class InventoryConflictError(FoodDashError):
    pass


class PaymentTimeoutError(FoodDashError):
    pass


class DuplicateOrderError(FoodDashError):
    pass


class NoDriversAvailableError(FoodDashError):
    pass


class DeliveryTimeoutError(FoodDashError):
    pass


class DeliveryStatusUpdateError(FoodDashError):
    pass
