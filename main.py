from __future__ import annotations

import asyncio
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.exceptions import (
    ConnectionFailure,
    DeliveryStatusUpdateError,
    DeliveryTimeoutError,
    DuplicateOrderError,
    FoodDashError,
    InternalServerFailure,
    InventoryConflictError,
    MenuItemUnavailableError,
    NoDriversAvailableError,
    PaymentTimeoutError,
    RedisUnavailableError,
    RestaurantOfflineError,
    TimeoutFailure,
)
from core.logging_config import get_logger
from services import delivery, order, restaurant
from traffic.simulator import run_traffic_simulator

logger = get_logger("main")

app = FastAPI(title="FoodDash", version="0.1.0")

app.include_router(restaurant.router)
app.include_router(order.router)
app.include_router(delivery.router)

_STATUS_MAP: dict[type[Exception], int] = {
    TimeoutFailure: 504,
    ConnectionFailure: 503,
    InternalServerFailure: 500,
    RestaurantOfflineError: 503,
    MenuItemUnavailableError: 409,
    InventoryConflictError: 409,
    PaymentTimeoutError: 402,
    DuplicateOrderError: 409,
    RedisUnavailableError: 503,
    NoDriversAvailableError: 503,
    DeliveryTimeoutError: 504,
    DeliveryStatusUpdateError: 502,
}


@app.exception_handler(FoodDashError)
async def fooddash_error_handler(request: Request, exc: FoodDashError) -> JSONResponse:
    status_code = _STATUS_MAP.get(type(exc), 500)
    request_id = request.headers.get("X-Request-ID", "unknown")
    logger.error(
        f"{type(exc).__name__} on {request.url.path}: {exc}",
        extra={"request_id": request_id, "endpoint": request.url.path, "error_type": type(exc).__name__},
    )
    return JSONResponse(status_code=status_code, content={"error": type(exc).__name__, "detail": str(exc)})


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = int((time.perf_counter() - start) * 1000)

    log = logger.error if response.status_code >= 400 else logger.info
    log(
        f"{request.method} {request.url.path} -> {response.status_code}",
        extra={"request_id": request_id, "endpoint": request.url.path, "duration_ms": duration_ms},
    )
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    # asyncio only keeps a weak reference to tasks created via create_task — without
    # holding a strong reference ourselves, the task can be garbage-collected mid-run
    # (this silently killed the Redis subscriber during testing).
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@app.on_event("startup")
async def on_startup() -> None:
    logger.info("FoodDash starting up", extra={"endpoint": "startup"})
    _spawn_background(delivery.subscribe_to_orders())
    _spawn_background(run_traffic_simulator())
