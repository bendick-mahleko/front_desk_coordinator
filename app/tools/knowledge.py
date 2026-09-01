"""The knowledge-base tool (R3).

One function, behind the same gate as every other tool. It takes a complaint in
the patient's own words and returns an *appointment type* — never a condition,
never a treatment, never a dose.

The safety property is structural rather than instructional: the retrieval is
hard-filtered to the `routing_only` tier, and the matched disease names are
consumed by `routing.combine()` and then dropped before the payload is built.
There is no field in the result that could carry one.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.knowledge.chunking import Tier, require_tiers
from app.knowledge.red_flags import Severity, severity_for
from app.knowledge.routing import DEFAULT, Routing, combine
from app.policy.decorator import current_audit, current_session
from app.store.session import Session, SubjectStatus
from app.tools.registry import knowledge_base, tool
from app.tools.schemas import AppointmentType, Modality

DISCLAIMER = (
    "I'm an AI assistant on the front desk, not a clinician. I can help you get "
    "in front of the right person at the right time, but only a doctor can tell "
    "you what is actually going on or what to do about it."
)


def _first_visit(routing: Routing, session: Session) -> Routing:
    """Correct a follow-up for someone who has never been here.

    The routing tables map a chronic condition to a review appointment, which
    is right for the patients those tables were written for and wrong for one
    who registered ten minutes ago. The gate refuses a follow_up for them, so
    without this the tool would recommend a visit type the very next call
    rejects — two layers of the same system disagreeing in front of a patient.

    A new-patient visit is in person by definition, so the modality moves with
    the type; leaving it as "either" would search telehealth slots that cannot
    host the appointment.
    """
    if session.status is not SubjectStatus.REGISTERED:
        return routing
    if routing.appointment_type is not AppointmentType.FOLLOW_UP:
        return routing
    return replace(
        routing,
        appointment_type=AppointmentType.NEW_PATIENT,
        modality=Modality.IN_PERSON,
    )


@tool("suggest_appointment_type")
def suggest_appointment_type(complaint: str) -> Any:
    """Work out what kind of appointment a described problem needs.

    Use this when a patient describes what is bothering them and you need to
    decide which visit type to search for. It returns a visit type, whether it
    should be in person, and how soon — the scheduling decision, nothing else.

    It does not tell you what the patient has, and you must not guess. Do not
    name a condition, do not suggest a treatment or a medicine, and do not say
    how serious the problem is. Say what kind of appointment you would book and
    offer to find a time.

    Tell the patient you are an AI assistant on the front desk and that a doctor
    is who diagnoses and treats. Say it plainly and once, not as a disclaimer
    bolted onto the end.

    If the result says the patient should be seen urgently, do not book a
    routine appointment — escalate to staff instead.
    """
    store = knowledge_base()
    # Resolved against the session role rather than named as a constant: §1.3
    # puts the filter at query construction, and one chokepoint for every
    # retrieval is what makes that reviewable.
    tiers = require_tiers([Tier.ROUTING_ONLY], current_session().effective_role)
    hits = store.search(complaint, tiers=tiers, k=3)

    # Audited so a reviewer can see what was retrieved and on what score, even
    # though none of it reaches the patient.
    current_audit().note(
        "retrieval",
        {
            "tool": "suggest_appointment_type",
            "tiers": sorted(t.value for t in tiers),
            "hits": [{"chunk_id": h.chunk_id, "score": round(h.score, 3)} for h in hits],
        },
    )

    if not hits:
        # No confident match. The front desk still has a sensible default, and
        # guessing from a weak neighbour would be worse than admitting it.
        routing = DEFAULT
        confidence = "none"
    else:
        routing = combine([hit.disease for hit in hits])
        confidence = "high" if hits[0].score >= 0.55 else "low"

    routing = _first_visit(routing, current_session())

    payload: dict[str, Any] = {
        **routing.as_payload(),
        "match_confidence": confidence,
        "disclaimer": DISCLAIMER,
    }

    if routing.urgent or any(severity_for(h.disease) is Severity.URGENT for h in hits):
        payload["next_step"] = (
            "This should be looked at by a clinician today rather than booked as a "
            "routine appointment. Do not name a condition. Tell the patient you want "
            "a member of clinical staff to speak with them, include the disclaimer "
            "above in your own words, and call escalate_to_staff with "
            "reason='complex_symptoms'."
        )
    else:
        window = routing.within_days
        payload["next_step"] = (
            f"Offer to search for a {routing.appointment_type.value} appointment "
            f"within about {window} days"
            + (" , in person" if routing.modality.value == "in_person" else "")
            + ". Say in your own words that you are an AI assistant on the front desk "
            "and that a doctor is who can actually diagnose and treat this. Do not "
            "name a condition or suggest any treatment or medicine."
        )
    return payload
