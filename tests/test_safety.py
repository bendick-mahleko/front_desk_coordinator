"""P5-T6 — the safety subsystem.

Phase 5's exit test: an emergency turn returns safety copy, queues an emergency
ticket, and calls no scheduling function at all.
"""

from __future__ import annotations

import pytest

from app.orchestrator import Orchestrator
from app.safety import refusals
from app.safety.prescreen import (
    EMERGENCY_PATTERNS,
    Label,
    Prescreen,
    _parse_label,
    keyword_screen,
)
from app.store.session import Session
from app.tools.schemas import EscalationReason, Priority
from tests.replay import Call, Say, ScriptedBackend, ScriptedPrescreen

SCHEDULING_FUNCTIONS = {
    "search_available_appointments",
    "book_appointment",
    "cancel_appointment",
    "reschedule_appointment",
    "get_patient_appointments",
    "get_patient_demographics",
}


@pytest.fixture
def orchestrate(sim, clinic):
    def build(script=None, label=Label.ROUTINE, prescreen=None):
        backend = ScriptedBackend(script=script or [[Say("ok")]])
        return Orchestrator(
            sim=sim,
            backend=backend,
            clinic=clinic,
            prescreen=prescreen or ScriptedPrescreen(label=label),
        ), backend

    return build


# ----------------------------------------------------- the keyword layer ---


@pytest.mark.parametrize(
    "text",
    [
        "I'm having chest pain right now",
        "I can't breathe properly",
        "my husband is having a stroke",
        "I think I'm having a heart attack",
        "she's unconscious and won't wake up",
        "my face is drooping on one side",
        "I took too many pills, I think I overdosed",
        "I want to kill myself",
        "his throat is closing up",
        "the baby is not breathing",
    ],
)
def test_unambiguous_emergencies_are_caught_without_a_model(text):
    """The deterministic layer, which works when the classifier does not."""
    screening = keyword_screen(text)

    assert screening is not None
    assert screening.label is Label.EMERGENCY
    assert screening.source == "keyword"


@pytest.mark.parametrize(
    "text",
    [
        "I'd like a follow-up for the chest pain I had last year",
        "I have a history of chest pain, booking a check-up",
        "my asthma is under control, I just need a repeat appointment",
        "I don't have chest pain, I just want a wellness visit",
        "follow-up about the breathing trouble I had months ago",
    ],
)
def test_past_and_managed_conditions_do_not_trigger_the_fast_path(text):
    """Without this, anyone with a cardiac history cannot book an appointment."""
    assert keyword_screen(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "I need to book a follow-up",
        "what are your opening hours?",
        "can you text me directions",
        "I've got a sore throat and want an appointment this week",
    ],
)
def test_ordinary_requests_pass_the_fast_path(text):
    assert keyword_screen(text) is None


def test_every_pattern_is_reachable():
    """A pattern that matches nothing is a rule nobody is enforcing."""
    assert len(EMERGENCY_PATTERNS) >= 15


# ------------------------------------------------------- the classifier ---


class _StubResponse:
    def __init__(self, text):
        self.content = [type("blk", (), {"type": "text", "text": text})()]


class _StubMessages:
    def __init__(self, text):
        self.text = text
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _StubResponse(self.text)


class _StubClient:
    def __init__(self, text):
        self.messages = _StubMessages(text)


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("routine", Label.ROUTINE),
        ("emergency", Label.EMERGENCY),
        ("clinical_advice", Label.CLINICAL_ADVICE),
        ("staff_request", Label.STAFF_REQUEST),
        ("  ROUTINE\n", Label.ROUTINE),
        ("clinical_advice.", Label.CLINICAL_ADVICE),
    ],
)
def test_the_classifier_reply_maps_to_a_label(reply, expected):
    screening = Prescreen(client=_StubClient(reply)).classify("something ambiguous")

    assert screening.label is expected
    assert screening.source == "model"


def test_a_chatty_classifier_reply_still_resolves():
    """A model that answers in a sentence must not silently become routine."""
    assert _parse_label("This looks like an emergency to me") is Label.EMERGENCY


def test_an_unrecognisable_reply_falls_back_to_routine():
    assert _parse_label("¯\\_(ツ)_/¯") is Label.ROUTINE


def test_the_classifier_uses_the_configured_model():
    client = _StubClient("routine")
    Prescreen(client=client).classify("book me in")

    assert "haiku" in client.messages.kwargs["model"]
    assert client.messages.kwargs["max_tokens"] <= 16, "one word needs no more"


def test_a_classifier_outage_does_not_block_the_turn():
    """Falls back to routine — safe *because* the keyword layer is independent."""

    class Broken:
        @property
        def messages(self):
            raise RuntimeError("upstream down")

    screening = Prescreen(client=Broken()).classify("I need an appointment")

    assert screening.label is Label.ROUTINE
    assert screening.source == "fallback"


def test_an_outage_still_catches_keyword_emergencies():
    """The point of the two layers: the deterministic one survives an outage."""

    class Broken:
        @property
        def messages(self):
            raise RuntimeError("upstream down")

    screening = Prescreen(client=Broken()).classify("I'm having chest pain")

    assert screening.is_emergency
    assert screening.source == "keyword"


def test_the_keyword_layer_short_circuits_before_any_model_call():
    client = _StubClient("routine")
    screening = Prescreen(client=client).classify("I can't breathe")

    assert screening.is_emergency
    assert client.messages.kwargs is None, "no model call should have been made"


# --------------------------------------------- the emergency short-circuit ---


def test_an_emergency_turn_never_enters_the_agent_loop(orchestrate, sim):
    """Phase 5 exit test."""
    orchestrator, backend = orchestrate(
        script=[[Call("search_available_appointments", {}), Say("booked")]]
    )
    session = Session()

    result = orchestrator.run_turn(session, "I'm having chest pain and need an appointment")

    assert backend.turns_run == 0, "the agent loop must not run"
    assert result.stopped_early == "emergency"
    assert "911" in result.reply
    assert "emergency department" in result.reply


def test_an_emergency_turn_calls_no_scheduling_function(orchestrate, sim):
    orchestrator, _ = orchestrate()
    result = orchestrator.run_turn(Session(), "my husband is unconscious")

    assert not SCHEDULING_FUNCTIONS.intersection(result.tool_calls)
    assert result.tool_calls == ["escalate_to_staff"]


def test_an_emergency_queues_a_ticket_at_emergency_priority(orchestrate, sim):
    orchestrator, _ = orchestrate()
    orchestrator.run_turn(Session(), "I think I'm having a heart attack")

    tickets = sim.staff.tickets()
    assert len(tickets) == 1
    assert tickets[0].priority is Priority.EMERGENCY
    assert tickets[0].reason is EscalationReason.COMPLEX_SYMPTOMS


def test_the_emergency_escalation_goes_through_the_gate(orchestrate, sim):
    """An emergency is the last place to want an untraced path."""
    orchestrator, _ = orchestrate()
    result = orchestrator.run_turn(Session(), "I can't breathe")

    gate_events = [e for e in result.events if e.kind == "gate"]
    assert len(gate_events) == 1
    assert gate_events[0].detail["function"] == "escalate_to_staff"
    assert gate_events[0].detail["allowed"] is True


def test_the_emergency_number_comes_from_configuration(sim, clinic):
    """911 is wrong outside the US, so it is data rather than a constant."""
    uk = clinic.model_copy(
        update={"policy": clinic.policy.model_copy(update={"emergency_number": "999"})}
    )
    orchestrator = Orchestrator(
        sim=sim,
        backend=ScriptedBackend(script=[[Say("x")]]),
        clinic=uk,
        prescreen=ScriptedPrescreen(),
    )

    result = orchestrator.run_turn(Session(), "I'm having chest pain")
    assert "999" in result.reply
    assert "911" not in result.reply


def test_the_prescreen_result_appears_in_the_trace(orchestrate):
    orchestrator, _ = orchestrate()
    result = orchestrator.run_turn(Session(), "I want to kill myself")

    events = [e for e in result.events if e.kind == "prescreen"]
    assert len(events) == 1
    assert events[0].detail["label"] == "emergency"
    assert events[0].detail["source"] == "keyword"


def test_an_emergency_still_records_a_known_patient_id(orchestrate, sim):
    orchestrator, _ = orchestrate()
    session = Session()
    session.mark_identified("PT-4101")

    orchestrator.run_turn(session, "I'm having chest pain")

    assert sim.staff.tickets()[0].patient_id == "PT-4101"


# --------------------------------------------------------- reinforcement ---


def test_a_clinical_advice_turn_still_runs_but_is_reinforced(orchestrate):
    orchestrator, backend = orchestrate(label=Label.CLINICAL_ADVICE)
    orchestrator.run_turn(Session(), "should I be worried about this rash?")

    assert backend.turns_run == 1, "advice is refused by the model, not short-circuited"
    context = backend.seen_messages[0][-1]["content"]
    assert "Do not diagnose" in context


def test_a_staff_request_turn_is_reinforced(orchestrate):
    orchestrator, backend = orchestrate(label=Label.STAFF_REQUEST)
    orchestrator.run_turn(Session(), "can I speak to a person please")

    assert "escalate_to_staff" in backend.seen_messages[0][-1]["content"]


def test_a_routine_turn_carries_no_reinforcement(orchestrate):
    orchestrator, backend = orchestrate(label=Label.ROUTINE)
    orchestrator.run_turn(Session(), "what are your hours")

    context = backend.seen_messages[0][-1]["content"]
    assert "Do not diagnose" not in context
    assert "escalate_to_staff" not in context


# ------------------------------------------------------- the refusal set ---


def test_all_six_refused_topics_route_to_an_escalation_reason():
    """spec §1 and §7 — a refusal is a handover, not a dead end."""
    assert len(refusals.all_topics()) == 6

    for topic in refusals.all_topics():
        routing = refusals.route(topic)
        assert isinstance(routing.reason, EscalationReason)
        assert routing.note


@pytest.mark.parametrize(
    "topic,reason",
    [
        (refusals.RefusedTopic.DIAGNOSIS, EscalationReason.COMPLEX_SYMPTOMS),
        (refusals.RefusedTopic.TRIAGE, EscalationReason.COMPLEX_SYMPTOMS),
        (refusals.RefusedTopic.PRESCRIPTIONS, EscalationReason.PRESCRIPTION_REFILL),
        (refusals.RefusedTopic.TEST_RESULTS, EscalationReason.TEST_RESULTS),
        (refusals.RefusedTopic.TREATMENT, EscalationReason.COMPLEX_SYMPTOMS),
        (refusals.RefusedTopic.BILLING, EscalationReason.BILLING_ISSUE),
    ],
)
def test_each_topic_routes_where_the_specification_says(topic, reason):
    assert refusals.route(topic).reason is reason


def test_refusal_notes_are_descriptive_not_clinical():
    """A staff note records what was asked, never an assessment of it."""
    for topic in refusals.all_topics():
        note = refusals.route(topic).note.lower()
        assert "patient" in note
        for clinical in ["diagnos", "likely", "probably", "appears to be", "suggests"]:
            assert clinical not in note, f"{topic}: {note}"


def test_accessibility_and_upset_have_their_own_routings():
    assert refusals.ACCESSIBILITY_ROUTING.reason is EscalationReason.ADA_ACCOMMODATION
    assert refusals.UPSET_ROUTING.reason is EscalationReason.UPSET_PATIENT


# ------------------------------------------------------- the system prompt ---


def test_the_prompt_carries_the_symptom_minimisation_rule(orchestrate):
    """P5-T5 — spec §7."""
    orchestrator, _ = orchestrate()
    prompt = orchestrator.system_blocks()[0]["text"]

    assert "reason for visit" in prompt.lower()
    assert "do not ask follow-up questions about symptoms" in prompt.lower()


def test_the_prompt_names_every_refused_topic(orchestrate):
    orchestrator, _ = orchestrate()
    prompt = orchestrator.system_blocks()[0]["text"].lower()

    for phrase in ["diagnosis", "triage", "refill", "test", "billing", "medication"]:
        assert phrase in prompt


def test_the_prompt_uses_the_configured_emergency_number(sim, clinic):
    uk = clinic.model_copy(
        update={"policy": clinic.policy.model_copy(update={"emergency_number": "999"})}
    )
    orchestrator = Orchestrator(
        sim=sim,
        backend=ScriptedBackend(script=[[Say("x")]]),
        clinic=uk,
        prescreen=ScriptedPrescreen(),
    )
    assert "999" in orchestrator.system_blocks()[0]["text"]
