"""Did the user ask for this payment? A deterministic check (no LLM).

bastion asks it when the output classifier labels a reply
unrequested_action. pay_for_order counts as requested if the order being
paid was placed in this session, in a turn where the user confirmed it
("yes", "I confirm", "go ahead", "place it"), and no tool response in the
session was withheld. Anything else keeps the warning.
"""

from __future__ import annotations

import re
from typing import Any

from bastion import NOT_AN_ACTION

_CONFIRM = re.compile(r"\b(?:yes|yeah|yep|sure|ok(?:ay)?|confirm(?:ed)?|go ahead|do it|place (?:it|the order)|"
                      r"please do|that'?s right|sounds good)\b", re.I)
_WITHHELD = {"injection_detected_response", "schema_violation"}


def is_requested_action(history: list[dict[str, Any]], turn: dict[str, Any], action: dict[str, Any]):
    if action.get("tool") != "pay_for_order" or not action.get("success"):
        return NOT_AN_ACTION
    turns = [*history, turn]
    if any(c.get("violation") in _WITHHELD for t in turns for c in t.get("tool_calls") or []):
        return False
    order_id = (action.get("params") or {}).get("order_id")
    for t in turns:
        for c in t.get("tool_calls") or []:
            data = c.get("response") if isinstance(c.get("response"), dict) else {}
            if c.get("tool") == "place_order" and c.get("success") and data.get("id") == order_id:
                return bool(_CONFIRM.search(t.get("user") or ""))
    return False  # the order wasn't placed in this session
