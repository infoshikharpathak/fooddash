"""Did the user ask for this action? Deterministic checks (no LLM).

Two kinds of actions, two moments:

- pay_for_order has an OTP behind it, so it runs, and is checked afterwards
  only if the output classifier labels the reply unrequested_action: it is
  requested if the order was placed in this session in a turn where the user
  confirmed it, and no tool response in the session was withheld.
- update_address and cancel_order change data with no OTP behind them, so a
  flag would do nothing: they are checked BEFORE they run (bastion's
  pre-call gate) and blocked with unrequested_action unless the user asked:
  - update_address: the new address appears in the user's messages of this
    turn or the previous 2 (case- and whitespace-insensitive), or the agent
    proposed it last turn and the user said yes.
  - cancel_order: the user asked to cancel in this turn or the previous 2,
    and the order is the one they referred to (by ID, or their only active
    order).
  A turn with a withheld tool response never counts.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from bastion import NOT_AN_ACTION, NotRequested

from .a2a import EXTERNAL_PAYMENT_MESSAGE, EXTERNAL_TOOLS

LOOKBACK = 2
ACTIVE = {"created", "confirmed", "preparing", "ready"}  # cancellable: not picked up yet
_CONFIRM = re.compile(r"\b(?:yes|yeah|yep|sure|ok(?:ay)?|confirm(?:ed)?|go ahead|do it|place (?:it|the order)|"
                      r"please do|that'?s right|sounds good|correct)\b", re.I)
_CANCEL = re.compile(r"\bcancel(?:l?ed|l?ing)?\b", re.I)
_DONT_CANCEL = re.compile(r"\b(?:don'?t|do not|never|not)\s+cancel\b", re.I)
# Order IDs as users might name them: the backend's UUIDs, or ord-style IDs.
_ORDER_ID = re.compile(r"\b(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|ord[-_][\w-]+)\b", re.I)
_WITHHELD = {"injection_detected_response", "schema_violation"}

ADDRESS_MESSAGE = "Ask the user to confirm before changing the address."
CANCEL_MESSAGE = "Ask the user to confirm before cancelling the order."


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[.,;!?]+(\s|$)", r"\1", (text or "").lower()).split())


def _withheld(turns: list[dict]) -> bool:
    return any(c.get("violation") in _WITHHELD for t in turns for c in t.get("tool_calls") or [])


def _user_texts(history: list[dict], turn: dict) -> list[str]:
    return [t.get("user") or "" for t in history[-LOOKBACK:]] + [turn.get("user") or ""]


def _session_orders(turns: list[dict]) -> set[str]:
    """IDs of orders place_order created in these turns."""
    return {(c.get("response") or {}).get("id") for t in turns for c in t.get("tool_calls") or []
            if c.get("tool") == "place_order" and c.get("success") and isinstance(c.get("response"), dict)}


def payment_requested(history: list[dict[str, Any]], turn: dict[str, Any], action: dict[str, Any]):
    turns = [*history, turn]
    if _withheld(turns):
        return False
    order_id = (action.get("params") or {}).get("order_id")
    for t in turns:
        for c in t.get("tool_calls") or []:
            data = c.get("response") if isinstance(c.get("response"), dict) else {}
            if c.get("tool") == "place_order" and c.get("success") and data.get("id") == order_id:
                return bool(_CONFIRM.search(t.get("user") or ""))
    return False  # the order wasn't placed in this session


def address_requested(history: list[dict[str, Any]], turn: dict[str, Any], action: dict[str, Any]):
    if _withheld([turn]):
        return NotRequested(ADDRESS_MESSAGE)
    address = _norm((action.get("params") or {}).get("address") or "")
    if address and any(address in _norm(t) for t in _user_texts(history, turn)):
        return True
    last_reply = _norm(history[-1].get("reply") or "") if history else ""
    if address and address in last_reply and _CONFIRM.search(turn.get("user") or ""):
        return True  # the agent proposed it and the user said yes
    return NotRequested(ADDRESS_MESSAGE)


def make_is_requested_action(backend_url, transport: Any = None):
    """The app's is_requested_action hook. *backend_url* is a callable (the
    base URL can change with a remote bundle); the backend is asked for the
    user's active orders when a cancel names no order."""

    async def active_orders(user_id: str) -> list[str]:
        async with httpx.AsyncClient(timeout=10, transport=transport) as client:
            resp = await client.get(f"{backend_url()}/orders")
        resp.raise_for_status()
        return [o["id"] for o in resp.json() if o.get("user_id") == user_id and o.get("status") in ACTIVE]

    async def cancel_requested(history: list[dict], turn: dict, action: dict):
        if _withheld([turn]):
            return NotRequested(CANCEL_MESSAGE)
        texts = _user_texts(history, turn)
        if not any(_CANCEL.search(t) and not _DONT_CANCEL.search(t) for t in texts):
            return NotRequested(CANCEL_MESSAGE)
        order_id = str((action.get("params") or {}).get("order_id") or "")
        named = {m.group(0).lower() for t in texts for m in _ORDER_ID.finditer(t)}
        if named:
            return True if order_id.lower() in named else NotRequested(CANCEL_MESSAGE)
        user_id = (action.get("params") or {}).get("user_id") or "user_1"
        try:
            active = await active_orders(user_id)
        except (httpx.HTTPError, ValueError, KeyError):
            return NotRequested(CANCEL_MESSAGE)
        return True if active == [order_id] else NotRequested(CANCEL_MESSAGE)

    async def external(history: list[dict], turn: dict, action: dict):
        """A session another app started: only EXTERNAL_TOOLS, payments never,
        and orders only from this session. Enforced from the authenticated
        caller (turn["caller_app"]), whatever the model or the prompt says."""
        tool = action.get("tool")
        if tool == "pay_for_order":
            return NotRequested(EXTERNAL_PAYMENT_MESSAGE, violation="external_caller_payment")
        if tool not in EXTERNAL_TOOLS:
            return NotRequested(f"{tool} isn't available in conversations started by another app.",
                                violation="external_caller_tool")
        if tool in ("get_order", "cancel_order"):
            order_id = (action.get("params") or {}).get("order_id")
            if order_id not in _session_orders([*history, turn]):
                return NotRequested("Only orders placed in this conversation can be checked or cancelled here.",
                                    violation="external_caller_order")
            if tool == "cancel_order":
                return await cancel_requested(history, turn, action)
        return NOT_AN_ACTION

    async def is_requested_action(history: list[dict[str, Any]], turn: dict[str, Any], action: dict[str, Any]):
        tool, before = action.get("tool"), action.get("phase") == "before"
        if before and turn.get("caller_app"):
            return await external(history, turn, action)
        if tool == "update_address":
            return address_requested(history, turn, action) if before else NOT_AN_ACTION
        if tool == "cancel_order":
            return await cancel_requested(history, turn, action) if before else NOT_AN_ACTION
        if tool == "pay_for_order" and not before and action.get("success"):
            return payment_requested(history, turn, action)
        return NOT_AN_ACTION

    return is_requested_action


def is_requested_action(history, turn, action):
    """pay_for_order only (no backend access): kept for callers and tests."""
    if action.get("tool") != "pay_for_order" or not action.get("success") or action.get("phase") == "before":
        return NOT_AN_ACTION
    return payment_requested(history, turn, action)
