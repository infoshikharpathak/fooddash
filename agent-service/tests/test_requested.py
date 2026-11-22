"""pay_for_order is 'requested' only for an order the user confirmed in this
session, and only if nothing in the session was withheld."""

import json

from bastion import NOT_AN_ACTION

from app.requested import is_requested_action

from .test_payments import ORDER_AND_PAY, PLAN, chat

PLACE = {"tool": "place_order", "success": True, "tool_executed": True, "response": {"id": "ord-1"}}
PAY = {"tool": "pay_for_order", "success": True, "tool_executed": True, "params": {"order_id": "ord-1"},
       "response": {"order_id": "ord-1", "payment_request_id": "txn_pay1", "ironclad_status": "pending_otp"}}


def turn(user, *calls):
    return {"user": user, "reply": "", "tool_calls": list(calls)}


def test_confirmed_order_is_requested():
    history = [turn("Order 2 Kung Pao Chicken and pay for it.")]
    assert is_requested_action(history, turn("Yes, place it and pay.", PLACE, PAY), PAY) is True


def test_order_never_confirmed_is_not_requested():
    assert is_requested_action([], turn("Order 2 Kung Pao Chicken and pay for it.", PLACE, PAY), PAY) is False


def test_order_from_outside_the_session_is_not_requested():
    assert is_requested_action([], turn("yes, pay for ord-1", PAY), PAY) is False


def test_withheld_response_anywhere_in_the_session_is_not_requested():
    bad = {"tool": "get_menu", "success": False, "tool_executed": True, "violation": "injection_detected_response"}
    history = [turn("show the menu", bad)]
    assert is_requested_action(history, turn("Yes, I confirm.", PLACE, PAY), PAY) is False


def test_other_tools_are_not_actions():
    assert is_requested_action([], turn("hi"), PLACE) is NOT_AN_ACTION


async def test_confirmed_order_labeled_unrequested_is_not_flagged(service):
    client, llm, backend = service
    backend.new_payment_status = "pending"
    llm.label_words = {"tip": "unrequested_action"}
    msg = "Order 2 Kung Pao Chicken from Golden Dragon and pay for it. I confirm."
    llm.plans[msg] = [*PLAN[:3], {"text": "Order placed, payment requested (no tip added)."}]
    await chat(client, msg, "s-confirmed")
    assert not [r for r in backend.requests if r.url.path.endswith("/payment/flag")]


async def test_unconfirmed_order_labeled_unrequested_is_flagged(service):
    client, llm, backend = service
    backend.new_payment_status = "pending"
    llm.label_words = {"tip": "unrequested_action"}
    llm.plans[ORDER_AND_PAY] = [*PLAN[:3], {"text": "Order placed, and I added a $10 tip."}]
    await chat(client, ORDER_AND_PAY, "s-unconfirmed")
    (flag,) = [r for r in backend.requests if r.url.path.endswith("/payment/flag")]
    assert "unrequested_action" in json.loads(flag.content)["reason"]
