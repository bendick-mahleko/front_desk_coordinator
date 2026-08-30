"""The SMS outbox view (P7-T3).

Nothing leaves the machine. This is where a sent message actually goes, and it
is the demo surface for specification §4.10 — including the case that matters
most, a message the gateway will not confirm.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

STATUS_STYLE = {
    "delivered": ("✅", "Delivered"),
    "queued": ("⏳", "Queued"),
    "unconfirmed": ("⚠️", "Delivery not confirmed"),
    "failed": ("🚫", "Failed"),
}

MESSAGE_LABEL = {
    "intake_forms": "Intake forms",
    "appointment_confirmation": "Appointment confirmation",
    "telehealth_link": "Telehealth link",
    "directions": "Directions",
    "portal_access": "Portal access",
}


def render(messages: list[dict[str, Any]]) -> None:
    if not messages:
        st.caption("No messages sent yet.")
        return

    unconfirmed = sum(1 for m in messages if m["delivery_status"] == "unconfirmed")
    if unconfirmed:
        st.warning(
            f"{unconfirmed} message(s) could not be confirmed. The assistant must tell "
            "the patient rather than assume they arrived.",
            icon="⚠️",
        )

    for message in messages:
        icon, label = STATUS_STYLE.get(
            message["delivery_status"], ("•", message["delivery_status"])
        )
        kind = MESSAGE_LABEL.get(message["message_type"], message["message_type"])
        with st.container(border=True):
            st.markdown(f"**{kind}** → {message['phone_number']}")
            st.caption(f"{icon} {label} · {message['sent_at'][11:19]} · `{message['message_id']}`")
