from __future__ import annotations


class FoodDashError(Exception):
    """Base for every FoodDash-specific failure — both injected and organic business-rule violations."""


# Generic failures raised by the failure_injection decorator (timing/shape driven, not business-specific)
class TimeoutFailure(FoodDashError):
    pass


class ConnectionFailure(FoodDashError):
    pass


class InternalServerFailure(FoodDashError):
    pass


# Domain-specific failures raised either by the decorator (with exception= override) or directly by business logic
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


class RedisUnavailableError(FoodDashError):
    pass


class NoDriversAvailableError(FoodDashError):
    pass


class DeliveryTimeoutError(FoodDashError):
    pass


class DeliveryStatusUpdateError(FoodDashError):
    pass
