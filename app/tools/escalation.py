"""Human escalation — specification §4.12.

The one path that always works. Every other failure routes here, so this
function has no failure mode of its own.
"""

from __future__ import annotations

from typing import Any

from app.policy.decorator import current_audit, current_session
from app.policy.redaction import redact_text
from app.tools.registry import backends, knowledge_base, tool
from app.tools.schemas import EscalationReason, Priority


def _clinician_briefing(reason: EscalationReason, notes: str) -> str:
    """Retrieve treatment context for a clinical escalation.

    Two steps rather than one, and the reason matters. Searching the clinician
    tier directly compares a *symptom* description against a *treatment*
    paragraph — different vocabularies, so the match is weak and sometimes
    wrong: a note describing cough, fever and chest pain retrieved "Common Cold"
    ahead of Pneumonia. Matching symptoms against symptoms and then fetching
    that condition's management chunk by id is both more accurate and exact.

    Only for complex_symptoms: a billing or accessibility escalation has no use
    for clinical content, and retrieving anyway would put it on tickets that do
    not need it. Failure is silent by design — a briefing is a convenience for
    staff, and losing it must never stop a patient being escalated.
    """
    if reason is not EscalationReason.COMPLEX_SYMPTOMS:
        return ""
    try:
        from app.knowledge.chunking import Tier, require_tiers, slug
        from app.knowledge.red_flags import BRIEFING_MIN_SCORE
        from app.policy.decorator import current_session

        role = current_session().effective_role
        store = knowledge_base()

        # Two retrievals, two tiers, both resolved through the chokepoint.
        #
        # The symptom search is ordinary patient-role retrieval. The management
        # fetch is the §4.12 exemption — a complex_symptoms ticket must carry
        # clinician-only reference context, and this is raised from a patient
        # session — so it names `staff_ticket=True` explicitly. That keyword is
        # the only way to reach CLINICIAN_ONLY from a patient role anywhere in
        # the codebase, which is what makes the exemption reviewable.
        symptom_tiers = require_tiers([Tier.ROUTING_ONLY], role)
        ticket_tiers = require_tiers([Tier.CLINICIAN_ONLY], role, staff_ticket=True)

        matches = store.search(notes, tiers=symptom_tiers, k=2, min_score=BRIEFING_MIN_SCORE)
        lines = []
        for match in matches:
            chunk = store.get(f"{slug(match.disease)}::management", tiers=ticket_tiers)
            if chunk is not None:
                lines.append(f"- ({match.score:.2f}) {chunk.text}")
    except Exception:  # noqa: BLE001
        return ""
    return "\n".join(lines)


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

    Before calling this, tell the patient what you were unable to see or do, in
    one sentence, and then who can help. "I can't see what a visit will cost —
    I don't have access to pricing or copay information, but billing can tell
    you" is useful; "I can't help with billing questions" leaves them knowing
    neither why nor what happens next.

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

    # R4 — clinical reference material for the person picking this up. It is
    # retrieved from the clinician tier, attached to the ticket, and never
    # returned in the patient-facing payload below.
    briefing = _clinician_briefing(reason, safe_notes)
    if briefing:
        safe_notes = f"{safe_notes}\n\n--- reference material, not a recommendation ---\n{briefing}"

    ticket = backends().staff.escalate(
        reason,
        priority,
        safe_notes,
        # Included only when it is actually known (spec §4.12).
        patient_id=patient_id or session.patient_id,
    )

    current_audit().note(
        "escalation",
        {
            "ticket_id": ticket.ticket_id,
            "reason": ticket.reason.value,
            "priority": ticket.priority.value,
            "patient_id": ticket.patient_id,
        },
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
        # Read by the model immediately before it composes the reply, which is
        # the highest-leverage place to put this: the system prompt and the tool
        # description both say it, and neither reliably changed the wording.
        payload["next_step"] = (
            "In your reply, first name what you could not see or do — for a cost "
            "question, that you have no access to pricing or copay information — "
            "then say a member of staff will follow up and roughly when. Do not "
            "just say you cannot help."
        )
    return payload
