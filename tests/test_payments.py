"""Real payments through Ironclad, with Ironclad stubbed at the HTTP layer."""

import json
import os

import httpx
import pytest

from services import delivery, payments
from tests.conftest import wait_for_background_tasks

pytestmark = pytest.mark.anyio

KEY = "mk_test_fooddash"
MENU_ORDER = {"restaurant_id": "rest_1", "items": [{"menu_item_id": "item_1", "quantity": 2}]}  # $25.00


class FakeIronclad:
    """POST/GET /payments/requests. `status` is what GET reports next."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status = "pending_otp"
        self.reject: tuple[int, str, str] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == f"Bearer {KEY}"
        if request.method == "POST" and request.url.path == "/payments/requests":
            if self.reject:
                code, err, msg = self.reject
                return httpx.Response(code, json={"error": err, "message": msg, "details": {}})
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "payment_request_id": "txn_pay1", "status": "pending_otp", "amount": body["amount"],
                "merchant_display_name": "FoodDash", "order_ref": body["order_ref"], "channel": body["channel"],
                "expires_at": "2026-11-22T00:05:00+00:00", "idempotent_replay": False,
            })
        if request.method == "GET" and request.url.path == "/payments/requests/txn_pay1":
            return httpx.Response(200, json={"payment_request_id": "txn_pay1", "status": self.status})
        return httpx.Response(404, json={"error": "NOT_FOUND", "message": request.url.path})

    def posted(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]


@pytest.fixture
def ironclad(monkeypatch):
    fake = FakeIronclad()
    monkeypatch.setenv("IRONCLAD_PAYMENTS_ENABLED", "true")
    monkeypatch.setenv("IRONCLAD_MERCHANT_KEY", KEY)
    monkeypatch.setenv("IRONCLAD_BASE_URL", "http://ironclad.test")
    monkeypatch.setenv("IRONCLAD_POLL_SECONDS", "0.01")
    monkeypatch.setattr(payments, "_transport", httpx.MockTransport(fake))
    return fake


async def order_for(client, user_id="user_1", **extra):
    resp = await client.post("/orders", json={**MENU_ORDER, "user_id": user_id, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def settle(fake, status):
    fake.status = status
    await wait_for_background_tasks()


# ── standalone mode ────────────────────────────────────────────────────────

async def test_disabled_payments_behave_as_before(client, monkeypatch):
    monkeypatch.delenv("IRONCLAD_PAYMENTS_ENABLED", raising=False)
    order = await order_for(client)
    assert order["payment_status"] == "paid" and order["payment_request_id"] is None
    await wait_for_background_tasks()
    assert (await client.get(f"/delivery/{order['id']}")).json()["status"] == "delivered"
    r = await client.post(f"/orders/{order['id']}/pay")
    assert r.status_code == 409


async def test_simulated_users_never_call_ironclad(client, ironclad):
    for user in ("user_5", "user_12", "user_20", "user_2"):
        order = await order_for(client, user_id=user)
        assert order["payment_status"] == "paid"
        assert (await client.post(f"/orders/{order['id']}/pay")).status_code == 409
    await wait_for_background_tasks()
    assert ironclad.requests == []


# ── checkout path ──────────────────────────────────────────────────────────

async def test_checkout_path_pays_then_delivers(client, ironclad):
    order = await order_for(client)
    assert (order["payment_status"], order["status"]) == ("pending", "confirmed")
    await wait_for_background_tasks()
    assert (await client.get(f"/delivery/{order['id']}")).status_code == 404  # delivery waits for payment

    r = await client.post(f"/orders/{order['id']}/pay", headers={"X-Request-ID": "req-pay-1"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["accepted"], out["payment_status"], out["payment_request_id"], out["ironclad_status"]) == (
        True, "pending", "txn_pay1", "pending_otp")
    assert "OTP in Ironclad" in out["message"]
    (sent,) = ironclad.posted()
    assert sent == {"order_ref": order["id"], "amount": 25.0, "user_id": "demo_user", "channel": "checkout",
                    "idempotency_key": f"fooddash-order-{order['id']}", "description": f"FoodDash order {order['id'][:8]}"}
    assert (await client.get(f"/orders/{order['id']}")).json()["payment_request_id"] == "txn_pay1"
    assert delivery._DELIVERIES.get(order["id"]) is None  # still waiting for the OTP

    await settle(ironclad, "confirmed")
    final = (await client.get(f"/orders/{order['id']}")).json()
    assert (final["payment_status"], final["status"]) == ("paid", "delivered")
    assert (await client.get(f"/delivery/{order['id']}")).json()["status"] == "delivered"


async def test_agent_channel_is_sent_to_ironclad(client, ironclad):
    order = await order_for(client)
    r = await client.post(f"/orders/{order['id']}/pay", json={"channel": "agent"})
    assert r.json()["channel"] == "agent"
    assert ironclad.posted()[0]["channel"] == "agent"
    await settle(ironclad, "confirmed")


async def test_amount_is_always_the_order_total(client, ironclad):
    order = await order_for(client)
    r = await client.post(f"/orders/{order['id']}/pay", json={"channel": "checkout", "amount": 0.01})
    assert r.status_code == 422  # the caller can't send an amount at all
    assert ironclad.requests == []
    await client.post(f"/orders/{order['id']}/pay")
    assert ironclad.posted()[0]["amount"] == order["total"] == 25.0
    await settle(ironclad, "confirmed")


async def test_paying_twice_reuses_the_request(client, ironclad):
    order = await order_for(client)
    await client.post(f"/orders/{order['id']}/pay")
    again = (await client.post(f"/orders/{order['id']}/pay", json={"channel": "agent"})).json()
    assert again["accepted"] is True and again["payment_request_id"] == "txn_pay1"
    assert len(ironclad.posted()) == 1
    await settle(ironclad, "confirmed")
    assert (await client.post(f"/orders/{order['id']}/pay")).json()["message"] == "This order is already paid."


@pytest.mark.parametrize("outcome", ["expired", "cancelled"])
async def test_expired_or_cancelled_payment_cancels_the_order(client, ironclad, outcome):
    order = await order_for(client)
    await client.post(f"/orders/{order['id']}/pay")
    await settle(ironclad, outcome)
    final = (await client.get(f"/orders/{order['id']}")).json()
    assert (final["payment_status"], final["status"]) == ("failed", "cancelled")
    assert delivery._DELIVERIES.get(order["id"]) is None
    assert (await client.post(f"/orders/{order['id']}/pay")).status_code == 409


async def test_agent_payments_not_allowed_is_a_clear_result(client, ironclad):
    ironclad.reject = (403, "AGENT_PAYMENTS_NOT_ALLOWED",
                       "FoodDash is not allowed to request payments from an AI agent.")
    order = await order_for(client)
    r = await client.post(f"/orders/{order['id']}/pay", json={"channel": "agent"})
    assert r.status_code == 200
    out = r.json()
    assert (out["accepted"], out["error"], out["payment_status"], out["payment_request_id"]) == (
        False, "AGENT_PAYMENTS_NOT_ALLOWED", "pending", None)
    assert "not allowed to request payments from an AI agent" in out["message"]
    await wait_for_background_tasks()
    assert (await client.get(f"/orders/{order['id']}")).json()["status"] == "confirmed"  # still payable


async def test_ironclad_down_is_502(client, ironclad, monkeypatch):
    def down(request):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(payments, "_transport", httpx.MockTransport(down))
    order = await order_for(client)
    assert (await client.post(f"/orders/{order['id']}/pay")).status_code == 502


async def test_outgoing_ironclad_call_is_logged(client, ironclad):
    order = await order_for(client)
    await client.post(f"/orders/{order['id']}/pay", headers={"X-Request-ID": "req-log-1"})
    await settle(ironclad, "confirmed")
    lines = [json.loads(line) for line in open(os.environ["FOODDASH_LOG_FILE"])]
    post = [l for l in lines if l["message"].startswith("Ironclad POST /payments/requests")][-1]
    assert post["service_name"] == "payments" and post["request_id"] == "req-log-1"
    assert (post["order_id"], post["payment_request_id"], post["status"]) == (order["id"], "txn_pay1", "pending_otp")
    assert isinstance(post["duration_ms"], int) and post["endpoint"] == "ironclad/payments/requests"
    polled = [l for l in lines if l["message"].startswith("Ironclad GET") and l.get("order_id") == order["id"]]
    assert polled and polled[-1]["status"] == "confirmed"
