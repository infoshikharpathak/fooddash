from __future__ import annotations

import asyncio
import functools
import os
import random
from typing import Awaitable, Callable, TypeVar

from core.exceptions import ConnectionFailure, FoodDashError, InternalServerFailure, TimeoutFailure
from core.logging_config import get_logger

logger = get_logger("failure_injection")

# `probability` on each @random_failure call is expressed relative to this baseline,
# so FAILURE_RATE scales every injected failure proportionally: doubling FAILURE_RATE
# doubles every decorator's effective odds, setting it to 0 disables injection entirely.
_BASELINE_RATE = 0.15

_DEFAULTS: dict[str, tuple[type[Exception], str, str]] = {
    "timeout": (TimeoutFailure, "TimeoutError", "{endpoint} timed out"),
    "connection_error": (ConnectionFailure, "ConnectionError", "{endpoint} lost its connection"),
    "internal_server_error": (InternalServerFailure, "InternalServerError", "{endpoint} hit an internal error"),
}

F = TypeVar("F", bound=Callable[..., Awaitable])


def _global_failure_rate() -> float:
    return float(os.environ.get("FAILURE_RATE", _BASELINE_RATE))


def random_failure(
    probability: float,
    failure_type: str,
    exception: type[FoodDashError] | None = None,
    message: str | None = None,
):
    """Wrap a working async endpoint with a probabilistic failure.

    The wrapped function's real implementation always runs on the happy path —
    failures are layered on top, never baked into the business logic itself.
    `slow_response` never raises; it just delays and then still calls through.
    """

    def decorator(func: F) -> F:
        endpoint = func.__name__

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            effective_probability = probability * (_global_failure_rate() / _BASELINE_RATE)
            if random.random() >= effective_probability:
                return await func(*args, **kwargs)

            if failure_type == "slow_response":
                delay = random.uniform(2, 8)
                logger.warning(
                    f"Injecting slow response in {endpoint} ({delay:.1f}s)",
                    extra={"endpoint": endpoint, "duration_ms": int(delay * 1000)},
                )
                await asyncio.sleep(delay)
                return await func(*args, **kwargs)

            exc_class, error_type, default_message = _DEFAULTS.get(
                failure_type, (FoodDashError, failure_type, "{endpoint} failed")
            )
            exc_class = exception or exc_class

            if failure_type == "timeout":
                await asyncio.sleep(5)

            logger.error(
                f"Injecting {failure_type} in {endpoint}",
                extra={"endpoint": endpoint, "error_type": error_type},
            )
            raise exc_class(message or default_message.format(endpoint=endpoint))

        return wrapper

    return decorator
