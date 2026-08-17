from __future__ import annotations

"""
FoodDash Streamlit dashboard — a showcase view of the self-running demo:
live metrics, error breakdown, recent orders/deliveries, and a restaurant
browser. Talks to the FastAPI backend over HTTP; auto-refreshes so it works
as a "walk away, come back" live view, same as the backend itself.

Requires the backend to be running:
    uvicorn main:app --reload --port 8000

Run:
    streamlit run app.py
"""

import os
import time

import httpx
import plotly.graph_objects as go
import streamlit as st

API_URL = os.environ.get("FOODDASH_API_URL", "http://localhost:8000")

# Categorical slots from the design system's validated palette (references/palette.md
# in the dataviz skill) — used here as single-hue magnitude bars, not per-category
# identity, so slot order doesn't matter beyond picking three visually distinct hues.
BLUE = "#2a78d6"
AQUA = "#1baf7a"
RED = "#e34948"
MUTED = "#898781"
GRIDLINE = "#e1e0d9"

st.set_page_config(page_title="FoodDash", page_icon="🍔", layout="wide")


# ── API helpers ─────────────────────────────────────────────────────────────

def fetch(path: str, params: dict | None = None):
    try:
        resp = httpx.get(f"{API_URL}{path}", params=params, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError:
        return None


def format_uptime(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def bar_chart(counts: dict[str, int], color: str, height: int = 320):
    """A single-hue horizontal bar chart — bar length carries magnitude, labels
    carry category identity, so no per-bar categorical color is needed."""
    items = sorted(counts.items(), key=lambda kv: kv[1])
    labels = [k for k, _ in items]
    values = [v for _, v in items]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker_color=color,
            text=values,
            textposition="outside",
        )
    )
    fig.update_layout(
        height=height,
        margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(gridcolor=GRIDLINE, zeroline=False),
        yaxis=dict(gridcolor=GRIDLINE),
        font=dict(color=MUTED),
    )
    return fig


# ── Header ───────────────────────────────────────────────────────────────────

st.title("🍔 FoodDash")
st.caption("Self-running demo — live metrics from the backend, no manual traffic needed.")

with st.sidebar:
    st.subheader("Refresh")
    auto_refresh = st.checkbox("Auto-refresh", value=True)
    refresh_interval = st.slider("Interval (seconds)", 2, 30, 5)
    st.caption(f"Backend: {API_URL}")

stats = fetch("/stats")

if stats is None:
    st.error(f"Can't reach the backend at {API_URL}. Is `uvicorn main:app` running?")
    st.stop()

tab_dashboard, tab_restaurants, tab_orders, tab_deliveries = st.tabs(
    ["Dashboard", "Restaurants", "Orders", "Deliveries"]
)

# ── Dashboard ────────────────────────────────────────────────────────────────

with tab_dashboard:
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total orders", stats["total_orders"])
    col2.metric("Total deliveries", stats["total_deliveries"])
    col3.metric("Total errors", stats["total_errors"])
    col4.metric("Total requests", stats["total_requests"])
    col5.metric("Uptime", format_uptime(stats["uptime_seconds"]))

    st.divider()

    chart_col1, chart_col2 = st.columns(2)
    with chart_col1:
        st.subheader("Orders by status")
        if stats["orders_by_status"]:
            st.plotly_chart(bar_chart(stats["orders_by_status"], BLUE), use_container_width=True)
        else:
            st.info("No orders yet.")

    with chart_col2:
        st.subheader("Deliveries by status")
        if stats["deliveries_by_status"]:
            st.plotly_chart(bar_chart(stats["deliveries_by_status"], AQUA), use_container_width=True)
        else:
            st.info("No deliveries yet.")

    st.subheader("Errors by type")
    if stats["errors_by_type"]:
        st.plotly_chart(bar_chart(stats["errors_by_type"], RED, height=400), use_container_width=True)
    else:
        st.info("No errors yet.")

# ── Restaurants ──────────────────────────────────────────────────────────────

with tab_restaurants:
    st.subheader("Restaurants")
    restaurants = fetch("/restaurants") or []
    if not restaurants:
        st.info("No restaurants online.")
    for restaurant in restaurants:
        with st.expander(f"{restaurant['name']} — {restaurant['cuisine']}"):
            menu = fetch(f"/restaurants/{restaurant['id']}/menu") or []
            st.dataframe(
                [
                    {"item": item["name"], "price": f"${item['price']:.2f}", "available": item["available"]}
                    for item in menu
                ],
                use_container_width=True,
                hide_index=True,
            )

# ── Orders ───────────────────────────────────────────────────────────────────

with tab_orders:
    st.subheader("Recent orders")
    orders = fetch("/orders", params={"limit": 100}) or []
    if not orders:
        st.info("No orders yet.")
    else:
        st.dataframe(
            [
                {
                    "id": o["id"][:8],
                    "restaurant_id": o["restaurant_id"],
                    "user_id": o["user_id"],
                    "total": f"${o['total']:.2f}",
                    "status": o["status"],
                    "created_at": o["created_at"],
                }
                for o in orders
            ],
            use_container_width=True,
            hide_index=True,
        )

# ── Deliveries ───────────────────────────────────────────────────────────────

with tab_deliveries:
    st.subheader("Recent deliveries")
    deliveries = fetch("/delivery", params={"limit": 100}) or []
    if not deliveries:
        st.info("No deliveries yet.")
    else:
        st.dataframe(
            [
                {
                    "order_id": d["order_id"][:8],
                    "status": d["status"],
                    "driver_id": d["driver_id"],
                    "eta_seconds": d["eta_seconds"],
                }
                for d in deliveries
            ],
            use_container_width=True,
            hide_index=True,
        )

if auto_refresh:
    time.sleep(refresh_interval)
    st.rerun()
