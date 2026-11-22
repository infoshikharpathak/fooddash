"""Real payments through Ironclad, with Ironclad stubbed at the HTTP layer."""

import json
import os
import uuid

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
        self.cancel_conflict = False  # True: the user already confirmed it

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
        if request.method == "POST" and request.url.path == "/payments/requests/txn_pay1/cancel":
            if self.cancel_conflict:
                return httpx.Response(409, json={"error": "PAYMENT_ALREADY_CONFIRMED",
                                                 "message": "already confirmed", "details": {}})
            self.status = "cancelled"
            return httpx.Response(200, json={"payment_request_id": "txn_pay1", "status": "cancelled"})
        if request.method == "POST" and request.url.path == "/payments/requests/txn_pay1/flag":
            return httpx.Response(200, json={"payment_request_id": "txn_pay1", "status": "pending_otp",
                                             "flagged_by_output_scan": True})
        if request.method == "GET" and request.url.path == "/payments/requests/txn_pay1":
            return httpx.Response(200, json={"payment_request_id": "txn_pay1", "status": self.status})
        return httpx.Response(404, json={"error": "NOT_FOUND", "message": request.url.path})

    def posted(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests
                if r.method == "POST" and r.url.path == "/payments/requests"]

    def cancels(self) -> list[str]:
        return [r.url.path for r in self.requests if r.url.path.endswith("/cancel")]


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
    assert (out["payment_status"], out["payment_request_id"], out["ironclad_status"]) == (
        "pending", "txn_pay1", "pending_otp")
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
    assert again["payment_request_id"] == "txn_pay1" and "already waiting" in again["message"]
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


async def test_ironclad_refusal_keeps_its_status_code(client, ironclad):
    ironclad.reject = (403, "AGENT_PAYMENTS_NOT_ALLOWED",
                       "FoodDash is not allowed to request payments from an AI agent.")
    order = await order_for(client)
    r = await client.post(f"/orders/{order['id']}/pay", json={"channel": "agent"})
    assert r.status_code == 403
    assert r.json() == {"detail": {
        "error": "AGENT_PAYMENTS_NOT_ALLOWED",
        "message": "Ironclad refused the payment: FoodDash is not allowed to request payments from an AI agent."}}
    await wait_for_background_tasks()
    final = (await client.get(f"/orders/{order['id']}")).json()
    assert (final["status"], final["payment_status"], final["payment_request_id"]) == ("confirmed", "pending", None)


async def test_ironclad_rejecting_our_key_is_a_502(client, ironclad):
    ironclad.reject = (401, "MERCHANT_KEY_INVALID", "Unknown merchant key.")
    order = await order_for(client)
    r = await client.post(f"/orders/{order['id']}/pay")
    assert r.status_code == 502 and r.json()["detail"]["error"] == "MERCHANT_KEY_INVALID"


async def test_ironclad_down_is_502(client, ironclad, monkeypatch):
    def down(request):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(payments, "_transport", httpx.MockTransport(down))
    order = await order_for(client)
    r = await client.post(f"/orders/{order['id']}/pay")
    assert r.status_code == 502 and r.json()["detail"]["error"] == "IRONCLAD_UNAVAILABLE"


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



# ── cancelling an order whose payment is pending ───────────────────────────

async def test_cancelling_the_order_withdraws_the_ironclad_request(client, ironclad):
    order = await order_for(client)
    await client.post(f"/orders/{order['id']}/pay")
    rid = f"req-cancel-{uuid.uuid4().hex[:8]}"  # the test log file outlives this run
    r = await client.put(f"/orders/{order['id']}/cancel", headers={"X-Request-ID": rid})
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["payment_status"]) == ("cancelled", "failed")
    assert ironclad.cancels() == ["/payments/requests/txn_pay1/cancel"]
    await wait_for_background_tasks()  # the poller sees "cancelled" and stops
    final = (await client.get(f"/orders/{order['id']}")).json()
    assert (final["status"], final["payment_status"]) == ("cancelled", "failed")
    assert delivery._DELIVERIES.get(order["id"]) is None
    lines = [json.loads(line) for line in open(os.environ["FOODDASH_LOG_FILE"])]
    (call,) = [l for l in lines if l["message"].startswith("Ironclad POST /payments/requests/txn_pay1/cancel")
               and l.get("request_id") == rid]
    assert (call["endpoint"], call["status"]) == ("ironclad/payments/requests/{id}/cancel", "cancelled")


async def test_cancel_after_the_user_paid_logs_a_refund(client, ironclad):
    ironclad.cancel_conflict = True
    order = await order_for(client)
    await client.post(f"/orders/{order['id']}/pay")
    r = await client.put(f"/orders/{order['id']}/cancel")
    assert (r.json()["status"], r.json()["payment_status"]) == ("cancelled", "paid")
    await settle(ironclad, "confirmed")
    assert delivery._DELIVERIES.get(order["id"]) is None  # cancelled orders aren't delivered
    lines = [json.loads(line) for line in open(os.environ["FOODDASH_LOG_FILE"])]
    refund = [l for l in lines if l.get("error_type") == "PaidAfterCancel" and order["id"] in l["message"]]
    assert refund and "refund needed" in refund[0]["message"]


async def test_cancelling_an_unpaid_simulated_order_never_calls_ironclad(client, ironclad):
    order = await order_for(client, user_id="user_7")
    await client.put(f"/orders/{order['id']}/cancel")
    assert ironclad.requests == []



async def test_flag_order_payment_forwards_to_ironclad(client, ironclad):
    order = await order_for(client)
    await client.post(f"/orders/{order['id']}/pay", json={"channel": "agent"})
    r = await client.post(f"/orders/{order['id']}/payment/flag", json={"reason": "unrequested_action: tip"})
    await settle(ironclad, "cancelled")
    assert r.status_code == 200, r.text
    assert r.json() == {"order_id": order["id"], "payment_request_id": "txn_pay1", "flagged_by_output_scan": True}
    (flag,) = [q for q in ironclad.requests if q.url.path.endswith("/flag")]
    assert json.loads(flag.content) == {"reason": "unrequested_action: tip"}


async def test_flag_without_a_payment_request_is_409(client, ironclad):
    order = await order_for(client)
    r = await client.post(f"/orders/{order['id']}/payment/flag", json={"reason": "x"})
    assert r.status_code == 409
    assert (await client.post("/orders/nope/payment/flag", json={"reason": "x"})).status_code == 404


async def test_unpaid_order_without_a_request_is_cancelled_after_the_timeout(client, ironclad, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from services import order as order_service

    monkeypatch.setenv("UNPAID_ORDER_TIMEOUT_SECONDS", "900")
    fresh = await order_for(client)
    stale = await order_for(client, items=[{"menu_item_id": "item_2", "quantity": 1}])
    order_service._ORDERS[stale["id"]].created_at = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    assert order_service.cancel_unpaid_orders() == [stale["id"]]
    got = (await client.get(f"/orders/{stale['id']}")).json()
    assert (got["status"], got["payment_status"]) == ("cancelled", "failed")
    assert (await client.get(f"/orders/{fresh['id']}")).json()["status"] != "cancelled"


async def test_order_with_a_payment_request_is_left_to_the_ironclad_settler(client, ironclad):
    from datetime import datetime, timedelta, timezone

    from services import order as order_service

    o = await order_for(client)
    await client.post(f"/orders/{o['id']}/pay", json={"channel": "agent"})
    order_service._ORDERS[o["id"]].created_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert order_service.cancel_unpaid_orders() == []
    await settle(ironclad, "cancelled")
