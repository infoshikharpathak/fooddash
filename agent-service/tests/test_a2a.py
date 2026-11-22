"""Calls from other apps (A2A): identity from the ak_ key FoodDash issued,
a restricted tool set enforced in code, and no payments."""

import json

import pytest
from bastion.a2a import CALL_CHAIN_HEADER, TRACE_ID_HEADER, hash_key, new_key

from app.a2a import EXTERNAL_PAYMENT_MESSAGE

from .conftest import order_json
from .test_payments import ORDER_AND_PAY, PLAN

HUB_KEY = new_key()


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("FOODDASH_A2A_KEYS", json.dumps({hash_key(HUB_KEY): {"app": "chathub", "user_id": "user_1"}}))


def hub(extra=None):
    return {"Authorization": f"Bearer {HUB_KEY}", **(extra or {})}


async def chat(client, message, session_id, headers=None, status=200):
    r = await client.post("/agent/chat", json={"message": message, "session_id": session_id}, headers=headers or {})
    assert r.status_code == status, r.text
    return r.json()


def corrections(llm, msg):
    return [r["correction"] for r in llm.tool_results_seen(msg) if r.get("correction")]


async def test_unknown_key_is_401(service):
    client, llm, backend = service
    await chat(client, "hi", "x", headers={"Authorization": f"Bearer {new_key()}"}, status=401)
    await chat(client, "hi", "x", headers={"Authorization": "Bearer not-a-key"}, status=401)


async def test_external_payment_is_blocked_and_the_order_stays_pending(service):
    client, llm, backend = service
    backend.new_payment_status = "pending"
    llm.plans[ORDER_AND_PAY] = [*PLAN[:3], {"text": "Order placed; pay in FoodDash."}]
    await chat(client, ORDER_AND_PAY, "hub-1", headers=hub())
    (c,) = corrections(llm, ORDER_AND_PAY)
    assert c["violation"] == "external_caller_payment" and c["message"] == EXTERNAL_PAYMENT_MESSAGE
    assert ("POST", "/orders/ord-1/pay") not in backend.calls()
    assert backend.orders[-1]["payment_status"] == "pending"


@pytest.mark.parametrize("tool,args", [
    ("update_address", {"address": "1 Evil St"}), ("list_orders", {}), ("get_user", {}),
])
async def test_tools_outside_the_restricted_set_are_blocked(service, tool, args):
    client, llm, backend = service
    msg = f"please use {tool}"
    llm.plans[msg] = [{"tool": tool, "args": args}, {"text": "Can't."}]
    await chat(client, msg, f"hub-{tool}", headers=hub())
    (c,) = corrections(llm, msg)
    assert c["violation"] == "external_caller_tool"
    assert backend.calls() == []


async def test_the_restricted_tools_work(service):
    client, llm, backend = service
    msg = "What's on Golden Dragon's menu?"
    llm.plans[msg] = [{"tool": "list_restaurants", "args": {}}, {"tool": "get_menu", "args": {"restaurant_id": "rest_1"}},
                      {"text": "Kung Pao Chicken and Spring Rolls."}]
    body = await chat(client, msg, "hub-menu", headers=hub())
    assert corrections(llm, msg) == [] and body["response"].startswith("Kung Pao")


async def test_get_order_and_cancel_only_for_this_sessions_orders(service):
    client, llm, backend = service
    backend.orders.append({**order_json([{"menu_item_id": "item_1", "quantity": 1}], 12.5), "id": "ord-9"})
    other = "What's the status of ord-9? Cancel ord-9."
    llm.plans[other] = [{"tool": "get_order", "args": {"order_id": "ord-9"}},
                        {"tool": "cancel_order", "args": {"order_id": "ord-9"}}, {"text": "Can't."}]
    await chat(client, other, "hub-orders", headers=hub())
    assert {c["violation"] for c in corrections(llm, other)} == {"external_caller_order"}
    assert ("GET", "/orders/ord-9") not in backend.calls() and ("PUT", "/orders/ord-9/cancel") not in backend.calls()
    # an order placed in this session can be read and cancelled
    backend.new_payment_status = "pending"
    llm.plans[ORDER_AND_PAY] = [*PLAN[:3], {"text": "Placed."}]
    await chat(client, ORDER_AND_PAY, "hub-orders", headers=hub())
    mine = "Check ord-1, then cancel ord-1."
    llm.plans[mine] = [{"tool": "get_order", "args": {"order_id": "ord-1"}},
                       {"tool": "cancel_order", "args": {"order_id": "ord-1"}}, {"text": "Cancelled."}]
    await chat(client, mine, "hub-orders", headers=hub())
    assert corrections(llm, mine) == []
    assert ("PUT", "/orders/ord-1/cancel") in backend.calls()


async def test_foodDash_own_callers_keep_the_full_tool_set(service):
    client, llm, backend = service
    backend.new_payment_status = "pending"
    llm.plans[ORDER_AND_PAY] = PLAN
    await chat(client, ORDER_AND_PAY, "own-1")
    assert ("POST", "/orders/ord-1/pay") in backend.calls()


async def test_external_sessions_are_namespaced_apart_from_foodDash_sessions(service):
    import app.main as main

    client, llm, backend = service
    llm.plans["hi"] = [{"text": "Hi!"}, {"text": "Hi again!"}]
    await chat(client, "hi", "shared-id")
    await chat(client, "hi", "shared-id", headers=hub())
    assert main._platform().session_records("shared-id") is not None
    assert main._platform().session_records("ext-chathub-shared-id") is not None


async def test_caller_app_comes_from_the_key_even_with_a_forged_chain(service):
    import app.main as main

    client, llm, backend = service
    llm.plans["hi"] = [{"text": "Hi!"}]
    seen = []
    original = main._platform().run_agent

    async def spy(*args, **kwargs):
        seen.append(kwargs)
        return await original(*args, **kwargs)

    main._platform().run_agent = spy
    body = await chat(client, "hi", "forged", headers=hub({CALL_CHAIN_HEADER: "fooddash.ordering_agent",
                                                          TRACE_ID_HEADER: "parent-1"}))
    assert seen[0]["caller_app"] == "chathub" and seen[0]["parent_trace_id"] == "parent-1"
    assert seen[0]["chain"] == ["fooddash.ordering_agent"]
    assert body["session_id"] == "forged"
