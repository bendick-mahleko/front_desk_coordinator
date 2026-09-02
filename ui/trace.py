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

from ui import design, diagrams

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
        # Before the first message this panel is most of the screen, and one grey
        # line of prose made the page look broken rather than ready. It now says
        # what will appear here and what to watch for.
        st.markdown(
            '<div class="ds-empty">'
            "<strong>Nothing has been asked yet.</strong><br>"
            "Every function the model proposes will appear here with the gate's "
            "verdict — the six checks it passed, and the one that stopped it if "
            "any did.<br><br>"
            "The two worth watching: ask for appointments <em>before</em> "
            "verifying and the gate refuses at <strong>authorization</strong>; "
            "verify, then ask again, and the same call is allowed."
            "</div>",
            unsafe_allow_html=True,
        )
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
        elif kind == "retrieval":
            _retrieval(event)
        elif kind == "clinical_retrieval":
            _clinical_retrieval(event)


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

    code = event.get("code") or ("" if allowed else "denied")
    mark = design.ALLOW_GLYPH if allowed else design.DENY_GLYPH
    header = f"{mark}  {function} — {'allowed' if allowed else code}"

    with st.expander(header, expanded=not allowed):
        # The pipeline replaces a pair of metrics. "Required: verified / Session
        # is: identified" told you the rungs; it did not tell you that four
        # earlier checks had passed and two later ones never ran, which is the
        # part that makes the design legible.
        st.markdown(diagrams.gate_pipeline(allowed, event.get("code")), unsafe_allow_html=True)
        st.markdown(design.verdict(allowed), unsafe_allow_html=True)

        if not allowed:
            explanation = DENIAL_HELP.get(code, "")
            st.markdown(f"**`{code}`** — {explanation}")
            if required and required != "—":
                st.caption(f"needed **{required}**, session is **{actual}**")
        elif required and required != "—":
            st.caption(f"needed **{required}**, session is **{actual}**")

        footer = []
        if event.get("rule"):
            footer.append(f"`{event['rule']}`")
        if latency is not None:
            footer.append(f"{latency} ms")
        if footer:
            st.caption(" · ".join(footer))

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


def _retrieval(event: dict[str, Any]) -> None:
    """A patient-side retrieval. §1.3 as a picture: the clinician band is drawn
    locked, because the filter was built from the role before the query ran."""
    queried = list(event.get("tiers") or [])
    with st.expander(f"Knowledge retrieval · {len(event.get('hits') or [])} hit(s)"):
        st.markdown(
            diagrams.tier_bands(queried, ["patient_safe", "routing_only"]),
            unsafe_allow_html=True,
        )
        for hit in event.get("hits") or []:
            st.caption(f"`{hit.get('chunk_id')}` · {hit.get('score')}")


def _clinical_retrieval(event: dict[str, Any]) -> None:
    """A clinical-session retrieval. The clinician tier is available here, which
    is the one respect in which the roles differ (§1.2)."""
    effective = event.get("effective_tier")
    queried = effective if isinstance(effective, list) else [effective] if effective else []
    label = event.get("tool", "retrieval")
    with st.expander(f"{label} · {event.get('outcome', '')}"):
        st.markdown(
            diagrams.tier_bands(
                [t for t in queried if t],
                ["patient_safe", "routing_only", "clinician_only"],
                surface="clinical",
            ),
            unsafe_allow_html=True,
        )
        if event.get("staff_id"):
            st.caption(f"staff `{event['staff_id']}`")


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
