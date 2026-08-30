"""The staff escalation queue (P7-T4).

Every refusal, every exhausted verification and every emergency ends up here.
It is the evidence that a refusal is a handover rather than a dead end.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

PRIORITY_STYLE = {
    "emergency": ("🚨", "error"),
    "urgent": ("⚠️", "warning"),
    "routine": ("📋", "info"),
}

REASON_LABEL = {
    "complex_symptoms": "Clinical question",
    "ada_accommodation": "Accessibility request",
    "provider_hold": "Provider hold",
    "upset_patient": "Wants a person",
    "billing_issue": "Billing",
    "prescription_refill": "Prescription / refill",
    "test_results": "Test results",
    "other": "Other",
}


def render(tickets: list[dict[str, Any]]) -> None:
    if not tickets:
        st.caption("Nothing escalated yet.")
        return

    emergencies = [t for t in tickets if t["priority"] == "emergency"]
    if emergencies:
        st.error(
            f"{len(emergencies)} emergency escalation(s) — these need a person now.",
            icon="🚨",
        )

    for ticket in tickets:
        icon, _ = PRIORITY_STYLE.get(ticket["priority"], ("📋", "info"))
        reason = REASON_LABEL.get(ticket["reason"], ticket["reason"])
        with st.container(border=True):
            head = f"{icon} **{reason}** · {ticket['priority']}"
            if ticket.get("patient_id"):
                head += f" · `{ticket['patient_id']}`"
            st.markdown(head)
            st.caption(ticket["notes"])
            st.caption(f"{ticket['created_at'][11:19]} · `{ticket['ticket_id']}`")
