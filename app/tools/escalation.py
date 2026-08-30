"""Human escalation — specification §4.12.

The one path that always works. Every other failure routes here, so this
function has no failure mode of its own.
"""

from __future__ import annotations

from typing import Any

from app.policy.decorator import current_session
from app.policy.redaction import redact_text
from app.tools.registry import backends, tool
from app.tools.schemas import EscalationReason, Priority

EMERGENCY_INSTRUCTION = (
    "Tell the patient immediately to hang up and call their local emergency number "
    "or go to the nearest emergency department. Do this before anything else."
)


@tool("escalate_to_staff")
def escalate_to_staff(
    reason: EscalationReason,
    priority: Priority = Priority.ROUTINE,
    notes: str = "",
    patient_id: str | None = None,
) -> Any:
    """Hand the conversation to a member of staff.

    Always available, and always honoured — if a patient asks to speak to a
    person, call this without argument.

    Use it for prescription refills, test results, complex symptoms, ADA
    accommodation requests and billing questions, rather than trying to resolve
    them yourself. Also use it when a function fails and the patient needs a
    human, or when identity verification has been exhausted.

    Write brief, factual notes: what the patient needs and what you already
    tried. Include the patient_id only if you have one. Use
    priority='emergency' only under the clinic's emergency-transfer policy, and
    when you do, tell the patient to contact emergency services straight away.
    """
    session = current_session()

    # A pattern sweep, not a blanking: staff need to be able to read the note,
    # but a phone number or date of birth that drifted into it should not be
    # copied into the ticket.
    safe_notes = redact_text(notes) if notes else "(no notes supplied)"

    ticket = backends().staff.escalate(
        reason,
        priority,
        safe_notes,
        # Included only when it is actually known (spec §4.12).
        patient_id=patient_id or session.patient_id,
    )

    payload: dict[str, Any] = {
        "ticket_id": ticket.ticket_id,
        "reason": ticket.reason.value,
        "priority": ticket.priority.value,
        "escalated": True,
    }
    if priority is Priority.EMERGENCY:
        payload["next_step"] = EMERGENCY_INSTRUCTION
    else:
        payload["next_step"] = (
            "Tell the patient a member of staff will follow up, and say roughly when."
        )
    return payload
