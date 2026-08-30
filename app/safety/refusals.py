"""The refusal set — six topics that are handed over rather than answered.

Specification §1 and §7 say the assistant must not give diagnoses, clinical
advice, triage, prescription decisions, test-result interpretation or billing
decisions.

Each one maps to an escalation reason, because a refusal here is a *handover*,
not a dead end. "I can't help with that" and nothing else leaves the patient
worse off than when they called.

This module is a routing table rather than a filter. The system prompt tells the
model what to decline; this says where a declined request should go, and gives
the orchestrator a way to attach the right reason without the model choosing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.tools.schemas import EscalationReason


class RefusedTopic(StrEnum):
    DIAGNOSIS = "diagnosis"
    TRIAGE = "triage"
    PRESCRIPTIONS = "prescriptions"
    TEST_RESULTS = "test_results"
    TREATMENT = "treatment"
    BILLING = "billing"


@dataclass(frozen=True)
class Routing:
    reason: EscalationReason
    note: str
    """What the staff ticket should say. Descriptive, never clinical."""


REFUSAL_ROUTING: dict[RefusedTopic, Routing] = {
    RefusedTopic.DIAGNOSIS: Routing(
        EscalationReason.COMPLEX_SYMPTOMS,
        "Patient asked what their symptoms mean. Not answered; routed to clinical staff.",
    ),
    RefusedTopic.TRIAGE: Routing(
        EscalationReason.COMPLEX_SYMPTOMS,
        "Patient asked how urgently they need to be seen. Not answered; routed to clinical staff.",
    ),
    RefusedTopic.PRESCRIPTIONS: Routing(
        EscalationReason.PRESCRIPTION_REFILL,
        "Patient asked about a prescription or refill. Routed to the prescribing team.",
    ),
    RefusedTopic.TEST_RESULTS: Routing(
        EscalationReason.TEST_RESULTS,
        "Patient asked about test results. Not interpreted; routed to clinical staff.",
    ),
    RefusedTopic.TREATMENT: Routing(
        EscalationReason.COMPLEX_SYMPTOMS,
        "Patient asked for treatment or medication guidance. Not answered; routed to "
        "clinical staff.",
    ),
    RefusedTopic.BILLING: Routing(
        EscalationReason.BILLING_ISSUE,
        "Patient asked about charges or costs. Routed to billing.",
    ),
}

# Two more routings that are not refusals but must not be improvised either.
ACCESSIBILITY_ROUTING = Routing(
    EscalationReason.ADA_ACCOMMODATION,
    "Patient requested an accessibility accommodation. Routed to staff.",
)
UPSET_ROUTING = Routing(
    EscalationReason.UPSET_PATIENT,
    "Patient asked to speak with a person.",
)


def route(topic: RefusedTopic) -> Routing:
    return REFUSAL_ROUTING[topic]


def all_topics() -> list[RefusedTopic]:
    return list(RefusedTopic)
