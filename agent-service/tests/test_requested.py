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


# ── pre-call gate: update_address and cancel_order (no OTP behind them) ────

from app.requested import ADDRESS_MESSAGE, CANCEL_MESSAGE, address_requested  # noqa: E402

from .conftest import order_json  # noqa: E402


def gated(llm, msg):
    """The correction the model got for the gated call, if any."""
    return [r["correction"] for r in llm.tool_results_seen(msg) if r.get("correction")]


def puts(backend, suffix):
    return [r for r in backend.requests if r.method == "PUT" and r.url.path.endswith(suffix)]


async def test_address_the_user_wrote_is_allowed(service):
    client, llm, backend = service
    msg = "Change my address to 12 Oak St."
    llm.plans[msg] = [{"tool": "update_address", "args": {"address": "12 oak  st"}}, {"text": "Updated."}]
    await chat(client, msg, "s-addr-ok")
    assert len(puts(backend, "/address")) == 1 and gated(llm, msg) == []


async def test_address_the_user_never_wrote_is_blocked(service):
    client, llm, backend = service
    msg = "My deliveries keep going to the wrong place."
    llm.plans[msg] = [{"tool": "update_address", "args": {"address": "99 Dock Rd"}}, {"text": "Hmm."}]
    await chat(client, msg, "s-addr-bad")
    assert puts(backend, "/address") == []
    (correction,) = gated(llm, msg)
    assert correction["violation"] == "unrequested_action" and correction["message"] == ADDRESS_MESSAGE


async def test_address_the_agent_proposed_and_the_user_confirmed_is_allowed(service):
    client, llm, backend = service
    first, second = "My address changed, it's the new place on Oak.", "Yes, that's right."
    llm.plans[first] = [{"text": "Should I change your delivery address to 12 Oak St?"}]
    llm.plans[second] = [{"tool": "update_address", "args": {"address": "12 Oak St"}}, {"text": "Done."}]
    await chat(client, first, "s-addr-yes")
    await chat(client, second, "s-addr-yes")
    assert len(puts(backend, "/address")) == 1 and gated(llm, second) == []


async def test_cancel_with_one_active_order_is_allowed(service):
    client, llm, backend = service
    backend.orders.append(order_json([{"menu_item_id": "item_1", "quantity": 1}], 12.5))
    msg = "Please cancel my order."
    llm.plans[msg] = [{"tool": "cancel_order", "args": {"order_id": "ord-1"}}, {"text": "Cancelled."}]
    await chat(client, msg, "s-cancel-ok")
    assert len(puts(backend, "/cancel")) == 1 and gated(llm, msg) == []


async def test_cancel_without_a_cancel_request_is_blocked(service):
    client, llm, backend = service
    backend.orders.append(order_json([{"menu_item_id": "item_1", "quantity": 1}], 12.5))
    msg = "Where is my order?"
    llm.plans[msg] = [{"tool": "cancel_order", "args": {"order_id": "ord-1"}}, {"text": "Hmm."}]
    await chat(client, msg, "s-cancel-unasked")
    assert puts(backend, "/cancel") == []
    (correction,) = gated(llm, msg)
    assert correction["violation"] == "unrequested_action" and correction["message"] == CANCEL_MESSAGE


async def test_cancel_of_a_different_order_is_blocked(service):
    client, llm, backend = service
    backend.orders.append(order_json([{"menu_item_id": "item_1", "quantity": 1}], 12.5))
    msg = "Cancel ord-2, please."
    llm.plans[msg] = [{"tool": "cancel_order", "args": {"order_id": "ord-1"}}, {"text": "Hmm."}]
    await chat(client, msg, "s-cancel-other")
    assert puts(backend, "/cancel") == []


async def test_cancel_with_two_active_orders_and_no_id_is_blocked(service):
    client, llm, backend = service
    a = order_json([{"menu_item_id": "item_1", "quantity": 1}], 12.5)
    backend.orders += [a, {**a, "id": "ord-2"}]
    msg = "Cancel my order."
    llm.plans[msg] = [{"tool": "cancel_order", "args": {"order_id": "ord-1"}}, {"text": "Hmm."}]
    await chat(client, msg, "s-cancel-ambiguous")
    assert puts(backend, "/cancel") == []


def test_withheld_response_in_the_turn_blocks_even_a_matching_address():
    withheld = {"tool": "get_user", "violation": "injection_detected_response", "tool_executed": True}
    t = turn("Change my address to 12 Oak St.", withheld)
    result = address_requested([], t, {"tool": "update_address", "params": {"address": "12 Oak St"}})
    assert not result and result.message == ADDRESS_MESSAGE
