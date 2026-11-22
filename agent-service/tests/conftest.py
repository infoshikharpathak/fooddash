"""Stubbed LLM and food backend for agent-service tests. Anvil, Agent Forge
and bastion are real; only the model and the backend's HTTP API are fake."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import httpx
import pytest
from bastion.classifier import is_classifier_request
from openai.types.chat import ChatCompletion

os.environ.setdefault("OPENAI_API_KEY", "dummy-key-for-tests")
for var in ("OPENAI_MODEL", "FOODDASH_BASE_URL", "BASTION_URL", "BASTION_KEY"):
    os.environ.pop(var, None)


def _completion(message: dict, finish: str) -> ChatCompletion:
    return ChatCompletion.model_validate({
        "id": "c", "object": "chat.completion", "created": 0, "model": "stub",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })


class StubLLM:
    """OpenAI-compatible stub. *plans* maps a user message to the steps the
    model takes for it: {"tool": name, "args": {...}} or {"text": "..."}.
    The step is picked from the conversation the agent actually sent (number
    of assistant turns since that user message), so concurrent sessions
    never interfere, and each request is recorded for history checks."""

    def __init__(self, plans: dict[str, list[dict]]):
        self.plans = plans
        self.requests: list[list] = []
        self.models: list[str] = []
        self.flag_words: list[str] = []  # the classifier flags text containing any of these
        self.label_words: dict[str, str] = {}  # word -> output label (unrequested_action, ...)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        msgs = kwargs["messages"]
        if is_classifier_request(msgs):  # Anvil's injection scan, via the SDK
            text = _content(msgs[-1]) or ""
            label = next((lbl for w, lbl in self.label_words.items() if w in text), None)
            if label:
                return _completion({"role": "assistant", "content": json.dumps({"label": label, "reason": "stub"})},
                                   "stop")
            hit = next((w for w in self.flag_words if w in text), None)
            verdict = {"injection": True, "pattern": f"stub flag: {hit}"} if hit else {"injection": False}
            return _completion({"role": "assistant", "content": json.dumps(verdict)}, "stop")
        self.requests.append(msgs)
        self.models.append(kwargs.get("model"))
        last_user = max(i for i, m in enumerate(msgs) if _role(m) == "user")
        user_text = _content(msgs[last_user])
        done = sum(1 for m in msgs[last_user + 1:] if _role(m) == "assistant")
        step = self.plans[user_text][done]
        if "tool" in step:
            return _completion({"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_{done}", "type": "function",
                "function": {"name": step["tool"], "arguments": json.dumps(step.get("args", {}))},
            }]}, "tool_calls")
        return _completion({"role": "assistant", "content": step["text"]}, "stop")

    def tool_results_seen(self, user_text: str) -> list[dict]:
        """Tool results the model was given while answering *user_text*."""
        out = []
        for msgs in self.requests:
            idx = [i for i, m in enumerate(msgs) if _role(m) == "user" and _content(m) == user_text]
            if not idx:
                continue
            for m in msgs[idx[-1] + 1:]:
                if _role(m) == "tool":
                    r = json.loads(m["content"])
                    if r not in out:
                        out.append(r)
        return out


def _role(m):
    return m.get("role") if isinstance(m, dict) else getattr(m, "role", None)


def _content(m):
    return m.get("content") if isinstance(m, dict) else getattr(m, "content", None)


# Shapes copied from the real FoodDash backend. The list endpoints return
# bare JSON arrays, which is exactly what the agent must receive.
MENU = [
    {"id": "item_1", "name": "Kung Pao Chicken", "price": 12.5, "available": True},
    {"id": "item_2", "name": "Spring Rolls", "price": 6.0, "available": True},
]
RESTAURANTS = [
    {"id": "rest_1", "name": "Golden Dragon", "cuisine": "Chinese", "is_online": True, "menu": MENU},
]
USER = {"id": "user_1", "name": "Alice Carter", "email": "alice@example.com",
        "address": "12 Elm Street, Springfield", "balance": 500.0}


PAY_MESSAGE = ("Payment of $25.00 requested. The user must approve it with the OTP in Ironclad; "
               "the order is not paid until then.")


def pay_json(order, *, message, payment_request_id=None, ironclad_status=None):
    """Shape of the backend's PayResponse."""
    return {"order_id": order["id"], "payment_status": order["payment_status"],
            "payment_request_id": payment_request_id, "amount": order["total"], "channel": "agent",
            "ironclad_status": ironclad_status, "merchant_display_name": "FoodDash",
            "expires_at": None, "message": message}


def order_json(items, total, status="confirmed", payment_status="paid"):
    return {
        "id": "ord-1", "restaurant_id": "rest_1", "user_id": "user_1", "items": items, "total": total,
        "status": status, "payment_status": payment_status, "payment_request_id": None,
        "delivery_address": USER["address"],
        "created_at": "2026-11-22T00:00:00+00:00", "request_id": None,
    }


class StubBackend:
    """The FoodDash backend endpoints the agent's tools hit."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.orders: list[dict] = []
        # (status, error, message) to make POST /orders/{id}/pay fail as the backend does
        self.pay_refusal: tuple[int, str, str] | None = None
        self.new_payment_status = "paid"  # "pending" when FoodDash takes real payments

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path
        if (method, path) == ("GET", "/restaurants"):
            return httpx.Response(200, json=RESTAURANTS)
        if (method, path) == ("GET", "/restaurants/rest_1/menu"):
            return httpx.Response(200, json=MENU)
        if (method, path) == ("GET", "/users/user_1"):
            return httpx.Response(200, json=USER)
        if (method, path) == ("POST", "/orders"):
            body = json.loads(request.content)
            items = [{"menu_item_id": i["menu_item_id"], "quantity": i.get("quantity", 1)} for i in body["items"]]
            prices = {i["id"]: i["price"] for i in MENU}
            order = order_json(items, sum(prices[i["menu_item_id"]] * i["quantity"] for i in items),
                               payment_status=self.new_payment_status)
            self.orders.append(order)
            return httpx.Response(201, json=order)
        if (method, path) == ("GET", "/orders"):
            return httpx.Response(200, json=list(reversed(self.orders)))
        if (method, path) == ("POST", "/orders/ord-1/pay"):
            order = self.orders[-1]
            if self.pay_refusal:
                status, code, message = self.pay_refusal
                return httpx.Response(status, json={"detail": {"error": code, "message": message}})
            order["payment_request_id"] = "txn_pay1"
            return httpx.Response(200, json=pay_json(order, payment_request_id="txn_pay1",
                                                     ironclad_status="pending_otp", message=PAY_MESSAGE))
        if (method, path) == ("POST", "/orders/ord-1/payment/flag"):
            return httpx.Response(200, json={"order_id": "ord-1", "payment_request_id": "txn_pay1",
                                             "flagged_by_output_scan": True})
        if (method, path) == ("PUT", "/users/user_1/address"):
            body = json.loads(request.content)
            return httpx.Response(200, json={**USER, "address": body["address"]})
        if method == "PUT" and path.startswith("/orders/") and path.endswith("/cancel"):
            oid = path.split("/")[2]
            order = next((o for o in self.orders if o["id"] == oid), None)
            if order is None:
                return httpx.Response(404, json={"detail": "Order not found"})
            order["status"] = "cancelled"
            return httpx.Response(200, json=order)
        if (method, path) == ("GET", "/orders/ord-1"):
            return httpx.Response(200, json={**self.orders[-1], "status": "picked_up"})
        return httpx.Response(404, json={"detail": f"stub backend has no {method} {path}"})

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]


def received(llm: "StubLLM", user_text: str) -> list[tuple[bool, object]]:
    """(success, data) of each tool result the model was given for *user_text*."""
    return [(r["success"], r["data"]) for r in llm.tool_results_seen(user_text)]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FORGE_DB_PATH", str(tmp_path / "forge.db"))
    monkeypatch.chdir(tmp_path)  # local traces land here
    yield
    from agent_forge.tools import _REGISTRY, sources

    for name in [n for n, fn in _REGISTRY.items() if getattr(fn, "_tool_source", None) is not None]:
        _REGISTRY.pop(name)
    sources._SOURCE_TOOLS.clear()


@pytest.fixture
async def service():
    """(client, llm, backend): the real FastAPI app on a Platform wired to stubs."""
    import app.main as main

    llm, backend = StubLLM({}), StubBackend()
    platform = main.build_platform(openai_client=llm, http_transport=httpx.MockTransport(backend))
    await platform.start()
    main.app.state.platform = platform
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
        yield client, llm, backend
    await platform.close()
