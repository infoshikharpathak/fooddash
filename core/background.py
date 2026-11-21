from __future__ import annotations

import asyncio
from typing import Coroutine

_background_tasks: set[asyncio.Task] = set()


def spawn_background(coro: Coroutine) -> asyncio.Task:
    """Fire-and-forget a coroutine on the running loop.

    asyncio only keeps a weak reference to tasks created via create_task — without
    holding a strong reference ourselves, the task can be garbage-collected mid-run.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
