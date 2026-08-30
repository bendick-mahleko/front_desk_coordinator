"""The trace panel (P7-T2).

This is the demo surface for the whole design. A reviewer watching it sees the
gate refuse a call and then allow the same call after verification — which is
the argument the rest of the system exists to make, happening live.

It renders the same redacted view the audit log stores. The panel is a window
onto the decisions, not a back door around the redactor.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

LEVEL_ORDER = ["open", "identified", "verified", "number_confirmed"]

DENIAL_HELP = {
    "verification_required": "Identity has not been verified to the level this call needs.",
    "unknown_reference": "That identifier never came from a result in this conversation.",
    "precondition_failed": "A required earlier step has not happened yet.",
    "invalid_arguments": "The call did not match the function's schema.",
    "unknown_function": "No such function.",
}


def render(events: list[dict[str, Any]]) -> None:
    """Draw the trace for one turn."""
    if not events:
        st.caption("No activity yet. Send a message to see the gate at work.")
        return

    for event in events:
        kind = event.get("kind")
        if kind == "prescreen":
            _prescreen(event)
        elif kind == "gate":
            _gate(event)
        elif kind == "result":
            _result(event)
        elif kind == "refusal":
            st.error(f"Model refusal · {event.get('category', 'unspecified')}", icon="🚫")
        elif kind == "error":
            st.error(f"Turn failed · {event.get('error')}", icon="⚠️")
        elif kind in {"verification", "escalation"}:
            _note(kind, event)


def _prescreen(event: dict[str, Any]) -> None:
    label = event.get("label", "routine")
    source = event.get("source", "")
    if label == "emergency":
        st.error(
            f"Pre-screen · **emergency** (matched by {source}"
            + (f": {event['matched']!r}" if event.get("matched") else "")
            + ") — the agent loop was not entered",
            icon="🚨",
        )
    elif label == "routine":
        st.caption(f"Pre-screen · routine ({source})")
    else:
        st.warning(f"Pre-screen · {label} ({source})", icon="⚠️")


def _gate(event: dict[str, Any]) -> None:
    function = event.get("function", "?")
    allowed = event.get("allowed", False)
    required = event.get("required") or "—"
    actual = event.get("actual") or "—"
    latency = event.get("latency_ms")

    verdict = "ALLOW" if allowed else "DENY"
    icon = "✅" if allowed else "⛔"
    header = f"{icon} `{function}` — {verdict}"

    with st.expander(header, expanded=not allowed):
        left, right = st.columns(2)
        left.metric("Required", required)
        right.metric("Session is", actual)

        if not allowed:
            code = event.get("code") or "denied"
            st.error(f"**{code}** — {DENIAL_HELP.get(code, '')}", icon="⛔")

        if event.get("rule"):
            st.caption(f"Rule: `{event['rule']}`")
        if latency is not None:
            st.caption(f"Decided in {latency} ms")

        args = event.get("args") or {}
        if args:
            st.caption("Arguments as recorded (redacted):")
            st.json(args, expanded=False)


def _result(event: dict[str, Any]) -> None:
    status = event.get("status", "ok")
    function = event.get("function", "?")
    if status == "ok":
        st.caption(f"↳ `{function}` returned ok · session now **{event.get('session_status')}**")
    else:
        st.caption(f"↳ `{function}` returned `{status}`")


def _note(kind: str, event: dict[str, Any]) -> None:
    if kind == "verification":
        outcome = "verified" if event.get("verified") else "not verified"
        methods = ", ".join(event.get("methods", []))
        remaining = event.get("attempts_remaining")
        st.info(
            f"Verification · **{outcome}** using {methods or '—'} · {remaining} attempt(s) left",
            icon="🔑",
        )
    else:
        st.warning(
            f"Escalated · {event.get('reason')} at **{event.get('priority')}** "
            f"({event.get('ticket_id')})",
            icon="📣",
        )
