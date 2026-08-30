"""Secure patient text messages — specification §4.10."""

from __future__ import annotations

from typing import Any

from app.ports import DeliveryStatus
from app.tools.registry import backends, key_for, tool
from app.tools.schemas import MessageType


@tool("send_secure_text")
def send_secure_text(
    phone_number: str,
    message_type: MessageType,
    appointment_details: str | None = None,
) -> Any:
    """Send a secure text to a patient's mobile number.

    Confirm with the patient that the number is theirs before calling.

    Directions can be sent to a number the patient confirms as their own.
    Intake forms, telehealth links, appointment confirmations and portal access
    all require identity verification first.

    Keep health details out of text messages. Use appointment_details only for
    an appointment confirmation.

    If delivery cannot be confirmed, tell the patient rather than assuming it
    arrived.
    """
    receipt = backends().messages.send(
        phone_number,
        message_type,
        appointment_details=appointment_details,
        idempotency_key=key_for(
            "send_secure_text", phone_number=phone_number, message_type=message_type.value
        ),
    )

    payload: dict[str, Any] = {
        "message_id": receipt.message_id,
        "message_type": receipt.message_type.value,
        "delivery_status": receipt.delivery_status.value,
    }
    if receipt.delivery_status is DeliveryStatus.DELIVERED:
        payload["next_step"] = "Tell the patient the message has been sent."
    else:
        # spec §4.10 — inform the patient when delivery cannot be confirmed.
        payload["next_step"] = (
            "Delivery could not be confirmed. Tell the patient it may not arrive, and "
            "offer to try again or to have staff follow up."
        )
    return payload
