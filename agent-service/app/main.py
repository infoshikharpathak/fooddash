"""FoodDash Agent Service: the food ordering assistant, on the bastion SDK.

bastion runs the agent (Agent Forge) under Anvil governance: the agent, its
tools and its policy all come from anvil-fooddash.yaml. This service only
adds what is FoodDash-specific: the signed-in user's ID on every tool call,
and the HTTP API (chat, traces, health).
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

load_dotenv()
# Local dev fallback: load .env from a sibling agent-forge checkout if present.
_forge_env = os.path.join(os.path.dirname(__file__), "../../../agent-forge/.env")
if os.path.exists(_forge_env):
    load_dotenv(_forge_env)

from bastion import Platform  # noqa: E402

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


def prepare_tool_call(ctx: Any, tool: str, params: dict[str, Any]) -> dict[str, Any]:
    """Values the model must not choose, filled in before Anvil sees the call."""
    if tool in _USER_SCOPED_TOOLS:
        params["user_id"] = FOODDASH_USER_ID
    if tool == "pay_for_order":
        # The model names the order and nothing else: the amount is the
        # order's stored total, and Ironclad is told an agent started it.
        return {"order_id": params.get("order_id"), "channel": "agent"}
    return params


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
        return platform

    with open(CONFIG_PATH) as f:
        raw = yaml.safe_load(f)
    if backend_url:  # e.g. http://app:8000 in a container; the YAML says localhost
        raw["apps"][APP]["base_url"] = backend_url.rstrip("/")
    if os.getenv("OPENAI_MODEL"):  # library mode only; in remote mode the service decides the model
        raw["agents"][AGENT_ID]["model"] = os.environ["OPENAI_MODEL"]
    return Platform.from_config(raw, app=APP, prepare_tool_call=prepare_tool_call, **platform_kwargs)


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
async def chat(req: ChatRequest) -> ChatResponse:
    session_id = req.session_id or f"sess_{uuid.uuid4().hex[:12]}"
    try:
        reply = await _platform().run_agent(AGENT, req.message, session_id=session_id)
    except Exception as exc:
        logger.exception("Agent error")
        raise HTTPException(status_code=502, detail=f"Agent error: {exc}") from exc
    return ChatResponse(response=reply.content, session_id=session_id, trace_id=reply.trace_id)


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
            "latency_ms": round(r["latency_ms"], 1) if r["latency_ms"] is not None else None,
        })
    return {"session_id": session_id, "traces": traces}
