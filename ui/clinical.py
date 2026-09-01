"""The Clinical Assistant surface (C7, spec r3 §3.2, §4.13).

A **separate Streamlit app**, run on its own port, not a tab beside the patient
chat. §3.2 says a clinical session is never established on a patient-facing
channel, and putting the clinician's window next to the patient's in one browser
app would blur exactly the boundary that sentence draws — in a demo, where the
boundary is the thing being demonstrated.

    uv run streamlit run ui/clinical.py --server.port 8502

The visual identity is deliberately different from the patient surface for the
same reason: somebody glancing at a screen should be able to tell which side of
the boundary they are on without reading a word.

§4.13 requires the established role and its scope to be stated once at session
start. That is this page's header, not a log line.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import streamlit as st

API = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000")

st.set_page_config(page_title="Clinical review — staff only", page_icon="🩺", layout="wide")

# Not the patient palette. A clinician who cannot tell at a glance which surface
# they are on is one keystroke from typing a patient's details into the wrong one.
st.markdown(
    """
    <style>
      .stApp { background-color: #101820; }
      .clin-banner {
        border-left: 4px solid #3ea6a6; background: #16222c; color: #d6e6e6;
        padding: 0.75rem 1rem; margin-bottom: 0.5rem; font-size: 0.9rem;
      }
      .clin-scope { color: #9fc3c3; font-size: 0.82rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def post(path: str, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        response = httpx.post(f"{API}{path}", json=body or {}, timeout=30)
        response.raise_for_status()
        return dict(response.json())
    except httpx.HTTPStatusError as exc:
        st.error(f"{exc.response.status_code}: {exc.response.json().get('detail', 'refused')}")
        return None
    except httpx.HTTPError as exc:
        st.error(f"The service did not respond: {type(exc).__name__}")
        return None


def get(path: str) -> dict[str, Any] | None:
    try:
        response = httpx.get(f"{API}{path}", timeout=15)
        response.raise_for_status()
        return dict(response.json())
    except httpx.HTTPError:
        return None


def stream_turn(session_id: str, message: str) -> tuple[str, list[dict[str, Any]]]:
    """One turn. Returns the reply and the trace events."""
    reply, events, event = "", [], None
    with httpx.stream(
        "POST", f"{API}/chat", json={"message": message, "session_id": session_id}, timeout=180
    ) as response:
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
                if payload.get("kind") == "done":
                    reply = payload.get("reply", "")
                elif event != "session":
                    events.append(payload)
    return reply, events


# --------------------------------------------------------------- session ---

for key, default in [("session_id", None), ("messages", []), ("scope_stated", False)]:
    if key not in st.session_state:
        st.session_state[key] = default

config = get("/config") or {}
clinical_config = config.get("clinical", {})

st.title("🩺 Clinical review")
st.caption("Staff channel. Not a patient-facing surface.")

if not clinical_config.get("enabled"):
    st.warning(
        "Clinical review is not enabled for this clinic. Set `clinical.enabled` in "
        "clinic.yaml. Nothing on this page will work until it is.",
        icon="⚠️",
    )
    st.stop()

with st.sidebar:
    st.markdown("### Session")
    if st.session_state.session_id is None:
        if st.button("Establish clinical session", type="primary", use_container_width=True):
            established = post("/clinical/session")
            if established:
                st.session_state.session_id = established["session_id"]
                st.session_state.messages = []
                st.session_state.scope_stated = False
                st.rerun()
        st.caption(
            "Establishing a session is not authenticating. The session starts with "
            "no capabilities and the assistant will ask for a credential."
        )
    else:
        summary = get(f"/session/{st.session_state.session_id}") or {}
        st.code(st.session_state.session_id, language=None)
        st.caption(f"status: {summary.get('status', '?')}")
        if st.button("End session", use_container_width=True):
            # §3.2 — expiry requires re-authentication, and re-authenticating
            # means a new session. Ending one here is the honest equivalent.
            st.session_state.session_id = None
            st.session_state.messages = []
            st.rerun()

    st.divider()
    st.markdown("### This clinic")
    st.caption(f"Session length: {clinical_config.get('session_minutes')} minutes")
    st.caption(f"Eligible channels: {', '.join(clinical_config.get('channels', [])) or '—'}")
    st.caption("Licensed roles accepted:")
    for role in clinical_config.get("permitted_roles", []):
        st.caption(f"  • {role.replace('_', ' ')}")
    st.divider()
    st.caption(
        "Reference material compiled from a fixed indexed source set, for "
        "clinician review. Not a formulary and not a guideline service."
    )

if st.session_state.session_id is None:
    st.info(
        "Establish a session to begin. You will be asked to authenticate with a "
        "staff identifier and a credential token from the clinic's identity "
        "provider — never a password.",
        icon="🔐",
    )
    st.stop()

# --------------------------------------------------------------- transcript ---

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        for note in message.get("trace", []):
            st.caption(note)

prompt = st.chat_input("Describe a presentation, or ask about a condition or a medication")
if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"), st.spinner("Retrieving…"):
        reply, events = stream_turn(st.session_state.session_id, prompt)
        st.markdown(reply or "_no reply_")

        # The trace, in a clinician-legible form: which function ran, on which
        # tier, and what it cited. §4.14's audit requirement is the log; this is
        # the same information where the person can see it.
        notes: list[str] = []
        for event in events:
            kind = event.get("kind")
            if kind == "gate":
                decision = "allowed" if event.get("allowed") else f"denied ({event.get('code')})"
                notes.append(f"`{event.get('function')}` — {decision}")
            elif kind == "clinical_auth":
                notes.append(
                    f"authentication: {event.get('outcome')} ({event.get('asserted_role', '—')})"
                )
            elif kind == "clinical_retrieval":
                tier = event.get("effective_tier") or event.get("requested_tier")
                chunks = event.get("chunks") or []
                notes.append(f"retrieved: tier `{tier}`, {len(chunks)} chunk(s)")
        for note in notes:
            st.caption(note)

    st.session_state.messages.append({"role": "assistant", "content": reply, "trace": notes})
