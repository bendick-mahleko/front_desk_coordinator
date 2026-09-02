"""The front-desk chat client (P7-T1, P7-T5).

Chat on the left, the policy gate's reasoning on the right. The split is the
point: a reviewer can watch a call be refused and then allowed, in the same
conversation, without reading any code.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import streamlit as st

from ui import design, diagrams
from ui import outbox as outbox_view
from ui import queue as queue_view
from ui import settings as settings_view
from ui import trace as trace_view

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
TURN_TIMEOUT = 180.0

STATUS_BADGE = {
    "none": ("Anonymous", "Nothing protected can be disclosed.", False),
    "identified": ("Identified", "A record is known. Still nothing protected.", True),
    "verified": ("Verified", "Protected information and changes are permitted.", True),
    "registered": ("Registered", "May book. May not read an existing record.", True),
    "locked": ("Locked", "Verification attempts exhausted. Staff only.", True),
}
# (label, what it permits, whether the session has established anybody).
#
# A comment, not a docstring. Streamlit's "magic" renders a bare expression at
# the top level of a page script, so a string sitting after an assignment becomes
# page content — this one appeared above the title as two paragraphs of prose
# about colour choices, which is how it was found.
#
# The coloured-circle emoji are gone. Five circles differing only in hue is
# colour alone, which is the failure the design rules exist to avoid, and a red
# circle beside "Locked" said nothing the word did not.

EXAMPLES = [
    "Are you open right now?",
    "I'm Amara Osei, born 1978-03-04 — what appointments do I have?",
    "My zip is 98101 and my phone is 206-555-0142.",
    "I'd like to book a follow-up next week.",
    "I'm having chest pain and need to be seen today.",
    "Can you tell me what my test results mean?",
]

st.set_page_config(page_title="AI Front Desk Coordinator", page_icon="🏥", layout="wide")


# ------------------------------------------------------------------ api ---


def post_turn(message: str, session_id: str | None) -> tuple[str | None, list[dict], str, str]:
    """Send one turn and consume the SSE stream.

    Returns (session_id, trace events, reply, stopped_early).
    """
    payload: dict[str, Any] = {"message": message}
    if session_id:
        payload["session_id"] = session_id

    events: list[dict] = []
    reply = ""
    stopped = ""
    new_session = session_id

    with httpx.stream(
        "POST", f"{API_BASE_URL}/chat", json=payload, timeout=TURN_TIMEOUT
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            data = json.loads(line[5:].strip())
            if "session_id" in data and data.get("kind") is None:
                new_session = data["session_id"]
            elif data.get("kind") == "done":
                reply = data.get("reply", "")
                stopped = data.get("stopped_early") or ""
            else:
                events.append(data)
    return new_session, events, reply, stopped


def get_json(path: str) -> Any:
    try:
        response = httpx.get(f"{API_BASE_URL}{path}", timeout=10.0)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError:
        return None


# ---------------------------------------------------------------- state ---

for key, default in [
    ("session_id", None),
    ("messages", []),
    ("last_trace", []),
    ("history", []),
]:
    st.session_state.setdefault(key, default)


def reset_session() -> None:
    """Start a new conversation. The old session stays in the audit log."""
    st.session_state.session_id = None
    st.session_state.messages = []
    st.session_state.last_trace = []
    st.session_state.history = []


# --------------------------------------------------------------- layout ---

st.markdown(design.stylesheet("patient"), unsafe_allow_html=True)
st.title("AI Front Desk Coordinator")
st.markdown(
    design.band(
        "<strong>Patient channel.</strong> The assistant can schedule, verify and "
        "escalate. It cannot give medical advice, and it holds no clinical "
        "capability to be talked into."
    ),
    unsafe_allow_html=True,
)

health = get_json("/health")
if health is None:
    st.error(
        f"Could not reach the API at {API_BASE_URL}. Start it with:\n\n"
        "`uv run uvicorn app.main:app --reload`",
        icon="🚫",
    )
    st.stop()

with st.sidebar:
    summary = (
        get_json(f"/session/{st.session_state.session_id}") if st.session_state.session_id else None
    )
    status = (summary or {}).get("status", "none")
    label, explanation, established = STATUS_BADGE.get(status, STATUS_BADGE["none"])

    st.markdown('<div class="ds-label">Session</div>', unsafe_allow_html=True)
    chip = "ds-chip" if established else "ds-chip-quiet"
    st.markdown(f'<span class="{chip}">{label}</span>', unsafe_allow_html=True)
    st.caption(explanation)

    # The ladder, always on screen. It used to be two metrics inside whichever
    # gate expander happened to be open — state expressed as an event, when a
    # conversation has exactly one position on the §3 ladder at a time.
    st.markdown(diagrams.verification_ladder(status), unsafe_allow_html=True)

    if summary:
        st.caption(f"Turn {summary['turn_index']} · `{summary['session_id']}`")
        if summary.get("patient_id"):
            st.caption(f"Record: `{summary['patient_id']}`")

    st.button("Start a new conversation", on_click=reset_session, use_container_width=True)

    ledger = (summary or {}).get("ledger") or {}
    if any(ledger.values()):
        st.divider()
        st.markdown('<div class="ds-label">What it has been handed</div>', unsafe_allow_html=True)
        st.markdown(diagrams.provenance_ledger(ledger), unsafe_allow_html=True)
        st.caption(
            "An id may only be passed into a function if a result produced it. "
            "Anything not shown here is an unknown reference."
        )

    st.divider()
    config = get_json("/config") or {}
    model = config.get("language_model", {})
    knowledge = config.get("knowledge_base", {})

    st.markdown('<div class="ds-label">Running with</div>', unsafe_allow_html=True)
    st.markdown(f"`{model.get('agent_model', health.get('agent_model', '?'))}`")
    st.caption(f"embeddings: `{knowledge.get('embedding_model', '—')}`")
    st.caption(f"{knowledge.get('chunks', 0)} knowledge chunks · see the Settings tab")
    if health.get("status") != "ok":
        st.warning("Service is degraded — see /health.", icon="⚠️")

    st.divider()
    st.markdown('<div class="ds-label">Try saying</div>', unsafe_allow_html=True)
    for example in EXAMPLES:
        st.caption(f"· {example}")

chat_column, trace_column = st.columns([3, 2], gap="large")

with chat_column:
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    prompt = st.chat_input("Type a message…")
    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"), st.spinner("Thinking…"):
            try:
                session_id, events, reply, stopped = post_turn(prompt, st.session_state.session_id)
            except httpx.HTTPError as exc:
                st.error(f"The request failed: {exc}", icon="🚫")
                st.stop()

            st.session_state.session_id = session_id
            st.session_state.last_trace = events
            st.session_state.history.append({"turn": prompt, "events": events})
            st.markdown(reply)
            if stopped == "emergency":
                st.error("Emergency path — the agent loop was not entered.", icon="🚨")

        st.session_state.messages.append({"role": "assistant", "content": reply})
        st.rerun()

with trace_column:
    tabs = st.tabs(["Policy gate", "Outbox", "Staff queue", "Settings"])

    with tabs[0]:
        st.caption(
            "Every function call the model proposed, and what the gate decided. "
            "Denials are expanded."
        )
        trace_view.render(st.session_state.last_trace)
        if len(st.session_state.history) > 1:
            with st.expander("Earlier turns"):
                for entry in reversed(st.session_state.history[:-1]):
                    st.caption(f"— {entry['turn'][:60]}")
                    trace_view.render(entry["events"])

    with tabs[1]:
        outbox_view.render(get_json("/outbox") or [])

    with tabs[2]:
        queue_view.render(get_json("/staff/queue") or [])

    with tabs[3]:
        st.caption(
            "What this process is actually running with. Read from the live service, "
            "not from a config file."
        )
        settings_view.render(config or get_json("/config"))
