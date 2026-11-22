# FoodDash

[![CI](https://github.com/infoshikharpathak/fooddash/actions/workflows/ci.yml/badge.svg)](https://github.com/infoshikharpathak/fooddash/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)

A fully functional, self-running food delivery app built to generate realistic
traffic and errors for [logscribe](../logscribe) to observe and analyze.
FoodDash has zero knowledge of logscribe — it just logs normally to a shared
file; logscribe taps that log stream externally.

## Architecture

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  Restaurant  │───▶│    Order     │───▶│   Delivery   │
│   Service    │    │   Service    │    │   Service    │
└──────┬───────┘    └──────┬───────┘    └──────┬───────┘
       │                   │                   │
       └───────────────────┼───────────────────┘
                            │
                     ┌──────▼───────┐
                     │ Shared Log   │  Python logging → file + stdout
                     │   Pipeline   │
                     └──────┬───────┘
                            │
                     ┌──────▼───────┐
                     │  logscribe   │  tapped line — observes the log stream,
                     │  (external)  │  activates on incident detection
                     └──────────────┘
```

All three "services" are routers mounted on one FastAPI app (`main.py`), but
they talk to each other over internal HTTP calls (not direct Python calls) so
the logs look like genuine service-to-service traffic. Those calls go through
the app in-process via an ASGI transport (`core/internal_client.py`) — the full
middleware/logging stack runs, but there's no network hop. Order validates
against restaurant via HTTP, delivery notifies order via HTTP, and a
`request_id` is propagated through every hop via the `X-Request-ID` header so
a whole customer journey can be correlated across services.

- **restaurant** (`services/restaurant.py`) — in-memory restaurants + menus.
- **users** (`services/users.py`) — in-memory demo users (`user_1`–`user_4`,
  plus generic `user_5`–`user_20` for the traffic simulator) with a delivery
  address and balance. `GET /users/{id}`, `PUT /users/{id}/address`.
- **order** (`services/order.py`) — order state machine, validates against
  restaurant over HTTP, starts a delivery for each confirmed order as an
  in-process background task.
- **delivery** (`services/delivery.py`) — runs each delivery through its
  lifecycle in the background, calls back to order over HTTP as status
  changes.
- **traffic simulator** (`traffic/simulator.py`) — launches on startup, drives
  full customer journeys (browse → menu → order → track/cancel) against the
  app's own HTTP endpoints indefinitely, including edge cases (ordering
  unavailable items, double-submitting, cancelling mid-delivery).

## Logging contract

This is the interface between the two repos. Every log line is a single JSON
object, written to both stdout and `/tmp/fooddash.log`:

```json
{"timestamp": "...", "service_name": "order", "level": "ERROR", "message": "...",
 "request_id": "...", "endpoint": "/orders", "duration_ms": 812,
 "error_type": "PaymentTimeoutError", "stack_trace": "..."}
```

`error_type`/`stack_trace` are only present on error-level logs. Calls to
Ironclad (see [Payments](#payments-through-ironclad)) add `order_id`,
`payment_request_id` and `status` (Ironclad's payment status).

## Dashboard

A Streamlit dashboard (`app.py`) gives a live, showcase-friendly view of the
running system — talks to the backend over HTTP and auto-refreshes, so it
works the same "walk away, come back" way the backend itself does.

```bash
streamlit run app.py
```

Four tabs:
- **Dashboard** — live metrics (orders, deliveries, errors, uptime, request
  count) plus three charts: orders by status, deliveries by status, and
  errors by type.
- **Restaurants** — browse the seeded restaurants and their menus.
- **Orders** — table of recent orders and their current status.
- **Deliveries** — table of recent deliveries and their current status.

Powered by three new read-only backend endpoints: `GET /orders`,
`GET /delivery` (list views — the existing `GET /orders/{id}` and
`GET /delivery/{order_id}` are unchanged), and `GET /stats` (aggregate
counts). None of these have side effects — purely for observing state, same
spirit as the log stream logscribe taps.

## Quick start

### Docker Compose (app + dashboard together)

```bash
docker compose up --build
```

Starts the backend (generating traffic immediately, no separate script
to run), and the dashboard at http://localhost:8501.

### Local dev (no Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

uvicorn main:app --reload

# in another terminal
streamlit run app.py
```

### Tests

```bash
pip install -r requirements-dev.txt
pytest tests/
```

The suite drives the real FastAPI app in memory through `httpx.AsyncClient`
with an ASGI transport — no server, no network, no mocks. Delivery stage
delays are set to 0 and the traffic simulator is off; test logs go to a
separate file (`FOODDASH_LOG_FILE`) so they never mix into `/tmp/fooddash.log`.

## Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `INTERNAL_BASE_URL` | `http://localhost:8000` | Base URL the traffic simulator drives the app through |
| `AUTO_TRAFFIC` | `true` | Whether the traffic simulator runs on startup |
| `TRAFFIC_RPS` | `3` | New customer journeys started per second |
| `DELIVERY_STAGE_DELAY_MIN` / `_MAX` | `1` / `4` | Seconds (uniform range) before each delivery lifecycle stage |
| `FOODDASH_LOG_FILE` | `/tmp/fooddash.log` | Where the JSON log stream is written (stdout too) |
| `IRONCLAD_PAYMENTS_ENABLED` | `false` | Take real payments through Ironclad (see below); off, every order is paid at once |
| `IRONCLAD_BASE_URL` | `http://localhost:8001` | Ironclad's bank service |
| `IRONCLAD_MERCHANT_KEY` | — | FoodDash's `mk_` merchant key, issued by Ironclad |
| `IRONCLAD_POLL_SECONDS` | `3` | How often a pending payment is checked |
| `FOODDASH_API_URL` | `http://localhost:8000` | Backend URL the dashboard polls (set to `http://app:8000` under Docker Compose) |

## Payments through Ironclad

FoodDash is a registered merchant of [Ironclad](../Ironclad), the demo bank.
With `IRONCLAD_PAYMENTS_ENABLED=true`, orders by a user with an Ironclad
account are paid for real; nothing moves until the user approves the payment
with the OTP in Ironclad.

- **Who pays for real:** users in `services/payments.py`'s `USER_MAP`
  (`user_1` → Ironclad `demo_user`). Everyone else keeps the simulated
  payment (paid at once). The traffic simulator never picks a mapped user,
  so simulated orders never wait on a payment, and FoodDash still runs on
  its own.
- **Flow:** the order starts with `payment_status: pending` and no delivery.
  `POST /orders/{id}/pay` (body `{"channel": "checkout"}` by default, or
  `"agent"`) asks Ironclad for a payment request with FoodDash's merchant
  key: the order's stored total (never an amount from the caller),
  `order_ref` = the order ID, and an idempotency key based on the order ID.
  A background task then polls the request: `confirmed` → `paid` and the
  delivery starts; `cancelled` or `expired` → `failed` and the order is
  cancelled. In production Ironclad would call a FoodDash webhook instead of
  being polled.
- **Refusals:** if Ironclad refuses, `/pay` answers with Ironclad's status
  code (e.g. 403 for `AGENT_PAYMENTS_NOT_ALLOWED`) and
  `{"detail": {"error", "message"}}`; Ironclad being down, or rejecting
  FoodDash's own key, is a 502. The ordering agent gets the status and body
  through Anvil.
- **Cancelling:** cancelling an order whose payment is still pending also
  withdraws the request in Ironclad (`POST /payments/requests/{id}/cancel`),
  which closes the user's OTP modal. If the user had already paid, the order
  stays cancelled and "refund needed" is logged.
- **Agent:** the ordering agent's `pay_for_order` tool calls this endpoint
  with `channel: agent`. See [Agent Service](#agent-service).

```bash
# Ironclad: issue FoodDash's key (once), from Ironclad/bank-service
python -m app.cli merchants issue-key fooddash

# FoodDash backend
IRONCLAD_PAYMENTS_ENABLED=true IRONCLAD_MERCHANT_KEY=mk_... uvicorn main:app --port 8000
```

## Demo flow

```bash
# terminal 1
docker compose up --build

# terminal 2, once fooddash is running
cd ../logscribe
logscribe watch --file /tmp/fooddash.log
```

Open http://localhost:8501 for the live dashboard, then walk away. Traffic
keeps flowing, the simulator's edge cases (unavailable items, double-submits,
cancellations) produce real business-rule errors, logscribe builds up a
history of root-cause analyses, and the dashboard's metrics and charts update
on their own.

## Agent Service

`agent-service/` is a chat-based ordering assistant: a separate FastAPI app
on port 8100 that drives the backend above through governed tool calls. It
runs on the [bastion](https://github.com/infoshikharpathak/bastion) SDK, the
same way Ironclad's agent-service does: Agent Forge runs the agent, and Anvil
checks every tool call against the policy in
[`anvil-fooddash.yaml`](anvil-fooddash.yaml).

- **Tools:** `list_restaurants`, `get_menu`, `get_user`, `get_order` and
  `list_orders` are trusted reads; `update_address`, `place_order` and
  `cancel_order` are untrusted writes (request and response are injection-scanned).
  `pay_for_order` requests payment through Ironclad: the model only names the
  order, and the service adds `channel: agent`. The user still has to
  approve it with the OTP in Ironclad. The agent never gets a tool for
  Ironclad itself.
- **Approval line:** after `pay_for_order` creates a pending request, the
  service (not the model) adds "Payment of $25.00 to FoodDash is waiting for
  your approval in your Ironclad app." after the reply, from the structured
  tool result, once. The model only confirms in its own words, without codes.
- **Requested payments:** when a reply is labeled `unrequested_action`,
  `app/requested.py` (no LLM) decides first: a `pay_for_order` for an order
  the user confirmed in this session ("yes", "I confirm", "go ahead"), with
  no withheld tool response in the session, isn't flagged.
- **Calls from other apps (A2A).** `POST /agent/chat` with
  `Authorization: Bearer ak_...`, a key FoodDash issued to that app
  (`python -m app.a2a issue chathub --user user_1`; only SHA-256 hashes are
  kept, in the file `FOODDASH_A2A_KEYS` points to). Identity comes from the
  key only: the `X-Bastion-Call-Chain` and `X-Bastion-Trace-Id` headers are
  recorded (the trace links to the caller's) but never trusted. An unknown
  key is 401; no key at all is FoodDash's own UI and agent, as before. Each
  key maps to a calling app and, for this demo, a fixed FoodDash user
  (`user_1`); production would use delegated user consent (OAuth-style), not
  a fixed mapping. Sessions another app starts are kept apart
  (`ext-<app>-<id>`) and get a restricted tool set, enforced in code from
  the authenticated caller, not the prompt: `list_restaurants`, `get_menu`,
  `place_order`, `get_order` and `cancel_order` (the last two only for orders
  placed in that session). `pay_for_order` is blocked with
  `external_caller_payment` ("Payments for orders started outside FoodDash
  must be completed in FoodDash."), so the order stays `pending`; anything
  else is `external_caller_tool`.
- **Unpaid orders time out.** An order still waiting for payment with no
  Ironclad payment request is cancelled after `UNPAID_ORDER_TIMEOUT_SECONDS`
  (900).
- **Rule: every tool that changes data needs the user's approval.** Either
  an OTP in Ironclad (`pay_for_order`) or a deterministic check before the
  call runs. `update_address` runs only if the new address appears in the
  user's messages of this turn or the previous two (or the agent proposed it
  and the user said yes); `cancel_order` only if the user asked to cancel in
  that window and it's the order they meant (by ID, or their only active
  order). Otherwise the call is blocked before it runs with
  `unrequested_action` ("Ask the user to confirm before changing the address
  / cancelling the order."), and it's in the trace. A turn with a withheld
  tool response never passes. New write tools must add one of the two.
- **Output check:** if the reply is blocked, or delivered but labeled
  `unrequested_action`, the service flags that turn's payment request
  through `POST /orders/{id}/payment/flag`, which forwards to Ironclad: it
  stays pending, and the OTP modal shows a warning.
- **Parameter checks:** Anvil checks every call against the tool's declared
  `parameters` in `anvil-fooddash.yaml`. A missing or mistyped field, or one
  the schema doesn't declare (an `amount` or `channel` on `pay_for_order`), is
  blocked as `invalid_params` before the backend sees it. If a tool fails at
  the backend, the model gets the HTTP status and the error body.
- **Sequence rules:** `place_order` is blocked until the agent has called
  `get_menu` in the same session, and `pay_for_order` until it has called
  `place_order`. The model gets a correction telling it what
  to call first, and the backend never sees the order.
- **User:** the agent's instructions call the user `demo_user`; the service
  pins `user_id` on `get_user`, `update_address` and `place_order` to
  `FOODDASH_USER_ID` (default `user_1`), so the model can't act as anyone else.

### Running both services

```bash
# terminal 1: the food backend on :8000
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --port 8000

# terminal 2: the agent service on :8100
cd agent-service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
uvicorn app.main:app --port 8100
```

Then:

```bash
curl -X POST http://localhost:8100/agent/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "What restaurants are available?"}'
```

The reply has `response`, `session_id` and `trace_id`. Send the `session_id`
back with the next message to keep the conversation (and the sequence state)
going. `GET /agent/traces/{session_id}` lists every tool call in the session
with Anvil's decision on it.

| Var | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | — (required) | The agent's model and Anvil's injection classifier |
| `OPENAI_MODEL` | `gpt-4o-mini` | Overrides the model in `anvil-fooddash.yaml` (library mode only) |
| `FOODDASH_BASE_URL` | `http://localhost:8000` | Where the tools reach the backend (library mode only) |
| `FOODDASH_USER_ID` | `user_1` | The backend user every user-scoped call acts as |
| `BASTION_URL` + `BASTION_KEY` | unset | Remote mode: load the bundle from the bastion control plane instead of the YAML |
| `ANVIL_CONFIG_PATH` | `../anvil-fooddash.yaml` | Config path (for containers, where the yaml is mounted) |

Agent-service tests stub the model and the backend (Anvil, Agent Forge and
bastion run for real), so they need no API key:

```bash
cd agent-service && pip install -r requirements-dev.txt && pytest
```

## Out of scope

No real database (in-memory state only), no auth, no card payments (only
the Ironclad demo bank), no refunds (an order cancelled just after its
payment was confirmed is logged as needing one), no real delivery tracking/maps, and no logscribe code in this
repo — zero coupling in either direction. The dashboard is read-only
observability for the demo itself, not a customer-facing ordering UI.
