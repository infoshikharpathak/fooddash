"""FoodDash Agent Service: the food ordering assistant, on the bastion SDK.

bastion runs the agent (Agent Forge) under Anvil governance: the agent, its
tools and its policy all come from anvil-fooddash.yaml. This service only
adds what is FoodDash-specific: the signed-in user's ID on every tool call,
and the HTTP API (chat, traces, health).
"""

from __future__ import annotations

import logging
import os
import contextvars
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

load_dotenv()
# Local dev fallback: load .env from a sibling agent-forge checkout if present.
_forge_env = os.path.join(os.path.dirname(__file__), "../../../agent-forge/.env")
if os.path.exists(_forge_env):
    load_dotenv(_forge_env)

from bastion import Platform  # noqa: E402

from . import a2a, requested  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger("fooddash-agent")

# A container mounts the yaml and sets ANVIL_CONFIG_PATH; local dev falls back
# to the repo-root file next to agent-service/.
CONFIG_PATH = os.environ.get("ANVIL_CONFIG_PATH") or os.path.join(
    os.path.dirname(__file__), "../../anvil-fooddash.yaml"
)
APP = "fooddash"
AGENT = "ordering_agent"
AGENT_ID = f"{APP}.{AGENT}"

# The agent's instructions call the user "demo_user"; FoodDash's backend has no
# such account, so every user-scoped call is pinned to this real one instead.
FOODDASH_USER_ID = os.environ.get("FOODDASH_USER_ID", "user_1")
_USER_SCOPED_TOOLS = ("get_user", "update_address", "place_order")


# The FoodDash user this request acts for: FOODDASH_USER_ID for FoodDash's
# own callers, or the user an A2A caller's key maps to (app/a2a.py).
_request_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("fooddash_request_user", default=None)


def prepare_tool_call(ctx: Any, tool: str, params: dict[str, Any]) -> dict[str, Any]:
    """Values the model must not choose, filled in before Anvil sees the call."""
    if tool in _USER_SCOPED_TOOLS:
        params["user_id"] = _request_user.get() or FOODDASH_USER_ID
    if tool == "pay_for_order":
        # Ironclad is told an agent started this payment. The tool's schema
        # declares only order_id, so Anvil blocks a model that tries to send
        # channel (or an amount) itself; this host-filled value is allowed.
        params["channel"] = "agent"
    return params


def _payment_started(call: dict[str, Any]) -> dict[str, Any] | None:
    """pay_for_order's result if this call created a pending Ironclad request."""
    data = call.get("response") if isinstance(call.get("response"), dict) else {}
    if call.get("tool") == "pay_for_order" and call.get("success") and data.get("payment_request_id") \
            and data.get("ironclad_status") == "pending_otp":
        return data
    return None


def approval_lines(tool_calls: list[dict[str, Any]]) -> list[str]:
    """The system's approval line for each payment request this turn created,
    from the structured tool result (never the model's words), once each."""
    lines, seen = [], set()
    for call in tool_calls:
        data = _payment_started(call)
        if data is None or data["payment_request_id"] in seen:
            continue
        try:
            amount = f"${float(data['amount']):,.2f}"
        except (KeyError, TypeError, ValueError):
            continue
        seen.add(data["payment_request_id"])
        lines.append(f"Payment of {amount} to FoodDash is waiting for your approval in your Ironclad app.")
    return lines


def add_lines(reply: str, lines: list[str]) -> str:
    out = reply.rstrip()
    for line in lines:
        if line not in out:
            out = f"{out}\n\n{line}" if out else line
    return out


def make_flagger(backend_url, transport: Any = None):
    """on_output_blocked hook: the reply was blocked, or delivered but labeled
    unrequested_action. Flag each payment request this turn created, through
    the backend (it holds FoodDash's Ironclad merchant key). The request
    stays pending; Ironclad's OTP modal warns the user."""

    async def flag(agent_id: str, tool_calls: list[dict[str, Any]], reason: str) -> list[dict[str, Any]]:
        out = []
        for call in tool_calls:
            data = _payment_started(call)
            if data is None:
                continue
            try:
                async with httpx.AsyncClient(timeout=10, transport=transport) as client:
                    resp = await client.post(f"{backend_url()}/orders/{data['order_id']}/payment/flag",
                                             json={"reason": (reason or "output_scan")[:500]})
                ok = resp.status_code < 400
            except httpx.HTTPError:
                ok = False
            (logger.warning if ok else logger.error)(
                "Output check: %s payment request %s", "flagged" if ok else "could not flag",
                data["payment_request_id"])
            out.append({"tool": "pay_for_order", "id": data["payment_request_id"], "what": "a payment request",
                        "where": "Ironclad", "flagged": ok})
        return out

    return flag


def build_platform(**platform_kwargs: Any) -> Platform:
    """The agent's platform, from the bastion control plane or the local YAML.

    Remote mode, when BASTION_URL and BASTION_KEY are both set: the bundle
    (tools, agent, prompt version, policy) comes from the service and is
    re-checked every BASTION_REFRESH_SECONDS (default 30), so prompt edits and
    rollbacks there apply without a restart. Otherwise it's library mode on
    the local anvil-fooddash.yaml, with the backend at FOODDASH_BASE_URL if
    that's set and the model from OPENAI_MODEL if that's set.

    *platform_kwargs* go to Platform (tests pass a stub LLM client, an HTTP
    transport for the backend, and in remote mode a service transport).
    """
    url, key = os.getenv("BASTION_URL"), os.getenv("BASTION_KEY")
    backend_url = os.getenv("FOODDASH_BASE_URL")
    holder: dict[str, Platform] = {}
    platform_kwargs.setdefault("is_requested_action", requested.make_is_requested_action(
        lambda: holder["platform"].config.platform.apps[APP]["base_url"].rstrip("/"),
        platform_kwargs.get("http_transport"),
    ))
    platform_kwargs.setdefault("on_output_blocked", make_flagger(
        lambda: holder["platform"].config.platform.apps[APP]["base_url"].rstrip("/"),
        platform_kwargs.get("http_transport"),
    ))
    if url and key:
        if backend_url:
            logger.warning("FOODDASH_BASE_URL is ignored in remote mode: set the fooddash app's "
                           "base_url on the bastion service instead.")
        platform = Platform.from_service(
            url, key, prepare_tool_call=prepare_tool_call,
            refresh_interval=float(os.getenv("BASTION_REFRESH_SECONDS", "30")),
            **platform_kwargs,
        )
        if platform.app != APP:
            raise RuntimeError(f"BASTION_KEY belongs to app {platform.app!r}, not {APP!r}.")
        logger.info("bastion remote mode: %s, revision %s", url, platform.bundle_revision)
        holder["platform"] = platform
        return platform

    with open(CONFIG_PATH) as f:
        raw = yaml.safe_load(f)
    if backend_url:  # e.g. http://app:8000 in a container; the YAML says localhost
        raw["apps"][APP]["base_url"] = backend_url.rstrip("/")
    if os.getenv("OPENAI_MODEL"):  # library mode only; in remote mode the service decides the model
        raw["agents"][AGENT_ID]["model"] = os.environ["OPENAI_MODEL"]
    platform = Platform.from_config(raw, app=APP, prepare_tool_call=prepare_tool_call, **platform_kwargs)
    holder["platform"] = platform
    return platform


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set: the ordering agent and Anvil's classifier need it.")
    platform = build_platform()
    await platform.start()
    app.state.platform = platform
    logger.info("FoodDash Agent Service ready (agents=%s)", [a["name"] for a in platform.list_agents()])
    yield
    await platform.close()  # flushes traces


app = FastAPI(title="FoodDash Agent Service", lifespan=lifespan)


def _platform() -> Platform:
    return app.state.platform


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str | None = None
    """Turns in one session share the agent's memory and Anvil's sequence state
    (a get_menu in one message satisfies place_order in the next). Omit it to
    start a new session; the response says which one was used."""


class ChatResponse(BaseModel):
    response: str
    session_id: str
    trace_id: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/agent/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request) -> ChatResponse:
    """FoodDash's own UI calls this without a key and gets the full tool set.
    Another app calls it with the ak_ key FoodDash issued to it: identity
    comes from that key only (headers are informational), its sessions are
    kept apart from FoodDash's own, and they get the restricted tool set."""
    caller = None
    if request.headers.get("authorization"):
        caller = a2a.load().authenticate(request.headers)
        if caller is None:
            raise HTTPException(status_code=401, detail="Unknown or revoked A2A key.")
    session_id = req.session_id or f"sess_{uuid.uuid4().hex[:12]}"
    # Another app's sessions live in their own namespace: it can't reach (or
    # spoof) one of FoodDash's own sessions by choosing its ID.
    run_session = f"ext-{caller.app}-{session_id}" if caller else session_id
    origin = ({"caller_app": caller.app, "parent_trace_id": caller.parent_trace_id, "chain": caller.chain}
              if caller else {})
    token = _request_user.set(caller.user_id if caller else None)
    try:
        reply = await _platform().run_agent(AGENT, req.message, session_id=run_session, **origin)
    except Exception as exc:
        logger.exception("Agent error")
        raise HTTPException(status_code=502, detail=f"Agent error: {exc}") from exc
    finally:
        _request_user.reset(token)
    # The approval line is the system's: added after the output scan and any
    # fallback reply, built from pay_for_order's structured result.
    lines = approval_lines(reply.trace.get("tool_calls", []))
    return ChatResponse(response=add_lines(reply.content, lines), session_id=session_id, trace_id=reply.trace_id)


def _short(name: str) -> str:
    return name.split(".", 1)[1] if "." in name else name


_BLOCKED_RESULTS = ("blocked", "response_withheld")


@app.get("/agent/traces/{session_id}")
def get_traces(session_id: str):
    """The agent's tool calls in a session, oldest first, with Anvil's decision
    on each. Unknown or expired sessions return an empty list."""
    traces = []
    for r in _platform().session_records(session_id):
        if r["kind"] != "tool" or r["agent"] != AGENT_ID:
            continue
        traces.append({
            "timestamp": r["timestamp"],
            "trace_id": r.get("trace_id"),
            "tool": _short(r["tool"]),
            "parameters": r["params"],
            "decision": "blocked" if r["result"] in _BLOCKED_RESULTS else "allowed",
            "result": r["result"],
            "violation": r["violation"],
            "correction_message": r["correction_message"],
            "http_status": r.get("http_status"),  # the tool's 4xx/5xx status, for upstream errors
            "latency_ms": round(r["latency_ms"], 1) if r["latency_ms"] is not None else None,
        })
    return {"session_id": session_id, "traces": traces}
