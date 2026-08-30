"""Phase 7 — the endpoints the UI depends on, and output masking (P7-T3, T4, T6)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.channel import TextChannel
from app.main import create_app
from app.orchestrator import Orchestrator
from app.policy.redaction import mask_contact_details
from app.store.session import Session
from tests.replay import Call, Say, ScriptedBackend, ScriptedPrescreen


@pytest.fixture
def app_and_sim(sim, clinic, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    backend = ScriptedBackend(
        script=[
            [
                Call(
                    "check_patient_exists",
                    {"first_name": "Amara", "last_name": "Osei", "date_of_birth": "1978-03-04"},
                ),
                Call("get_patient_appointments", {"patient_id": "PT-4101"}),
                Say("I'll need to verify you first."),
            ]
        ]
    )
    orchestrator = Orchestrator(
        sim=sim, backend=backend, clinic=clinic, prescreen=ScriptedPrescreen()
    )
    app = create_app(orchestrator=orchestrator)
    with TestClient(app) as client:
        yield client, sim


# ------------------------------------------------------------- endpoints ---


def test_the_outbox_is_readable_and_masks_the_number(app_and_sim):
    """P7-T3 — the outbox is a demo surface, not a way around masking."""
    client, sim = app_and_sim
    from app.tools.schemas import MessageType

    sim.messages.send("+12065550142", MessageType.DIRECTIONS)
    body = client.get("/outbox").json()

    assert len(body) == 1
    assert body[0]["message_type"] == "directions"
    assert body[0]["delivery_status"] == "delivered"
    assert body[0]["phone_number"] == "(•••) •••-0142"
    assert "2065550142" not in str(body)


def test_the_outbox_reports_unconfirmed_delivery(app_and_sim):
    client, sim = app_and_sim
    from app.tools.schemas import MessageType

    sim.faults.arm("MessageGateway", "send", "delivery_unconfirmed")
    sim.messages.send("+12065550142", MessageType.TELEHEALTH_LINK)

    assert client.get("/outbox").json()[0]["delivery_status"] == "unconfirmed"


def test_the_outbox_is_newest_first(app_and_sim):
    client, sim = app_and_sim
    from app.tools.schemas import MessageType

    sim.messages.send("+12065550142", MessageType.DIRECTIONS)
    sim.messages.send("+12065550142", MessageType.INTAKE_FORMS)

    body = client.get("/outbox").json()
    assert body[0]["message_type"] == "intake_forms"


def test_the_staff_queue_is_readable(app_and_sim):
    """P7-T4."""
    client, sim = app_and_sim
    from app.tools.schemas import EscalationReason, Priority

    sim.staff.escalate(EscalationReason.BILLING_ISSUE, Priority.ROUTINE, "Asked about a copay.")
    body = client.get("/staff/queue").json()

    assert len(body) == 1
    assert body[0]["reason"] == "billing_issue"
    assert body[0]["priority"] == "routine"
    assert body[0]["notes"]


def test_both_views_start_empty(app_and_sim):
    client, _ = app_and_sim
    assert client.get("/outbox").json() == []
    assert client.get("/staff/queue").json() == []


def test_health_reports_the_model_for_the_sidebar(app_and_sim):
    client, _ = app_and_sim
    body = client.get("/health").json()

    assert "provider" in body
    assert "agent_model" in body


# ------------------------------------------------------------ the trace ---


def test_the_trace_carries_what_the_panel_needs(app_and_sim):
    """P7-T2 — arguments, both levels, the rule and a latency."""
    client, _ = app_and_sim
    body = client.post("/chat", json={"message": "what appointments do I have?"}).text

    assert "event: gate" in body
    assert '"required"' in body and '"actual"' in body
    assert '"latency_ms"' in body
    assert '"rule"' in body
    assert '"args"' in body


def test_the_trace_shows_a_denial_then_the_session_status(app_and_sim):
    """The reviewable moment: refused before verification."""
    client, _ = app_and_sim
    body = client.post("/chat", json={"message": "what appointments do I have?"}).text

    assert "verification_required" in body
    assert '"allowed": false' in body


def test_trace_arguments_are_redacted(app_and_sim):
    """The panel renders the same view the audit log stores."""
    client, _ = app_and_sim
    body = client.post("/chat", json={"message": "I'm Amara Osei"}).text

    assert "1978-03-04" not in body
    assert "<dob>" in body or "<name>" in body


# ------------------------------------- spec §4.10 patient-confirmed number ---


def test_a_number_the_patient_states_can_receive_directions(sim, clinic):
    """Regression: the assistant used to ask for the number forever.

    Specification §4.10 lets directions go to a number the patient confirms as
    their own. Their saying it is the confirmation — but confirmed_phone was
    only ever set by verification, so an unverified patient could state their
    number, the gate would still refuse, and the assistant would ask again. The
    eval caught it looping four turns without ever sending.
    """
    from app.orchestrator import Orchestrator

    orchestrator = Orchestrator(
        sim=sim,
        clinic=clinic,
        prescreen=ScriptedPrescreen(),
        backend=ScriptedBackend(
            script=[
                [Say("What number should I send that to?")],
                [
                    Call(
                        "send_secure_text",
                        {"phone_number": "206-555-0142", "message_type": "directions"},
                    ),
                    Say("Sent."),
                ],
            ]
        ),
    )
    session = Session()

    orchestrator.run_turn(session, "Please text me directions to the main clinic.")
    result = orchestrator.run_turn(session, "My number is 206-555-0142.")

    assert "+12065550142" in session.patient_asserted_phones
    gate = [e for e in result.events if e.kind == "gate"][0]
    assert gate.detail["allowed"] is True, "the patient stating it is the confirmation"


def test_a_stated_number_does_not_unlock_anything_carrying_health_detail(sim, clinic):
    """Only directions. A telehealth link still needs the number on the record."""
    from app.orchestrator import Orchestrator

    orchestrator = Orchestrator(
        sim=sim,
        clinic=clinic,
        prescreen=ScriptedPrescreen(),
        backend=ScriptedBackend(
            script=[
                [
                    Call(
                        "send_secure_text",
                        {"phone_number": "206-555-0142", "message_type": "telehealth_link"},
                    ),
                    Say("…"),
                ]
            ]
        ),
    )
    session = Session()
    result = orchestrator.run_turn(session, "Text my telehealth link to 206-555-0142.")

    gate = [e for e in result.events if e.kind == "gate"][0]
    assert gate.detail["allowed"] is False
    assert gate.detail["code"] == "verification_required"


def test_a_number_the_patient_never_mentioned_is_still_refused(sim, clinic):
    """The exemption is for a number they stated, not any number at all."""
    from app.orchestrator import Orchestrator

    orchestrator = Orchestrator(
        sim=sim,
        clinic=clinic,
        prescreen=ScriptedPrescreen(),
        backend=ScriptedBackend(
            script=[
                [
                    Call(
                        "send_secure_text",
                        {"phone_number": "206-555-0188", "message_type": "directions"},
                    ),
                    Say("…"),
                ]
            ]
        ),
    )
    session = Session()
    result = orchestrator.run_turn(session, "Text directions to 206-555-0142.")

    gate = [e for e in result.events if e.kind == "gate"][0]
    assert gate.detail["allowed"] is False


# --------------------------------------------------------- P7-T6 masking ---


@pytest.mark.parametrize(
    "text,expected",
    [
        ("I have your number as 206-555-0142.", "(•••) •••-0142"),
        ("Sent to (206) 555-0142.", "(•••) •••-0142"),
        ("Texting +1 206 555 0142 now.", "(•••) •••-0142"),
        ("I'll email amara.osei@example.invalid", "a•••@example.invalid"),
    ],
)
def test_contact_details_are_masked_in_replies(text, expected):
    assert expected in mask_contact_details(text)


@pytest.mark.parametrize(
    "text",
    [
        "Confirmed for September 13, 2026 at 9:30 AM.",
        "Your appointment reference is AP-77301.",
        "That slot is SL-2026-09-14-0-1.",
        "We're open 08:00 to 17:00.",
    ],
)
def test_masking_leaves_legitimate_content_alone(text):
    """A redactor may over-fire; a masker may not.

    Masking an appointment date or an appointment id would make the assistant
    unable to confirm a booking, which is worse than the risk it removes.
    """
    assert mask_contact_details(text) == text


def test_the_channel_masks_what_the_patient_sees():
    """Defence in depth: the prompt tells the model to mask, this holds if it forgets."""
    rendered = TextChannel().render("Your number on file is 206-555-0142.")

    assert "206-555-0142" not in rendered
    assert "(•••) •••-0142" in rendered


def test_a_reply_that_is_already_masked_is_untouched():
    already = "I have your number as (•••) •••-0142."
    assert TextChannel().render(already) == already


def test_masking_runs_on_every_turn(app_and_sim):
    client, _ = app_and_sim
    session = Session()
    assert session.session_id  # the channel is bound per turn, not per session
    body = client.post("/chat", json={"message": "hello"}).text
    assert "2065550142" not in body
