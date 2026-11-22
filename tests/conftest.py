from __future__ import annotations

import asyncio
import copy
import os
import tempfile

# Configure before the app is imported: no traffic simulator, instant delivery
# stages, and test logs kept out of the /tmp/fooddash.log stream LogScribe tails.
os.environ["AUTO_TRAFFIC"] = "false"
os.environ["DELIVERY_STAGE_DELAY_MIN"] = "0"
os.environ["DELIVERY_STAGE_DELAY_MAX"] = "0"
os.environ.setdefault("FOODDASH_LOG_FILE", os.path.join(tempfile.gettempdir(), "fooddash-tests.log"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from core import background  # noqa: E402
from main import app  # noqa: E402
from services import delivery, order, users  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def wait_for_background_tasks(timeout: float = 5.0) -> None:
    """Await every in-flight background task (delivery lifecycles) to completion."""
    # Tasks can spawn tasks (a settled payment starts the delivery), so keep
    # going until none are left.
    async def drain():
        while background._background_tasks:
            await asyncio.gather(*set(background._background_tasks))

    await asyncio.wait_for(drain(), timeout=timeout)


@pytest.fixture(autouse=True)
def reset_state():
    """Every test starts from the seeded in-memory state."""
    saved_users = copy.deepcopy(users._USERS)
    order._ORDERS.clear()
    order._recent_signatures.clear()
    delivery._DELIVERIES.clear()
    yield
    users._USERS.clear()
    users._USERS.update(saved_users)


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await wait_for_background_tasks()
