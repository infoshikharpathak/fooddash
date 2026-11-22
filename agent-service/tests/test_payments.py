"""pay_for_order: the agent can request payment for an order it placed, the
model only names the order, and it hears exactly what the backend said."""

import json

from .conftest import MENU, PAY_MESSAGE, order_json, pay_json, received

ITEMS = [{"menu_item_id": "item_1", "quantity": 2}]
ORDER_AND_PAY = "Order 2 Kung Pao Chicken from Golden Dragon and pay for it."
PLAN = [
    {"tool": "get_menu", "args": {"restaurant_id": "rest_1"}},
    {"tool": "place_order", "args": {"restaurant_id": "rest_1", "items": ITEMS}},
    # The model tries to set the amount and channel itself: both are dropped.
    {"tool": "pay_for_order", "args": {"order_id": "ord-1", "amount": 0.01, "channel": "checkout"}},
    {"text": "Order ord-1 placed. Approve the $25.00 payment with the OTP in Ironclad."},
]


async def chat(client, message, session_id):
    resp = await client.post("/agent/chat", json={"message": message, "session_id": session_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_order_then_pay_sends_channel_agent_and_nothing_else(service):
    client, llm, backend = service
    backend.new_payment_status = "pending"
    llm.plans[ORDER_AND_PAY] = PLAN

    out = await chat(client, ORDER_AND_PAY, "s-pay")
    assert out["response"].startswith("Order ord-1 placed")
    pending = order_json(ITEMS, 25.0, payment_status="pending")
    assert received(llm, ORDER_AND_PAY) == [
        (True, MENU),
        (True, pending),
        (True, pay_json({**pending, "payment_request_id": "txn_pay1"}, accepted=True,
                        payment_request_id="txn_pay1", ironclad_status="pending_otp", message=PAY_MESSAGE)),
    ]
    pay = backend.requests[-1]
    assert (pay.method, pay.url.path) == ("POST", "/orders/ord-1/pay")
    assert json.loads(pay.content) == {"channel": "agent"}  # no amount, and the channel is fixed

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


async def test_agent_payments_not_allowed_reaches_the_model_clearly(service):
    client, llm, backend = service
    backend.new_payment_status = "pending"
    backend.pay_refusal = ("AGENT_PAYMENTS_NOT_ALLOWED",
                           "Ironclad refused the payment: FoodDash is not allowed to request payments from an AI agent.")
    llm.plans[ORDER_AND_PAY] = [*PLAN[:3], {"text": "Ironclad won't take agent payments for FoodDash."}]
    await chat(client, ORDER_AND_PAY, "s-refused")
    pay_result = llm.tool_results_seen(ORDER_AND_PAY)[-1]
    assert pay_result["success"] is True  # the call worked; Ironclad said no
    data = pay_result["data"]
    assert (data["accepted"], data["error"], data["payment_status"]) == (False, "AGENT_PAYMENTS_NOT_ALLOWED", "pending")
    assert "not allowed to request payments from an AI agent" in data["message"]


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
    assert "call pay_for_order" in prompt and "OTP in Ironclad" in prompt
    assert 'Never say an order is paid until get_order shows payment_status "paid"' in prompt
