"""The knowledge extension end to end — the tool, the screen and the briefing.

The load-bearing tests here are the ones asserting what a patient *cannot* see.
Everything the extension adds either routes an appointment or reaches a nurse;
nothing clinical reaches a patient turn, and these say so mechanically rather
than by reading the wording.
"""

from __future__ import annotations

import json
import re

import pytest

from app.orchestrator import TurnRecorder
from app.policy.decorator import session_scope
from app.policy.gates import PolicyGate
from app.safety.prescreen import Label, Prescreen, keyword_screen
from app.store.session import Session
from app.tools import registry

DOSE = re.compile(r"\d+\s*(?:mg|mcg|ml|g|units|IU)\b|mg/kg|units/kg", re.IGNORECASE)


# ------------------------------------------------------------------ stubs ---


class _StubResponse:
    def __init__(self, text: str) -> None:
        self.content = [type("blk", (), {"type": "text", "text": text})()]


class _StubMessages:
    def __init__(self, text: str) -> None:
        self.text = text

    def create(self, **kwargs):
        return _StubResponse(self.text)


class _StubClient:
    def __init__(self, text: str) -> None:
        self.messages = _StubMessages(text)


class _NeverCalled:
    @property
    def messages(self):
        raise AssertionError("the classifier should not have been reached")


class _BrokenIndex:
    def search(self, *args, **kwargs):
        raise RuntimeError("index gone")


# --------------------------------------------------------------- fixtures ---


@pytest.fixture
def kb():
    from app.knowledge.chunking import chunk_all
    from app.knowledge.corpus import load
    from app.knowledge.embedding import HashingEmbedder
    from app.knowledge.store import InMemoryKnowledgeBase

    store = InMemoryKnowledgeBase(HashingEmbedder())
    store.index(chunk_all(load().records))
    return store


def verified_session() -> Session:
    session = Session()
    session.existence_checked = True
    session.mark_identified("PT-4101")
    session.mark_verified([])
    return session


@pytest.fixture
def running(sim, clinic, kb):
    session = verified_session()
    with (
        session_scope(session, gate=PolicyGate(clinic)),
        registry.backend_scope(sim),
        registry.knowledge_scope(kb),
    ):
        yield session


def call(name: str, **kwargs):
    return json.loads(registry.load()[name].call(kwargs))


# --------------------------------------------------- the tool, via the gate ---


def test_the_tool_requires_verification(sim, clinic, kb):
    """A complaint is health information about the person describing it."""
    with (
        session_scope(Session(), gate=PolicyGate(clinic)),
        registry.backend_scope(sim),
        registry.knowledge_scope(kb),
    ):
        result = call("suggest_appointment_type", complaint="itchy rash between my toes")

    assert result["error"] == "verification_required"


def test_the_tool_returns_a_visit_type_and_nothing_clinical(running):
    result = call("suggest_appointment_type", complaint="itchy scaly rash between my toes")

    assert result["appointment_type"] in {"sick_visit", "follow_up"}
    assert "suggested_within_days" in result

    blob = str(result)
    assert "Athlete" not in blob
    assert "Terbinafine" not in blob
    assert not DOSE.search(blob)


@pytest.mark.parametrize(
    "complaint,must_not_name",
    [
        ("burning when I pee and going constantly", "Cystitis"),
        ("red itchy patches with silvery scales", "Psoriasis"),
        ("throbbing headache with nausea and light sensitivity", "Migraine"),
        ("short of breath and wheezing at night", "Asthma"),
        ("tender swollen joints, stiff in the mornings", "Rheumatoid"),
    ],
)
def test_the_tool_never_names_the_condition_it_matched(running, complaint, must_not_name):
    """Swept across complaints rather than resting on one lucky case."""
    blob = str(call("suggest_appointment_type", complaint=complaint))

    assert must_not_name not in blob
    assert not DOSE.search(blob)


def test_the_tool_says_it_is_an_ai_and_points_at_a_doctor(running):
    result = call("suggest_appointment_type", complaint="itchy rash between my toes")

    assert "AI assistant" in result["disclaimer"]
    assert "doctor" in result["disclaimer"]


def test_a_complaint_matching_nothing_still_routes(running):
    result = call("suggest_appointment_type", complaint="purple spotted zebra syndrome")

    assert result["match_confidence"] == "none"
    assert result["appointment_type"] == "sick_visit"


def test_the_retrieval_is_audited(sim, clinic, kb):
    """A reviewer can see what was retrieved and at what score, even though none
    of it reached the patient."""
    recorder = TurnRecorder()
    with (
        session_scope(verified_session(), gate=PolicyGate(clinic), audit=recorder),
        registry.backend_scope(sim),
        registry.knowledge_scope(kb),
    ):
        call("suggest_appointment_type", complaint="itchy rash between my toes")

    retrieval = [e for e in recorder.events if e.kind == "retrieval"]
    assert retrieval
    assert retrieval[0].detail["tiers"] == ["routing_only"]
    assert retrieval[0].detail["hits"]


# ------------------------------------------------- red-flag screening (R2) ---


def test_retrieval_catches_an_emergency_the_keywords_miss(kb):
    """A stroke described without any word the keyword layer looks for.

    Phrased with vocabulary the corpus shares, because these tests run on the
    deterministic hashing embedder. It scores a paraphrase like "the left side
    of my face has dropped" at 0.17 where the real embedder scores 0.38 — the
    real one ranks Stroke first in both cases, but only one clears a threshold.
    That gap is the measured cost of a hermetic test suite, recorded in
    docs/gaps.md rather than hidden by lowering the floor.
    """
    described = "sudden numbness and weakness on one side with trouble speaking"
    assert keyword_screen(described) is None, "the keyword layer should not catch this"

    screening = Prescreen(client=_NeverCalled(), knowledge=kb).classify(described)

    assert screening.label is Label.EMERGENCY
    assert screening.source == "retrieval"
    assert screening.matched == "Stroke"


def test_an_ordinary_request_is_not_flagged_by_retrieval(kb):
    screening = Prescreen(client=_StubClient("routine"), knowledge=kb).classify(
        "I would like to book a check-up next week"
    )
    assert screening.source == "model"


def test_a_broken_index_degrades_to_the_earlier_two_layer_screen():
    """The extension must never be able to take the safety screen down."""
    screening = Prescreen(client=_StubClient("routine"), knowledge=_BrokenIndex()).classify(
        "I need an appointment"
    )
    assert screening.label is Label.ROUTINE

    emergency = Prescreen(client=_StubClient("routine"), knowledge=_BrokenIndex()).classify(
        "I'm having chest pain"
    )
    assert emergency.label is Label.EMERGENCY
    assert emergency.source == "keyword"


# ------------------------------------------------ clinician briefing (R4) ---


# Worded with vocabulary the corpus shares, for the same reason as above: the
# hashing embedder matches on wording, and this test must not depend on a
# network call to pass.
CLINICAL_NOTE = (
    "Patient reports a persistent cough with phlegm, fever, chills and shortness "
    "of breath with chest pain."
)


def test_a_clinical_escalation_carries_reference_material_for_staff(running, sim):
    call(
        "escalate_to_staff",
        reason="complex_symptoms",
        priority="routine",
        notes=CLINICAL_NOTE,
    )

    ticket = sim.staff.tickets()[0]
    assert "reference material" in ticket.notes
    assert DOSE.search(ticket.notes), "the nurse gets the dosing context"


def test_the_briefing_never_reaches_the_patient_facing_payload(running, sim):
    result = call(
        "escalate_to_staff",
        reason="complex_symptoms",
        priority="routine",
        notes=CLINICAL_NOTE,
    )

    assert not DOSE.search(str(result))
    assert "reference material" not in str(result)


def test_a_billing_escalation_gets_no_clinical_briefing(running, sim):
    call(
        "escalate_to_staff",
        reason="billing_issue",
        priority="routine",
        notes="Patient asked what a visit costs.",
    )

    assert "reference material" not in sim.staff.tickets()[0].notes


def test_escalation_still_works_with_no_knowledge_base(sim, clinic):
    """Losing the briefing must never stop a patient being escalated."""
    with (
        session_scope(Session(), gate=PolicyGate(clinic)),
        registry.backend_scope(sim),
        registry.knowledge_scope(None),
    ):
        result = call("escalate_to_staff", reason="complex_symptoms", priority="routine", notes="x")

    assert result["escalated"] is True
