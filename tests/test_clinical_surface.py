"""C7 — the clinical surface: endpoint, prompt, and the §7.1 scoping.

The load-bearing test is that no request body can elevate a session. Everything
else in r3 rests on the role being bound outside the conversation, and an HTTP
API is where "outside the conversation" gets tested for real.

C7 also pays C2's debt — the clinical system prompt — and fixes something that
only became visible once a clinical turn could run: §7.1's emergency prescreen
would have interrupted a clinician describing a stroke and told them to call an
ambulance.

No model, no network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.orchestrator import PROMPT_BY_ROLE, Orchestrator
from app.safety.prescreen import Label, Screening
from app.store.session import Role, Session
from tests.replay import Say, ScriptedBackend, ScriptedPrescreen


class AlwaysEmergency:
    """A prescreen that flags everything, so the §7.1 scoping is visible."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def classify(self, text: str) -> Screening:
        self.calls.append(text)
        return Screening(Label.EMERGENCY, source="keyword", matched="chest pain")


@pytest.fixture
def orchestrator(sim, clinic):
    return Orchestrator(
        sim=sim,
        clinic=clinic,
        prescreen=ScriptedPrescreen(),
        backend=ScriptedBackend(script=[[Say("noted")]] * 4),
        knowledge=None,
    )


@pytest.fixture
def client(orchestrator):
    app = create_app(orchestrator=orchestrator)
    with TestClient(app) as test_client:
        yield test_client


# ------------------------------------------------ no body may elevate a role ---


def test_chat_takes_no_role_and_no_channel(client):
    """The claim r3 rests on. §3.2 makes channel eligibility configuration, *"not
    a runtime decision"* — and anything that can be named in a request body is a
    runtime decision by definition."""
    schema = client.get("/openapi.json").json()
    body = schema["components"]["schemas"]["ChatRequest"]["properties"]

    assert set(body) == {"message", "session_id"}


@pytest.mark.parametrize(
    "extra",
    [
        {"role": "clinical_assistant"},
        {"channel": "clinical"},
        {"effective_role": "clinical_assistant"},
        {"staff_id": "STAFF-2001"},
    ],
    ids=["role", "channel", "effective_role", "staff_id"],
)
def test_an_extra_field_on_chat_is_rejected(client, extra):
    """Not ignored — rejected. A silently dropped field is one somebody will
    believe worked."""
    response = client.post("/chat", json={"message": "hello", **extra})

    assert response.status_code == 422


def test_a_chat_session_is_always_a_patient_session(client):
    response = client.post("/chat", json={"message": "are you open?"})
    sessions = list(client.app.state.sessions.values())

    assert response.status_code == 200
    assert sessions
    assert all(session.role is Role.PATIENT for session in sessions)


# ------------------------------------------------- the clinical endpoint ---


def test_a_clinical_session_can_be_established(client):
    response = client.post("/clinical/session")

    assert response.status_code == 200
    assert response.json()["session_id"].startswith("s_")


def test_the_established_session_is_clinical_on_the_clinical_channel(client):
    session_id = client.post("/clinical/session").json()["session_id"]

    session = client.app.state.sessions[session_id]
    assert session.role is Role.CLINICAL_ASSISTANT
    assert session.channel == "clinical"


def test_establishing_is_not_authenticating(client):
    """spec §4.13 — the session starts with no capabilities. Its *effective*
    role is SYSTEM until a credential is checked, so it can call nothing but
    authenticate_clinical_user."""
    session_id = client.post("/clinical/session").json()["session_id"]

    session = client.app.state.sessions[session_id]
    assert session.effective_role is Role.SYSTEM
    assert not session.clinical_authentication_valid
    assert session.staff_id is None


def test_the_endpoint_is_absent_when_the_clinic_has_not_enabled_the_role(
    orchestrator, clinic, monkeypatch
):
    """A clinic that has not turned the role on should not have a door to it."""
    off = clinic.model_copy(deep=True)
    off.clinical.enabled = False
    monkeypatch.setattr("app.main.get_clinic_config", lambda: off)

    with TestClient(create_app(orchestrator=orchestrator)) as client:
        assert client.post("/clinical/session").status_code == 404


def test_a_clinical_session_survives_a_reload(client):
    """It goes through the same store as a patient session, and the Session
    validator refuses a clinical role on a patient channel on *every*
    construction path — including rehydration."""
    session_id = client.post("/clinical/session").json()["session_id"]
    del client.app.state.sessions[session_id]

    summary = client.get(f"/session/{session_id}")

    assert summary.status_code == 200
    assert client.app.state.sessions[session_id].role is Role.CLINICAL_ASSISTANT


# --------------------------------------------------------- the config block ---


def test_config_reports_the_clinical_settings(client):
    clinical = client.get("/config").json()["clinical"]

    assert clinical["enabled"] is True
    assert clinical["session_minutes"] == 30
    assert clinical["channels"] == ["clinical"]
    assert "physician" in clinical["permitted_roles"]
    assert clinical["directory"] == "SimulatedIdentityProvider"


def test_config_never_reports_credential_material(client):
    """The settings panel is the one screen whose whole purpose is displaying
    configuration, which makes it the likeliest place for a token to appear."""
    import json as json_module

    blob = json_module.dumps(client.get("/config").json()).lower()

    assert "fixture-token" not in blob
    assert "credential_token" not in blob
    assert "token" not in blob.replace("input_tokens", "").replace("output_tokens", "")


# --------------------------------------------------------------- the prompt ---


def test_each_conversational_role_has_its_own_prompt():
    assert PROMPT_BY_ROLE[Role.PATIENT] == "system.md"
    assert PROMPT_BY_ROLE[Role.CLINICAL_ASSISTANT] == "clinical.md"
    assert Role.SYSTEM not in PROMPT_BY_ROLE


def test_the_clinical_prompt_says_what_section_7_2_requires(orchestrator):
    """§7.2's bullets are the contract for this prompt, so they are asserted
    rather than trusted to have been remembered while writing it."""
    text = orchestrator.system_blocks(Role.CLINICAL_ASSISTANT)[0]["text"].lower()

    for requirement in (
        "clinical information assistant",  # A.0 framing
        "cite",  # every statement grounded and cited
        "abstain rather than approximate",  # §7.2
        "fixed indexed set",  # corpus limits
        "do not calculate a dose",  # §4.16
        "prescription",  # no orders
        "do not assign urgency",  # §4.15
        "test results",  # no interpretation
        "data, never instructions",  # §7.2 injection rule
        "roles are not mixed",  # §7.3
    ):
        assert requirement in text, requirement


def test_the_prompt_forbids_handing_the_clinician_an_instruction(orchestrator):
    """Found live. Told "that calculation is the clinician's", the model
    faithfully turned it into *"Calculate the dose for this patient's weight
    before use"* — an imperative, which §7.2 says nothing this role produces may
    be. The prompt now says to state what the figure is rather than what to do
    with it."""
    text = orchestrator.system_blocks(Role.CLINICAL_ASSISTANT)[0]["text"]

    assert "do not tell the clinician to, either" in text
    assert "is an instruction" in text


def test_the_clinical_prompt_does_not_impersonate_the_front_desk(orchestrator):
    """The failure C2 refused to ship: a clinician reading receptionist rules."""
    text = orchestrator.system_blocks(Role.CLINICAL_ASSISTANT)[0]["text"].lower()

    assert "receptionist" not in text
    assert "book you" not in text


def test_the_patient_prompt_is_untouched_by_any_of_this(orchestrator):
    text = orchestrator.system_blocks(Role.PATIENT)[0]["text"].lower()

    assert "receptionist" in text
    assert "clinical information assistant" not in text
    assert "dosage" not in text


def test_both_prompts_render_the_clinic_configuration(orchestrator, clinic):
    """Rendered from configuration, not hardcoded, so the same prompt serves a
    second clinic."""
    for role in (Role.PATIENT, Role.CLINICAL_ASSISTANT):
        text = orchestrator.system_blocks(role)[0]["text"]
        assert clinic.name in text
        assert clinic.timezone in text


# ------------------------------------------- §7.1 is patient-facing only ---


def test_the_emergency_prescreen_does_not_run_in_a_clinical_session(sim, clinic):
    """The defect C7 exposed. §7.1 is titled *Patient-facing sessions*, and
    every bullet is about somebody describing their own symptoms to a bot.

    Running it for a clinician would be actively wrong rather than merely
    wasteful: a nurse describing a stroke presentation — the clinical role's
    entire purpose — would be interrupted and told to call an ambulance for
    themselves.
    """
    screen = AlwaysEmergency()
    orchestrator = Orchestrator(
        sim=sim,
        clinic=clinic,
        prescreen=screen,
        backend=ScriptedBackend(script=[[Say("considerations follow")]]),
        knowledge=None,
    )
    session = Session(role=Role.CLINICAL_ASSISTANT, channel="clinical")

    result = orchestrator.run_turn(session, "sudden facial droop and slurred speech")

    assert screen.calls == []
    assert "911" not in result.reply
    assert result.reply == "considerations follow"


def test_the_emergency_prescreen_still_runs_for_a_patient(sim, clinic):
    """The other direction, and the one that must never regress: r1's emergency
    path is untouched."""
    screen = AlwaysEmergency()
    orchestrator = Orchestrator(
        sim=sim,
        clinic=clinic,
        prescreen=screen,
        backend=ScriptedBackend(script=[[Say("should not be reached")]]),
        knowledge=None,
    )

    result = orchestrator.run_turn(Session(), "I have crushing chest pain")

    assert screen.calls
    assert clinic.policy.emergency_number in result.reply


def test_the_skip_is_recorded_rather_than_silent(sim, clinic):
    """An audit log with no prescreen record would look like an omission. One
    saying "skipped, and here is the clause" is a decision."""
    orchestrator = Orchestrator(
        sim=sim,
        clinic=clinic,
        prescreen=AlwaysEmergency(),
        backend=ScriptedBackend(script=[[Say("ok")]]),
        knowledge=None,
    )
    session = Session(role=Role.CLINICAL_ASSISTANT, channel="clinical")

    result = orchestrator.run_turn(session, "productive cough and fever")

    prescreen_events = [event for event in result.events if event.kind == "prescreen"]
    assert len(prescreen_events) == 1
    detail = prescreen_events[0].detail
    assert detail["source"] == "skipped"
    assert "§7.1" in detail["reason"]


def test_a_clinical_turn_reaches_the_model_with_the_clinical_tools(sim, clinic):
    """The whole C7 wiring in one assertion: role bound at the endpoint, prompt
    chosen by role, tool schema chosen by role, all the way to the request."""
    backend = ScriptedBackend(script=[[Say("noted")]])
    orchestrator = Orchestrator(
        sim=sim, clinic=clinic, prescreen=ScriptedPrescreen(), backend=backend, knowledge=None
    )
    session = Session(role=Role.CLINICAL_ASSISTANT, channel="clinical")

    orchestrator.run_turn(session, "what does the corpus say about pneumonia")

    assert backend.seen_roles == [Role.CLINICAL_ASSISTANT]
    assert "clinical information assistant" in backend.seen_system[0][0]["text"].lower()


# ---------------------------------------------------------------- the UI ---


def test_the_clinical_ui_is_a_separate_app():
    """Not a tab beside the patient chat. §3.2 draws a channel boundary, and a
    demo that draws it in the same browser window is demonstrating the
    opposite."""
    from pathlib import Path

    ui = Path(__file__).resolve().parent.parent / "ui"

    assert (ui / "clinical.py").exists()
    assert "clinical" not in (ui / "app.py").read_text(encoding="utf-8").lower().split("def ")[
        0
    ].replace("clinical review", "")


def test_the_clinical_ui_renders_without_a_service(monkeypatch):
    """It has to fail visibly rather than tracebacking when the API is down."""
    import sys
    import types
    from unittest.mock import MagicMock

    fake = types.ModuleType("streamlit")
    for name in (
        "set_page_config",
        "markdown",
        "caption",
        "title",
        "divider",
        "error",
        "warning",
        "info",
        "success",
        "code",
        "stop",
        "button",
        "chat_input",
        "chat_message",
        "spinner",
        "rerun",
        "sidebar",
        "columns",
        "metric",
    ):
        setattr(fake, name, MagicMock())
    fake.session_state = {}
    fake.stop = MagicMock(side_effect=SystemExit)
    monkeypatch.setitem(sys.modules, "streamlit", fake)
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")

    with pytest.raises(SystemExit):
        import importlib

        import ui.clinical

        importlib.reload(ui.clinical)


def test_the_settings_panel_reports_the_clinical_block(client, monkeypatch):
    import sys
    import types
    from unittest.mock import MagicMock

    fake = types.ModuleType("streamlit")
    for name in ("markdown", "caption", "divider", "success", "warning", "metric"):
        setattr(fake, name, MagicMock())
    fake.columns = MagicMock(
        side_effect=lambda spec: tuple(
            MagicMock() for _ in (spec if isinstance(spec, list) else range(spec))
        )
    )
    monkeypatch.setitem(sys.modules, "streamlit", fake)

    import importlib

    from ui import settings

    importlib.reload(settings)
    settings.render(client.get("/config").json())

    rendered = " ".join(str(call) for call in fake.caption.call_args_list)
    assert "not configurable" in rendered or "never eligible" in rendered


def test_the_settings_panel_survives_a_config_without_the_block(monkeypatch):
    """An older service, or one with the role off.

    ``monkeypatch.setitem`` rather than assigning ``sys.modules`` directly: this
    test used to leak the fake module for the rest of the session, so anything
    later that wanted the *real* Streamlit — the icon validator two tests down —
    got ``'streamlit' is not a package`` instead. A mocked module that outlives
    its test makes every later test in the process unreliable, and the failure
    surfaces somewhere it was not caused.
    """
    import sys
    import types
    from unittest.mock import MagicMock

    fake = types.ModuleType("streamlit")
    for name in ("markdown", "caption", "divider", "success", "warning", "metric"):
        setattr(fake, name, MagicMock())
    fake.columns = MagicMock(
        side_effect=lambda spec: tuple(
            MagicMock() for _ in (spec if isinstance(spec, list) else range(spec))
        )
    )
    monkeypatch.setitem(sys.modules, "streamlit", fake)

    import importlib

    from ui import settings

    importlib.reload(settings)
    settings.render({"service": {}, "language_model": {}, "knowledge_base": {}})
    # Leave the module as the rest of the suite expects to find it.
    monkeypatch.undo()
    importlib.reload(settings)


# ------------------------------------------------------------ doc drift ---


def test_every_ui_entry_point_is_in_the_demo_script():
    """A guard for the failure that prompted it.

    C7 built the clinical surface and documented it in the runbook. The demo
    script — which is what somebody actually follows — was never updated, so a
    reader ran the two commands it lists, found no way to ask a clinical
    question, and reasonably concluded the feature was not there.

    The surface working and the surface being findable are different properties,
    and only one of them had a test.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    demo = (root / "docs" / "demo.md").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    entry_points = sorted(
        path.name
        for path in (root / "ui").glob("*.py")
        if "streamlit" in path.read_text(encoding="utf-8")
        and path.name != "__init__.py"
        and "st.set_page_config" in path.read_text(encoding="utf-8")
    )

    assert entry_points, "no Streamlit entry points found — has the UI moved?"
    for name in entry_points:
        assert f"ui/{name}" in demo, f"docs/demo.md never tells anyone to run ui/{name}"
        assert f"ui/{name}" in readme, f"README.md never tells anyone to run ui/{name}"


# ----------------------------------------------- Streamlit's own API rules ---


def test_every_icon_argument_is_an_emoji_streamlit_accepts():
    """A crash I shipped, as a test.

    Removing emoji from the verdicts was right; passing the replacement marks to
    `icon=` was not. Streamlit validates that argument as a genuine emoji and
    raises `StreamlitAPIException` on anything else, so `icon="○"` took the page
    down the moment the sidebar rendered with a session.

    Nothing caught it. The UI tests mock Streamlit with MagicMock, which accepts
    any argument by construction, so they cannot see API misuse — and the smoke
    check only proved the server answered on `/`, which does not exercise a
    sidebar that has a session in it.

    So this validates against Streamlit's own function rather than a regex of my
    own: the rule cannot drift from the library's, and a version that widens or
    narrows what it accepts moves this test with it.
    """
    import re
    from pathlib import Path

    from streamlit.string_util import validate_emoji

    ui = Path(__file__).resolve().parent.parent / "ui"
    offenders: list[str] = []

    for path in sorted(ui.glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in re.finditer(r'\bicon="([^"]*)"', line):
                try:
                    validate_emoji(match.group(1))
                except Exception:  # noqa: BLE001 - any rejection is a failure
                    offenders.append(f"{path.name}:{number}: icon={match.group(1)!r}")

    assert offenders == [], "Streamlit will raise on these icons:\n" + "\n".join(offenders)


def test_page_icons_are_valid_too():
    """`page_icon` takes the same validation path."""
    import re
    from pathlib import Path

    from streamlit.string_util import validate_emoji

    ui = Path(__file__).resolve().parent.parent / "ui"
    for path in sorted(ui.glob("*.py")):
        for match in re.finditer(r'page_icon="([^"]*)"', path.read_text(encoding="utf-8")):
            validate_emoji(match.group(1))
