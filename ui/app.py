"""Streamlit client — Phase 0 stub.

Shows whether the API is reachable and what its startup checks say. The chat
pane, trace panel, SMS outbox and staff queue arrive in Phase 7; this exists so
every later phase has a place to render into.
"""

from __future__ import annotations

import os

import httpx
import streamlit as st

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
REQUEST_TIMEOUT = 5.0

STATUS_LABEL = {
    "ok": ("✅", "All startup checks passed."),
    "degraded": ("⚠️", "The service is running, but something it needs is missing."),
}

st.set_page_config(page_title="AI Front Desk Coordinator", page_icon="🏥", layout="centered")

st.title("AI Front Desk Coordinator")
st.caption("Prototype · Phase 0 skeleton — chat arrives in Phase 7")


def fetch_health() -> tuple[dict | None, str | None]:
    """Return (payload, error). Exactly one is None."""
    try:
        response = httpx.get(f"{API_BASE_URL}/health", timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json(), None
    except httpx.HTTPStatusError as exc:
        return None, f"The API answered with {exc.response.status_code}."
    except httpx.RequestError:
        return None, (
            f"Could not reach the API at {API_BASE_URL}. "
            "Start it with: uv run uvicorn app.main:app --reload"
        )


with st.sidebar:
    st.subheader("Connection")
    st.code(API_BASE_URL, language=None)
    refresh = st.button("Check again", use_container_width=True)

payload, error = fetch_health()

if error is not None:
    st.error(error)
else:
    assert payload is not None
    status = payload.get("status", "degraded")
    icon, message = STATUS_LABEL.get(status, ("⚠️", "Unknown status."))

    st.subheader(f"{icon} {payload.get('service', 'Service')} — {status}")
    st.write(message)

    left, right = st.columns(2)
    left.metric("Version", payload.get("version", "—"))
    right.metric("Environment", payload.get("environment", "—"))

    st.markdown("**Startup checks**")
    for name, value in payload.get("checks", {}).items():
        label = name.replace("_", " ")
        if value == "ok":
            st.success(f"{label}: ok", icon="✅")
        elif value == "missing":
            st.warning(f"{label}: missing", icon="⚠️")
        else:
            st.error(f"{label}: {value}", icon="🚫")

    detail = payload.get("detail", [])
    if detail:
        with st.expander("Detail"):
            for line in detail:
                st.text(line)
