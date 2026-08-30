"""Phase 4 — the agent loop, driven by recorded scripts (P4-T8).

No API call anywhere in this file. Everything below the model is real: the
registry, the gate, the ledger, the simulator and the audit trail.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.channel import DEFAULT_CHANNEL, Capabilities, TextChannel
from app.main import create_app
from app.orchestrator import (
    MAX_INVALID_CALLS_PER_TURN,
    Orchestrator,
    TurnRecorder,
    _retryable,
)
from app.store.session import Session, SubjectStatus
from app.tools.schemas import AppointmentType, Modality
from tests.replay import Call, ExplodingBackend, Refuse, Say, ScriptedBackend


@pytest.fixture
def orchestrate(sim, clinic):
    def build(script):
        backend = ScriptedBackend(script=script)
        return Orchestrator(sim=sim, backend=backend, clinic=clinic), backend

    return build


# --------------------------------------------------------------- channel ---


def test_the_text_channel_declares_its_capabilities():
    """AD-08 — the channel-conditional rules need something to be conditional on."""
    channel = TextChannel()

    assert channel.capabilities == Capabilities(
        spoken=False, overhearable=False, supports_masking=True
    )
    assert channel.privacy_check_required() is False


def test_the_channel_masks_identifiers():
    """spec §4.2 — a shoulder-surfer reads a screen as easily as a bystander hears."""
    assert DEFAULT_CHANNEL.mask_identifier("phone", "+12065550142") == "(•••) •••-0142"
    assert DEFAULT_CHANNEL.mask_identifier("dob", "1978-03-04") == "••/••/1978"


# ---------------------------------------------------------------- prompt ---


def test_the_system_prompt_is_rendered_from_configuration(orchestrate):
    orchestrator, _ = orchestrate([[Say("hello")]])
    blocks = orchestrator.system_blocks()

    assert len(blocks) == 1
    assert "Riverbend Family Health" in blocks[0]["text"]
    assert "{clinic_name}" not in blocks[0]["text"], "template was not rendered"


def test_the_cache_breakpoint_sits_at_the_end_of_the_frozen_prefix(orchestrate):
    """P4-T4 — anything volatile before this point invalidates the cache."""
    orchestrator, _ = orchestrate([[Say("hello")]])

    assert orchestrator.system_blocks()[0]["cache_control"] == {"type": "ephemeral"}


def test_the_system_prompt_is_byte_stable_across_turns(orchestrate):
    """The silent failure mode: a timestamp here and the cache never warms."""
    orchestrator, _ = orchestrate([[Say("a")], [Say("b")]])
    session = Session()

    orchestrator.run_turn(session, "hello")
    first = orchestrator.system_blocks()[0]["text"]
    orchestrator.run_turn(session, "again")
    second = orchestrator.system_blocks()[0]["text"]

    assert first == second


def test_volatile_context_goes_after_the_transcript(orchestrate):
    orchestrator, backend = orchestrate([[Say("hello")]])
    orchestrator.run_turn(Session(), "what are your hours?")

    messages = backend.seen_messages[0]
    assert messages[-1]["role"] == "system", "the context block must come last"
    assert "Session status:" in messages[-1]["content"]
    assert messages[-2]["role"] == "user"


def test_the_context_block_falls_back_when_the_model_lacks_the_feature(sim, clinic, monkeypatch):
    """Mid-conversation system messages are Opus-5-family only."""
    from app.config import get_settings, reset_config_cache

    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5")
    reset_config_cache()
    backend = ScriptedBackend(script=[[Say("hi")]])
    orchestrator = Orchestrator(sim=sim, backend=backend, clinic=clinic, settings=get_settings())

    orchestrator.run_turn(Session(), "hello")
    messages = backend.seen_messages[0]

    assert messages[-1]["role"] == "user"
    assert "<context>" in messages[-1]["content"]
    reset_config_cache()


def test_the_transcript_grows_and_is_replayed(orchestrate):
    orchestrator, backend = orchestrate([[Say("first")], [Say("second")]])
    session = Session()

    orchestrator.run_turn(session, "hello")
    orchestrator.run_turn(session, "again")

    assert len(session.transcript) == 4
    assert backend.seen_messages[1][0]["content"] == "hello"


# ------------------------------------------------------------------ turn ---


def test_a_clinic_information_turn_needs_no_identity(orchestrate):
    """First of the two exit-test flows."""
    orchestrator, _ = orchestrate(
        [[Call("check_business_hours", {}), Say("We're open until five today.")]]
    )
    result = orchestrator.run_turn(Session(), "are you open now?")

    assert result.tool_calls == ["check_business_hours"]
    assert result.reply == "We're open until five today."
    assert result.stopped_early is None


def test_the_full_booking_flow_runs_through_the_loop(orchestrate, sim, today):
    """Second exit-test flow — design §12, driven by the orchestrator."""
    slot = sim.schedule.search_slots(
        AppointmentType.FOLLOW_UP, today, today + timedelta(days=14), Modality.IN_PERSON
    )[0]

    orchestrator, _ = orchestrate(
        [
            [
                Call(
                    "check_patient_exists",
                    {
                        "first_name": "Amara",
                        "last_name": "Osei",
                        "date_of_birth": "1978-03-04",
                    },
                ),
                Call(
                    "verify_patient_identity",
                    {
                        "patient_id": "PT-4101",
                        "identifier_1_type": "dob",
                        "identifier_1_value": "1978-03-04",
                        "identifier_2_type": "address_zip",
                        "identifier_2_value": "98101",
                    },
                ),
                Call(
                    "search_available_appointments",
                    {
                        "appointment_type": "follow_up",
                        "date_range_start": today.isoformat(),
                        "date_range_end": (today + timedelta(days=14)).isoformat(),
                        "modality": "in_person",
                    },
                ),
                Call(
                    "book_appointment",
                    {
                        "appointment_date": slot.slot_date.isoformat(),
                        "appointment_time": slot.slot_time.isoformat(),
                        "reason_for_visit": "Blood pressure review",
                        "patient_id": "PT-4101",
                        "provider": slot.provider,
                    },
                ),
                Say("You're booked in. Anything else?"),
            ]
        ]
    )

    session = Session()
    result = orchestrator.run_turn(session, "I'd like to book a follow-up next week")

    assert result.tool_calls == [
        "check_patient_exists",
        "verify_patient_identity",
        "search_available_appointments",
        "book_appointment",
    ]
    assert session.status is SubjectStatus.VERIFIED
    assert all(event.detail.get("allowed", True) for event in result.events if event.kind == "gate")


def test_a_denial_is_recorded_and_the_turn_continues(orchestrate):
    """design §11 — a denial is a conversational event, not a crash."""
    orchestrator, _ = orchestrate(
        [
            [
                Call("get_patient_appointments", {"patient_id": "PT-4101"}),
                Say("I'll need to verify you first — what's your date of birth?"),
            ]
        ]
    )
    result = orchestrator.run_turn(Session(), "what appointments do I have?")

    gate_events = [e for e in result.events if e.kind == "gate"]
    assert gate_events[0].detail["allowed"] is False
    assert gate_events[0].detail["code"] == "verification_required"
    assert result.stopped_early is None
    assert "verify" in result.reply.lower()


# --------------------------------------------------------------- tracing ---


def test_every_gate_decision_appears_in_the_trace(orchestrate):
    orchestrator, _ = orchestrate(
        [
            [
                Call("check_business_hours", {}),
                Call("get_clinic_hours", {"date": "2026-09-11"}),
                Say("done"),
            ]
        ]
    )
    result = orchestrator.run_turn(Session(), "hours?")

    kinds = [event.kind for event in result.events]
    assert kinds.count("gate") == 2
    assert kinds.count("result") == 2
    assert kinds[0] == "turn" and kinds[-1] == "reply"


def test_trace_events_serialise_for_the_stream(orchestrate):
    orchestrator, _ = orchestrate([[Call("check_business_hours", {}), Say("ok")]])
    result = orchestrator.run_turn(Session(), "hours?")

    for event in result.events:
        json.dumps(event.as_sse(), default=str)


# ------------------------------------------------------- error handling ---


def test_three_invalid_calls_break_the_turn_and_escalate(orchestrate):
    """P4-T7 — a model that cannot form a valid call will not fix itself."""
    bad = Call("get_clinic_hours", {"date": "not-a-date"})
    orchestrator, _ = orchestrate([[bad, bad, bad, bad, Say("never reached")]])

    result = orchestrator.run_turn(Session(), "when are you open?")

    assert result.stopped_early == "invalid_call_breaker"
    assert "staff" in result.reply.lower()
    assert result.tool_calls.count("get_clinic_hours") == MAX_INVALID_CALLS_PER_TURN


def test_a_refusal_stop_reason_is_handled_explicitly(orchestrate):
    """stop_details is populated only on a refusal — reading content blindly
    would produce an empty reply with no explanation."""
    orchestrator, _ = orchestrate([[Refuse(category="cyber")]])

    result = orchestrator.run_turn(Session(), "something disallowed")

    assert result.stopped_early == "refusal"
    assert "staff" in result.reply.lower()
    assert any(e.kind == "refusal" and e.detail["category"] == "cyber" for e in result.events)


def test_a_model_failure_becomes_an_apology_and_an_offer(sim, clinic):
    orchestrator = Orchestrator(
        sim=sim, backend=ExplodingBackend(RuntimeError("boom")), clinic=clinic
    )
    result = orchestrator.run_turn(Session(), "hello")

    assert result.stopped_early == "model_error"
    assert "call you back" in result.reply
    assert any(e.kind == "error" for e in result.events)


@pytest.mark.parametrize(
    "status,expected", [(429, True), (500, True), (503, True), (400, False), (404, False)]
)
def test_retry_classification(status, expected):
    exc = Exception("x")
    exc.status_code = status
    assert _retryable(exc) is expected


def test_connection_errors_are_retryable():
    class APIConnectionError(Exception):
        pass

    assert _retryable(APIConnectionError()) is True


# ----------------------------------------------------------------- cache ---


def test_the_cache_is_read_on_the_second_turn(orchestrate):
    """The assertion that catches a silent prefix invalidation (P4-T4).

    If this ever reads zero, the system still works — it just quietly costs
    several times more, which is exactly the kind of regression nobody notices.
    """
    orchestrator, _ = orchestrate([[Say("one")], [Say("two")]])
    session = Session()

    first = orchestrator.run_turn(session, "hello")
    second = orchestrator.run_turn(session, "again")

    assert first.cache_read_tokens == 0, "nothing to read on the first turn"
    assert second.cache_read_tokens > 0


# ------------------------------------------------------------- recorder ---


def test_the_recorder_counts_only_invalid_arguments_towards_the_breaker():
    """A verification_required denial is normal; it must not trip the breaker."""
    from app.policy.gates import Verdict
    from app.policy.messages import DenialCode, Remedy

    recorder = TurnRecorder()
    session = Session()
    for _ in range(5):
        recorder.gate_decision(
            "get_patient_appointments",
            Verdict.deny(DenialCode.VERIFICATION_REQUIRED, "rule", Remedy.VERIFY_FIRST),
            session,
        )

    assert recorder.denials == 5
    assert recorder.invalid_calls == 0
    assert recorder.should_break is False


# ------------------------------------------------- the real request shape ---


class _FakeMessages:
    def __init__(self):
        self.kwargs = None

    def tool_runner(self, **kwargs):
        self.kwargs = kwargs
        return iter(())


class _FakeClient:
    def __init__(self):
        self.beta = type("beta", (), {"messages": _FakeMessages()})()


def test_the_first_party_request_is_shaped_correctly():
    """Covers the real request without spending a token.

    Everything here is a documented API constraint rather than a preference:
    budget_tokens is rejected on this model family, effort lives inside
    output_config rather than at the top level, and the fallback beta has to
    accompany the fallbacks parameter.
    """
    from app.config import Settings
    from app.orchestrator import AnthropicBackend

    client = _FakeClient()
    settings = Settings(anthropic_api_key="k", model_provider="anthropic")
    AnthropicBackend(settings=settings, client=client).run(
        system=[{"type": "text", "text": "s"}], messages=[], recorder=TurnRecorder()
    )

    kwargs = client.beta.messages.kwargs
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in json.dumps(kwargs, default=str)
    assert kwargs["output_config"]["effort"] == "medium"
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"
    assert len(kwargs["tools"]) == 15
    assert kwargs["system"][0]["text"] == "s"


def test_the_openrouter_request_translates_the_model_and_drops_fallbacks():
    """OpenRouter speaks the Anthropic Messages API, with two differences.

    The model id is namespaced, and the fallbacks parameter is rejected with a
    400 — so it must be omitted rather than merely ignored.
    """
    from app.config import Settings
    from app.orchestrator import AnthropicBackend

    client = _FakeClient()
    settings = Settings(openrouter_api_key="sk-or-x", model_provider="openrouter")
    AnthropicBackend(settings=settings, client=client).run(
        system=[], messages=[], recorder=TurnRecorder()
    )

    kwargs = client.beta.messages.kwargs
    assert kwargs["model"] == "anthropic/claude-opus-5"
    assert "fallbacks" not in kwargs
    assert "betas" not in kwargs
    # Everything else survives the translation — verified against the live API.
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"]["effort"] == "medium"


def test_openrouter_client_is_pointed_at_the_right_base_url():
    from app.config import OPENROUTER_BASE_URL, Settings

    settings = Settings(openrouter_api_key="sk-or-x", model_provider="openrouter")
    kwargs = settings.client_kwargs()

    # The SDK appends /v1/messages, so the base must stop at /api.
    assert kwargs["base_url"] == OPENROUTER_BASE_URL == "https://openrouter.ai/api"
    assert kwargs["api_key"] == "sk-or-x"


def test_first_party_client_takes_no_overrides():
    from app.config import Settings

    assert Settings(anthropic_api_key="k", model_provider="anthropic").client_kwargs() == {}


def test_haiku_maps_to_a_dotted_slug_on_openrouter():
    """claude-haiku-4-5 first-party, claude-haiku-4.5 on OpenRouter."""
    from app.config import Settings

    settings = Settings(openrouter_api_key="sk-or-x", model_provider="openrouter")
    assert settings.route_model("claude-haiku-4-5") == "anthropic/claude-haiku-4.5"


def test_fallbacks_can_be_turned_off(monkeypatch):
    from app.config import Settings
    from app.orchestrator import AnthropicBackend

    client = _FakeClient()
    settings = Settings(
        server_side_fallbacks=False, anthropic_api_key="k", model_provider="anthropic"
    )
    AnthropicBackend(settings=settings, client=client).run(
        system=[], messages=[], recorder=TurnRecorder()
    )

    assert "fallbacks" not in client.beta.messages.kwargs


def test_an_empty_response_does_not_crash_the_turn():
    """The runner yielding nothing must not raise on `last is None`."""
    from app.orchestrator import AnthropicBackend

    outcome = AnthropicBackend(client=_FakeClient()).run(
        system=[], messages=[], recorder=TurnRecorder()
    )
    assert outcome.stop_reason == "empty"


# --------------------------------------------------------------- the API ---


@pytest.fixture
def client(sim, clinic, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    backend = ScriptedBackend(
        script=[[Call("check_business_hours", {}), Say("We're open until five.")]]
    )
    app = create_app(orchestrator=Orchestrator(sim=sim, backend=backend, clinic=clinic))
    with TestClient(app) as test_client:
        yield test_client


def test_chat_streams_trace_events_then_the_reply(client):
    response = client.post("/chat", json={"message": "are you open?"})

    assert response.status_code == 200
    body = response.text
    assert "event: session" in body
    assert "event: gate" in body
    assert "event: done" in body
    assert "We're open until five." in body


def test_chat_rejects_an_empty_message(client):
    assert client.post("/chat", json={"message": ""}).status_code == 422


def test_an_unknown_session_is_a_404(client):
    assert client.get("/session/s_nope").status_code == 404
