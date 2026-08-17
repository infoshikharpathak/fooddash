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

## Quick start

### Docker Compose (Redis + app together)

```bash
docker compose up --build
```

The app starts generating traffic immediately — no separate script to run.

### Local dev (no Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# needs a Redis instance reachable at REDIS_URL (default redis://localhost:6379)
redis-server &

uvicorn main:app --reload
```

## Config (env vars)

| Var | Default | Meaning |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379` | Redis connection for order confirmation pub/sub |
| `INTERNAL_BASE_URL` | `http://localhost:8000` | Base URL services use to call each other and themselves |
| `AUTO_TRAFFIC` | `true` | Whether the traffic simulator runs on startup |
| `TRAFFIC_RPS` | `3` | New customer journeys started per second |
| `FAILURE_RATE` | `0.15` | Global scaling factor for every injected failure's odds |

## Demo flow

```bash
# terminal 1
docker compose up --build

# terminal 2, once fooddash is running
cd ../logscribe
logscribe watch --file /tmp/fooddash.log
```

Walk away. Traffic keeps flowing, ~15% of requests fail with varied error
types, and logscribe builds up a history of root-cause analyses you can come
back to.

## Out of scope

No frontend, no real database (in-memory state only), no auth, no real
payment processing, no real delivery tracking/maps, and no logscribe code in
this repo — zero coupling in either direction.
