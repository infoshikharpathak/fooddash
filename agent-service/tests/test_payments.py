"""pay_for_order: the agent can request payment for an order it placed, the
model only names the order, and it hears exactly what the backend said."""

import json

import pytest

from .conftest import MENU, PAY_MESSAGE, order_json, pay_json, received

ITEMS = [{"menu_item_id": "item_1", "quantity": 2}]
ORDER_AND_PAY = "Order 2 Kung Pao Chicken from Golden Dragon and pay for it."
PLAN = [
    {"tool": "get_menu", "args": {"restaurant_id": "rest_1"}},
    {"tool": "place_order", "args": {"restaurant_id": "rest_1", "items": ITEMS}},
    {"tool": "pay_for_order", "args": {"order_id": "ord-1"}},
    {"text": "Order ord-1 placed. Approve the $25.00 payment with the OTP in Ironclad."},
]


async def chat(client, message, session_id):
    resp = await client.post("/agent/chat", json={"message": message, "session_id": session_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_order_then_pay_sends_channel_agent(service):
    client, llm, backend = service
    backend.new_payment_status = "pending"
    llm.plans[ORDER_AND_PAY] = PLAN

    out = await chat(client, ORDER_AND_PAY, "s-pay")
    assert out["response"].startswith("Order ord-1 placed")
    pending = order_json(ITEMS, 25.0, payment_status="pending")
    assert received(llm, ORDER_AND_PAY) == [
        (True, MENU),
        (True, pending),
        (True, pay_json({**pending, "payment_request_id": "txn_pay1"},
                        payment_request_id="txn_pay1", ironclad_status="pending_otp", message=PAY_MESSAGE)),
    ]
    pay = backend.requests[-1]
    assert (pay.method, pay.url.path) == ("POST", "/orders/ord-1/pay")
    assert json.loads(pay.content) == {"channel": "agent"}  # filled in by the service, not the model

    (trace,) = [t for t in (await client.get("/agent/traces/s-pay")).json()["traces"] if t["tool"] == "pay_for_order"]
    assert (trace["decision"], trace["parameters"]) == ("allowed", {"order_id": "ord-1", "channel": "agent"})


async def test_pay_for_order_requires_place_order_first(service):
    client, llm, backend = service
    msg = "Pay for order ord-1."
    llm.plans[msg] = [{"tool": "pay_for_order", "args": {"order_id": "ord-1"}},
                      {"text": "I can only pay for an order I placed in this conversation."}]
    await chat(client, msg, "s-nopay")
    (seen,) = llm.tool_results_seen(msg)
    assert seen["success"] is False and seen["data"] is None
    assert seen["correction"]["violation"] == "missing_prerequisite"
    assert seen["correction"]["required_actions"] == ["place_order"]
    assert backend.calls() == []


@pytest.mark.parametrize("extra", [{"amount": 0.01}, {"channel": "checkout"}, {"channel": "agent"}])
async def test_model_cannot_send_params_beyond_order_id(service, extra):
    """The schema declares only order_id: an amount, or a channel (even the
    right one), from the model is blocked before the backend sees it."""
    client, llm, backend = service
    backend.new_payment_status = "pending"
    llm.plans[ORDER_AND_PAY] = [*PLAN[:2], {"tool": "pay_for_order", "args": {"order_id": "ord-1", **extra}},
                                {"text": "That didn't work."}]
    await chat(client, ORDER_AND_PAY, "s-extra")
    seen = llm.tool_results_seen(ORDER_AND_PAY)[-1]
    assert seen["success"] is False and seen["correction"]["violation"] == "invalid_params"
    (field,) = extra
    assert f"{field}: unknown field" in seen["correction"]["message"]
    assert ("POST", "/orders/ord-1/pay") not in backend.calls()
    (trace,) = [t for t in (await client.get("/agent/traces/s-extra")).json()["traces"] if t["tool"] == "pay_for_order"]
    assert (trace["decision"], trace["violation"]) == ("blocked", "invalid_params")


async def test_agent_payments_not_allowed_reaches_the_model_with_code_and_message(service):
    client, llm, backend = service
    backend.new_payment_status = "pending"
    backend.pay_refusal = (403, "AGENT_PAYMENTS_NOT_ALLOWED",
                           "Ironclad refused the payment: FoodDash is not allowed to request payments from an AI agent.")
    llm.plans[ORDER_AND_PAY] = [*PLAN[:3], {"text": "Ironclad won't take agent payments for FoodDash."}]
    await chat(client, ORDER_AND_PAY, "s-refused")
    seen = llm.tool_results_seen(ORDER_AND_PAY)[-1]
    assert seen["success"] is False and seen["correction"]["violation"] == "upstream_error"
    assert "HTTP 403" in seen["correction"]["message"]
    assert seen["data"] == {"http_status": 403, "body": {"detail": {
        "error": "AGENT_PAYMENTS_NOT_ALLOWED",
        "message": "Ironclad refused the payment: FoodDash is not allowed to request payments from an AI agent."}}}
    (trace,) = [t for t in (await client.get("/agent/traces/s-refused")).json()["traces"] if t["tool"] == "pay_for_order"]
    assert (trace["decision"], trace["result"], trace["violation"]) == ("blocked", "blocked", "upstream_error")
    assert trace["http_status"] == 403


async def test_get_order_is_what_says_paid(service):
    client, llm, backend = service
    backend.new_payment_status = "pending"
    llm.plans[ORDER_AND_PAY] = PLAN
    await chat(client, ORDER_AND_PAY, "s-status")
    backend.orders[-1]["payment_status"] = "paid"  # the user approved the OTP in Ironclad
    msg = "Is it paid?"
    llm.plans[msg] = [{"tool": "get_order", "args": {"order_id": "ord-1"}}, {"text": "Yes, it's paid."}]
    await chat(client, msg, "s-status")
    ((ok, data),) = received(llm, msg)
    assert ok and data["payment_status"] == "paid"


async def test_instructions_tell_the_agent_to_pay_and_never_claim_paid(service):
    client, llm, _ = service
    llm.plans["hi"] = [{"text": "Hi!"}]
    await chat(client, "hi", "s-prompt")
    prompt = llm.requests[-1][0]["content"]
    assert "call pay_for_order" in prompt and "the system adds the approval line" in prompt
    assert "never ask the user for a code" in prompt
    assert 'Never say an order is paid until get_order shows payment_status "paid"' in prompt
