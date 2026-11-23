"""agent-service on garrison: chat, governance, sessions and traces.

Every test checks what the model actually received in each tool result, not
just which backend endpoints were called: a tool can reach the backend and
still hand the model an error."""

import json

from .conftest import MENU, RESTAURANTS, USER, order_json, received

ITEMS = [{"menu_item_id": "item_1", "quantity": 2}, {"menu_item_id": "item_2", "quantity": 1}]
BROWSE_AND_ORDER = "I'd like 2 Kung Pao Chicken and spring rolls from Golden Dragon."
ORDER_PLAN = [
    {"tool": "list_restaurants", "args": {}},
    {"tool": "get_menu", "args": {"restaurant_id": "rest_1"}},
    {"tool": "place_order", "args": {"restaurant_id": "rest_1", "user_id": "demo_user", "items": ITEMS}},
    {"text": "Order ord-1 placed at Golden Dragon: $31.00, paid."},
]
SHOW_MENU = "What's on the Golden Dragon menu?"
MENU_PLAN = [
    {"tool": "get_menu", "args": {"restaurant_id": "rest_1"}},
    {"text": "Kung Pao Chicken $12.50, Spring Rolls $6.00."},
]
ORDER_NOW = "Order 2 Kung Pao Chicken from there."
PLACE_PLAN = [
    {"tool": "place_order", "args": {"restaurant_id": "rest_1", "items": [{"menu_item_id": "item_1", "quantity": 2}]}},
    {"text": "Done: order ord-1, $25.00."},
]
BLOCKED_PLAN = [PLACE_PLAN[0], {"text": "I need to check the menu first."}]


async def chat(client, message, session_id=None):
    body = {"message": message}
    if session_id is not None:
        body["session_id"] = session_id
    resp = await client.post("/agent/chat", json=body)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert set(out) == {"response", "session_id", "trace_id"}
    return out


async def test_chat_browses_then_places_an_order(service):
    client, llm, backend = service
    llm.plans[BROWSE_AND_ORDER] = ORDER_PLAN

    out = await chat(client, BROWSE_AND_ORDER)
    assert out["response"] == "Order ord-1 placed at Golden Dragon: $31.00, paid."
    assert out["session_id"].startswith("sess_") and out["trace_id"]

    # What the model was handed at each step: the backend's JSON, arrays included.
    assert received(llm, BROWSE_AND_ORDER) == [
        (True, RESTAURANTS),
        (True, MENU),
        (True, order_json(ITEMS, 31.0)),
    ]
    assert backend.calls() == [("GET", "/restaurants"), ("GET", "/restaurants/rest_1/menu"), ("POST", "/orders")]
    sent = json.loads(backend.requests[-1].content)
    # The model asked for "demo_user"; the service pins the real backend account.
    assert sent == {"restaurant_id": "rest_1", "user_id": "user_1", "items": ITEMS}


async def test_place_order_without_get_menu_is_blocked(service):
    client, llm, backend = service
    llm.plans[ORDER_NOW] = BLOCKED_PLAN

    out = await chat(client, ORDER_NOW, "fresh")
    assert out["response"] == "I need to check the menu first."
    assert ("POST", "/orders") not in backend.calls()
    (seen,) = llm.tool_results_seen(ORDER_NOW)
    assert seen["success"] is False and seen["data"] is None
    assert seen["correction"]["violation"] == "missing_prerequisite"
    assert seen["correction"]["required_actions"] == ["get_menu"]


async def test_list_restaurants_alone_does_not_satisfy_the_menu_rule(service):
    client, llm, backend = service
    msg = "Just order me something from Golden Dragon."
    llm.plans[msg] = [{"tool": "list_restaurants", "args": {}}, *BLOCKED_PLAN]
    await chat(client, msg, "s-list-only")
    assert backend.calls() == [("GET", "/restaurants")]
    (listed, blocked) = llm.tool_results_seen(msg)
    assert (listed["success"], listed["data"]) == (True, RESTAURANTS)
    assert blocked["correction"]["violation"] == "missing_prerequisite"


async def test_session_continuity(service):
    """get_menu in message 1 satisfies place_order in message 2 of the same
    session, the model sees message 1 in its history, and another session
    gets neither."""
    client, llm, backend = service
    llm.plans.update({SHOW_MENU: MENU_PLAN, ORDER_NOW: PLACE_PLAN})

    first = await chat(client, SHOW_MENU)
    assert received(llm, SHOW_MENU) == [(True, MENU)]
    session = first["session_id"]
    second = await chat(client, ORDER_NOW, session)
    assert second["session_id"] == session
    assert second["response"] == "Done: order ord-1, $25.00."
    assert received(llm, ORDER_NOW) == [(True, order_json([{"menu_item_id": "item_1", "quantity": 2}], 25.0))]
    assert any(isinstance(m, dict) and m.get("content") == SHOW_MENU for m in llm.requests[-1])
    assert second["trace_id"] != first["trace_id"]  # one trace per turn

    llm.plans[ORDER_NOW] = BLOCKED_PLAN
    other = await chat(client, ORDER_NOW, "another-session")
    assert other["response"] == "I need to check the menu first."
    assert backend.calls().count(("POST", "/orders")) == 1
    for msgs in llm.requests[-2:]:
        assert not any(isinstance(m, dict) and m.get("content") == SHOW_MENU for m in msgs), "history leaked"


async def test_trace_endpoint_returns_the_session_traces(service):
    client, llm, _ = service
    llm.plans.update({SHOW_MENU: MENU_PLAN, ORDER_NOW: BLOCKED_PLAN})
    ok = await chat(client, SHOW_MENU, "s-ok")
    await chat(client, ORDER_NOW, "s-blocked")

    t_ok = (await client.get("/agent/traces/s-ok")).json()
    assert t_ok["session_id"] == "s-ok"
    (menu,) = t_ok["traces"]
    assert (menu["tool"], menu["decision"], menu["result"], menu["violation"]) == ("get_menu", "allowed", "success", None)
    assert menu["parameters"] == {"restaurant_id": "rest_1"}
    assert menu["trace_id"] == ok["trace_id"]
    assert received(llm, SHOW_MENU) == [(True, MENU)]  # the trace's "success" is what the model got

    (blocked,) = (await client.get("/agent/traces/s-blocked")).json()["traces"]
    assert (blocked["tool"], blocked["decision"], blocked["violation"]) == ("place_order", "blocked", "missing_prerequisite")
    assert "get_menu" in blocked["correction_message"]
    assert blocked["parameters"]["user_id"] == "user_1"  # the prepared params Anvil saw

    assert (await client.get("/agent/traces/nobody")).json() == {"session_id": "nobody", "traces": []}


async def test_profile_order_status_and_order_list(service):
    client, llm, backend = service
    llm.plans.update({SHOW_MENU: MENU_PLAN, ORDER_NOW: PLACE_PLAN})
    await chat(client, SHOW_MENU, "s-status")
    await chat(client, ORDER_NOW, "s-status")
    placed = order_json([{"menu_item_id": "item_1", "quantity": 2}], 25.0)

    msg = "Who am I, where's ord-1, and what have I ordered?"
    llm.plans[msg] = [
        {"tool": "get_user", "args": {"user_id": "demo_user"}},
        {"tool": "get_order", "args": {"order_id": "ord-1"}},
        {"tool": "list_orders", "args": {"limit": 5}},
        {"text": "Alice; ord-1 has been picked up; one order."},
    ]
    out = await chat(client, msg, "s-status")
    assert out["response"] == "Alice; ord-1 has been picked up; one order."
    assert received(llm, msg) == [(True, USER), (True, {**placed, "status": "picked_up"}), (True, [placed])]
    assert backend.calls()[-3:] == [("GET", "/users/user_1"), ("GET", "/orders/ord-1"), ("GET", "/orders")]


async def test_message_is_required(service):
    client, _, _ = service
    assert (await client.post("/agent/chat", json={})).status_code == 422
    assert (await client.post("/agent/chat", json={"message": ""})).status_code == 422


async def test_health(service):
    client, _, _ = service
    assert (await client.get("/health")).json() == {"status": "ok"}
