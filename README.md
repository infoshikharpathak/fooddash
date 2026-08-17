# FoodDash

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
they talk to each other over real internal HTTP calls (not direct Python
calls) so the logs look like genuine service-to-service traffic — order
validates against restaurant via HTTP, delivery notifies order via HTTP, and a
`request_id` is propagated through every hop via the `X-Request-ID` header so
a whole customer journey can be correlated across services.

- **restaurant** (`services/restaurant.py`) — in-memory restaurants + menus.
- **order** (`services/order.py`) — order state machine, validates against
  restaurant over HTTP, publishes confirmed orders to Redis.
- **delivery** (`services/delivery.py`) — subscribes to confirmed orders over
  Redis pub/sub, runs each delivery through its lifecycle in the background,
  calls back to order over HTTP as status changes.
- **traffic simulator** (`traffic/simulator.py`) — launches on startup, drives
  full customer journeys (browse → menu → order → track/cancel) against the
  app's own HTTP endpoints indefinitely, including edge cases (ordering
  unavailable items, double-submitting, cancelling mid-delivery).
- **failure injection** (`core/failure_injection.py`) — a decorator that wraps
  already-working endpoints with a probabilistic chance of failing (timeout,
  connection error, internal server error, or an unfailing slow response).
  Every endpoint's real implementation always succeeds on its own; failures
  are layered on top, never baked into the business logic.

## Logging contract

This is the interface between the two repos. Every log line is a single JSON
object, written to both stdout and `/tmp/fooddash.log`:

```json
{"timestamp": "...", "service_name": "order", "level": "ERROR", "message": "...",
 "request_id": "...", "endpoint": "/orders", "duration_ms": 812,
 "error_type": "PaymentTimeoutError", "stack_trace": "..."}
```

`error_type`/`stack_trace` are only present on error-level logs.

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
counts). None of these have side effects or failure injection — purely for
observing state, same spirit as the log stream logscribe taps.

## Quick start

### Docker Compose (Redis + app + dashboard together)

```bash
docker compose up --build
```

Starts Redis, the backend (generating traffic immediately, no separate script
to run), and the dashboard at http://localhost:8501.

### Local dev (no Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# needs a Redis instance reachable at REDIS_URL (default redis://localhost:6379)
redis-server &

uvicorn main:app --reload

# in another terminal
streamlit run app.py
```

## Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379` | Redis connection for order confirmation pub/sub |
| `INTERNAL_BASE_URL` | `http://localhost:8000` | Base URL services use to call each other and themselves |
| `AUTO_TRAFFIC` | `true` | Whether the traffic simulator runs on startup |
| `TRAFFIC_RPS` | `3` | New customer journeys started per second |
| `FAILURE_RATE` | `0.15` | Global scaling factor for every injected failure's odds |
| `FOODDASH_API_URL` | `http://localhost:8000` | Backend URL the dashboard polls (set to `http://app:8000` under Docker Compose) |

## Demo flow

```bash
# terminal 1
docker compose up --build

# terminal 2, once fooddash is running
cd ../logscribe
logscribe watch --file /tmp/fooddash.log
```

Open http://localhost:8501 for the live dashboard, then walk away. Traffic
keeps flowing, ~15% of requests fail with varied error types, logscribe
builds up a history of root-cause analyses, and the dashboard's metrics and
charts update on their own.

## Out of scope

No real database (in-memory state only), no auth, no real payment
processing, no real delivery tracking/maps, and no logscribe code in this
repo — zero coupling in either direction. The dashboard is read-only
observability for the demo itself, not a customer-facing ordering UI.
