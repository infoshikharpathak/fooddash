from __future__ import annotations

import os

import httpx

INTERNAL_BASE_URL = os.environ.get("INTERNAL_BASE_URL", "http://localhost:8000")

_app = None


def register_app(app) -> None:
    """Route service-to-service calls through the app in-process.

    Calls still go through the full ASGI stack (middleware, access logs,
    X-Request-ID propagation), so the log stream looks exactly like real
    service-to-service traffic — but there is no network hop, so nothing
    depends on the server listening on a port (and tests work under an
    in-memory ASGI client).
    """
    global _app
    _app = app


def internal_client(timeout: float) -> httpx.AsyncClient:
    if _app is None:
        return httpx.AsyncClient(base_url=INTERNAL_BASE_URL, timeout=timeout)
    # raise_app_exceptions=False: an unhandled error inside the callee becomes a
    # 500 response, same as it would over a real socket.
    transport = httpx.ASGITransport(app=_app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://fooddash.internal", timeout=timeout)
