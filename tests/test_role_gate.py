"""C2 — the role axis at the gate, and the per-role tool schema (spec r3 §2, §4.13).

r1 tested the §3 authorization table cell by cell, in both directions. r3 turns
that table into a cube, and this file is the second face of it: every function
against every principal, both ways round.

Exhaustive rather than spot-checked on purpose. The per-patient verification
defect in r1 survived to be found later precisely because one cell of a matrix
was never asserted, and a policy cube has three times the places to hide.

No model, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.policy.gates import CLINICAL_ONLY, PATIENT_WORKFLOWS, TOOL_POLICY, PolicyGate
from app.policy.messages import DenialCode, Remedy
from app.store.session import Role, Session
from app.tools import registry
from app.tools.schemas import ARGUMENT_MODELS, ClinicalRole
from tests.test_gates import VALID_ARGS, satisfy_preconditions, verified

CLINICAL_FUNCTIONS = {name for name, p in TOOL_POLICY.items() if Role.PATIENT not in p.roles}
PATIENT_FUNCTIONS = {name for name, p in TOOL_POLICY.items() if Role.PATIENT in p.roles}


@pytest.fixture
def gate(clinic) -> PolicyGate:
    return PolicyGate(clinic)


def clinical(*, authenticated: bool = True, expired: bool = False) -> Session:
    session = Session(role=Role.CLINICAL_ASSISTANT, channel="clinical")
    session.existence_checked = True
    if authenticated:
        when = (
            datetime.now(UTC) - timedelta(seconds=1)
            if expired
            else datetime.now(UTC) + timedelta(minutes=30)
        )
        session.bind_clinical_authentication("STAFF-2001", ClinicalRole.PHYSICIAN, when)
    return session


def args_for(fn_name: str) -> dict[str, Any]:
    return dict(VALID_ARGS[fn_name])


# ------------------------------------------------------- the inventory ---


def test_every_policy_declares_its_principals():
    """A function with an empty role tuple would be unreachable by everyone,
    which is a bug that looks like security."""
    for name, policy in TOOL_POLICY.items():
        assert policy.roles, f"{name} is registered for no principal"
        assert set(policy.roles) <= set(Role), name


def test_the_clinical_group_is_exactly_what_section_2_lists():
    """§2 names four, and all four now exist.

    Pinned as the exact set rather than a subset, so a fifth clinical function
    cannot appear without somebody deciding its policy entry — and so one
    quietly leaving the group fails here too.
    """
    assert {
        "authenticate_clinical_user",
        "search_clinical_knowledge",
        "summarize_diagnostic_considerations",
        "get_dosage_information",
    } == CLINICAL_FUNCTIONS


def test_no_patient_function_became_clinical_only():
    """The other direction — a §4.1–§4.12 function quietly leaving the patient
    schema would break the front desk without failing anything else."""
    assert len(PATIENT_FUNCTIONS) == 16
    assert "book_appointment" in PATIENT_FUNCTIONS
    assert "escalate_to_staff" in PATIENT_FUNCTIONS


def test_patient_workflows_are_open_to_both_principals():
    """§1.1 — the clinical role also performs *"the patient-facing workflows
    performed on a patient's behalf"*."""
    assert set(PATIENT_WORKFLOWS) == {Role.PATIENT, Role.CLINICAL_ASSISTANT}
    for name in PATIENT_FUNCTIONS:
        assert Role.CLINICAL_ASSISTANT in TOOL_POLICY[name].roles, name


def test_only_section_4_14_to_4_16_require_authentication():
    """§4.13 scopes the requirement. authenticate_clinical_user itself must not
    require it, or clinical review would be unreachable."""
    assert TOOL_POLICY["authenticate_clinical_user"].requires_clinical_auth is False
    for name in PATIENT_FUNCTIONS:
        assert TOOL_POLICY[name].requires_clinical_auth is False, name

    # Everything else in the clinical group does require it (§4.13).
    for name in CLINICAL_FUNCTIONS - {"authenticate_clinical_user"}:
        assert TOOL_POLICY[name].requires_clinical_auth is True, name


# --------------------------------------------------------- the tool schema ---


@pytest.mark.parametrize("fn_name", sorted(CLINICAL_FUNCTIONS))
def test_a_clinical_function_is_absent_from_the_patient_schema(fn_name):
    """spec §2 — *"absent from the tool schema presented to a patient session,
    so a patient session cannot name them"*."""
    assert fn_name not in {d["name"] for d in registry.tool_definitions(Role.PATIENT)}


@pytest.mark.parametrize("fn_name", sorted(PATIENT_FUNCTIONS))
def test_every_patient_function_is_in_both_schemas(fn_name):
    assert fn_name in {d["name"] for d in registry.tool_definitions(Role.PATIENT)}
    assert fn_name in {d["name"] for d in registry.tool_definitions(Role.CLINICAL_ASSISTANT)}


def test_the_schemas_together_account_for_every_function():
    """No function may be unreachable from every principal."""
    reachable = {d["name"] for d in registry.tool_definitions(Role.PATIENT)} | {
        d["name"] for d in registry.tool_definitions(Role.CLINICAL_ASSISTANT)
    }

    assert reachable == set(ARGUMENT_MODELS)


def test_the_default_tool_list_is_the_patient_schema():
    """all_tools() is what the orchestrator has always called, so the default
    has to stay the safe one."""
    assert {t.name for t in registry.all_tools()} == {
        d["name"] for d in registry.tool_definitions(Role.PATIENT)
    }


def test_an_expired_clinical_session_still_sees_its_own_functions():
    """Keyed on the established role, not the effective one. Otherwise a
    clinician whose session lapsed would be told the capability never existed
    instead of that it expired."""
    schema = {t.name for t in registry.tools_for(clinical(expired=True).role)}

    assert schema >= CLINICAL_FUNCTIONS


def test_the_role_map_has_one_source_of_truth():
    """It was briefly duplicated in the registry. Two copies of an
    access-control fact is one too many."""
    for name in ARGUMENT_MODELS:
        assert registry.roles_for(name) == TOOL_POLICY[name].roles, name


def test_the_clinical_tool_set_is_derived_not_listed():
    assert registry.CLINICAL_TOOLS == CLINICAL_FUNCTIONS


# ------------------------------------------------- the cube, both directions ---


@pytest.mark.parametrize("fn_name", sorted(CLINICAL_FUNCTIONS))
def test_a_patient_session_is_told_a_clinical_function_does_not_exist(gate, fn_name):
    """spec §2 — *"answered as an unknown capability rather than as a refusal"*.

    The distinction is the point. "Refused" confirms the capability is there and
    that this caller is not allowed it, which is exactly the information a
    probing caller wants.
    """
    verdict = gate.evaluate(fn_name, args_for(fn_name), verified())

    assert not verdict.allowed
    assert verdict.code is DenialCode.UNKNOWN_FUNCTION
    assert verdict.remedy_key is Remedy.USE_CLINICAL_CHANNEL


@pytest.mark.parametrize("fn_name", sorted(CLINICAL_FUNCTIONS))
def test_a_clinical_function_is_reachable_by_a_clinical_session(gate, fn_name):
    """The other direction. A boundary that refuses everybody is not a
    boundary, it is an outage."""
    session = satisfy_preconditions(clinical(), fn_name)

    assert gate.evaluate(fn_name, args_for(fn_name), session).allowed


@pytest.mark.parametrize("fn_name", sorted(PATIENT_FUNCTIONS))
def test_a_clinical_session_reaches_the_patient_workflows(gate, fn_name):
    """§1.1 gives the clinical role the patient-facing workflows too, and §3.2's
    last bullet keeps them behind the ordinary §3.1 path — which this session
    has satisfied for a patient, separately from its clinical authentication."""
    session = clinical()
    session.mark_identified("PT-4101")
    session.mark_verified([])
    session = satisfy_preconditions(session, fn_name)

    verdict = gate.evaluate(fn_name, args_for(fn_name), session)

    assert verdict.allowed, f"{fn_name}: {verdict.code} — {verdict.detail}"


def test_a_patient_session_is_unaffected_by_any_of_this(gate):
    """The r1 behaviour, still true. The role check must be invisible to the
    principal that was there before it existed."""
    for fn_name in sorted(PATIENT_FUNCTIONS):
        session = satisfy_preconditions(verified(), fn_name)
        verdict = gate.evaluate(fn_name, args_for(fn_name), session)
        assert verdict.allowed, f"{fn_name}: {verdict.code} — {verdict.detail}"


# ------------------------------------------------------------- expiry ---


def test_an_unauthenticated_clinical_session_can_still_authenticate(gate):
    """Otherwise you would need to be authenticated to authenticate."""
    verdict = gate.evaluate(
        "authenticate_clinical_user",
        args_for("authenticate_clinical_user"),
        clinical(authenticated=False),
    )

    assert verdict.allowed


def test_the_gate_reads_the_effective_role_for_authentication(gate):
    """The composition C0 set up: role decides the schema, effective_role
    decides whether the capability is live. Asserted here because nothing else
    exercises both at once."""
    live = clinical()
    lapsed = clinical(expired=True)

    assert live.effective_role is Role.CLINICAL_ASSISTANT
    assert lapsed.effective_role is Role.SYSTEM
    assert lapsed.role is Role.CLINICAL_ASSISTANT


def test_expiry_is_reported_as_expiry_not_as_a_missing_role(gate, clinic):
    """A clinician whose session lapsed and one who never authenticated need
    different things — a new session, or a first authentication. §4.13 makes
    both an authorization error; conflating them wastes the clinician's turn.

    Exercised against a stand-in policy because no §4.14–§4.16 function exists
    yet — the requirement is what is under test, not any one function.
    """
    from dataclasses import replace

    protected = replace(
        TOOL_POLICY["authenticate_clinical_user"],
        roles=CLINICAL_ONLY,
        requires_clinical_auth=True,
    )
    args = args_for("authenticate_clinical_user")

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(TOOL_POLICY, "authenticate_clinical_user", protected)

        never = gate.evaluate("authenticate_clinical_user", args, clinical(authenticated=False))
        lapsed = gate.evaluate("authenticate_clinical_user", args, clinical(expired=True))

    assert never.code is DenialCode.ROLE_REQUIRED
    assert never.remedy_key is Remedy.AUTHENTICATE_FIRST

    assert lapsed.code is DenialCode.SESSION_EXPIRED
    assert lapsed.remedy_key is Remedy.REAUTHENTICATE


def test_the_expiry_remedy_forbids_answering_anyway(gate):
    """spec §6 — *"Do not degrade to a partial answer, a general answer, or an
    answer from model knowledge."* The remedy is where the model reads that."""
    from app.policy.messages import remedy_text

    text = remedy_text(Remedy.REAUTHENTICATE)

    assert "own knowledge" in text
    assert "partial" in text
    assert "cannot be extended" in text


# -------------------------------------------------------- the check order ---


def test_role_is_checked_before_the_schema(gate):
    """Ordering claim. Telling an unauthorised caller that their arguments were
    malformed confirms the function exists and describes its signature."""
    verdict = gate.evaluate(
        "authenticate_clinical_user", {"definitely_not_a_field": True}, verified()
    )

    assert verdict.code is DenialCode.UNKNOWN_FUNCTION


def test_role_is_checked_before_authorization(gate):
    """An anonymous patient session naming a clinical function gets
    unknown_function, not verification_required — otherwise the denial would
    tell them the capability exists and how to try for it."""
    verdict = gate.evaluate(
        "authenticate_clinical_user", args_for("authenticate_clinical_user"), Session()
    )

    assert verdict.code is DenialCode.UNKNOWN_FUNCTION
    assert verdict.code is not DenialCode.VERIFICATION_REQUIRED


def test_the_patient_check_order_is_unchanged(gate):
    """r1's ordering, still true: schema before authorization. The role check
    was inserted ahead of both, not woven into them."""
    verdict = gate.evaluate("get_patient_demographics", {"patient_id": 12345}, Session())

    assert verdict.code is DenialCode.INVALID_ARGUMENTS


def test_a_denial_never_names_the_capability_it_withheld(gate):
    """§7.3 — *"Where a patient's question can only be answered by
    clinician-only material, the answer is an escalation to a human, not a
    partial disclosure."* A remedy describing what the function would have said
    is a partial disclosure."""
    verdict = gate.evaluate(
        "authenticate_clinical_user", args_for("authenticate_clinical_user"), verified()
    )

    blob = f"{verdict.message} {verdict.remedy}".lower()
    for leak in ("dosage", "diagnos", "treatment", "mg/kg"):
        assert leak not in blob, blob


# ------------------------------------------------------ the model call ---


def test_the_turn_sends_the_schema_for_the_session_role(sim, clinic):
    """The wiring, end to end: §2's split has to reach the request, not just the
    registry. A tool list captured at construction would hand a patient session
    the clinical functions the moment a clinical session existed."""
    from app.config import Settings
    from app.orchestrator import AnthropicBackend

    backend = AnthropicBackend(
        settings=Settings(anthropic_api_key="k", model_provider="anthropic"), client=object()
    )

    patient = {t.name for t in backend._tools(Role.PATIENT)}
    clinician = {t.name for t in backend._tools(Role.CLINICAL_ASSISTANT)}

    assert patient & CLINICAL_FUNCTIONS == set()
    assert clinician >= CLINICAL_FUNCTIONS


def test_the_orchestrator_passes_the_session_role_to_the_backend(sim, clinic):
    from app.orchestrator import Orchestrator
    from tests.replay import Say, ScriptedBackend, ScriptedPrescreen

    backend = ScriptedBackend(script=[[Say("hello")]])
    orchestrator = Orchestrator(
        sim=sim,
        clinic=clinic,
        prescreen=ScriptedPrescreen(),
        backend=backend,
        knowledge=None,
    )

    orchestrator.run_turn(Session(), "are you open?")

    assert backend.seen_roles == [Role.PATIENT]


def test_a_clinical_session_will_not_run_on_the_patient_prompt(sim, clinic):
    """system.md opens as a receptionist, forbids diagnostic guidance, and
    carries the §4.2 masking rules for a patient having their own record read
    back. Running a clinician on it would put the wrong frame on the whole
    exchange — so until C7 supplies the clinical prompt, the turn fails loudly
    rather than looking like it worked.
    """
    from app.orchestrator import Orchestrator, PromptUnavailable
    from tests.replay import ScriptedBackend, ScriptedPrescreen

    orchestrator = Orchestrator(
        sim=sim,
        clinic=clinic,
        prescreen=ScriptedPrescreen(),
        backend=ScriptedBackend(script=[[]]),
        knowledge=None,
    )

    with pytest.raises(PromptUnavailable, match="C7"):
        orchestrator.system_blocks(Role.CLINICAL_ASSISTANT)


def test_the_patient_prompt_still_renders(sim, clinic):
    from app.orchestrator import Orchestrator
    from tests.replay import ScriptedBackend, ScriptedPrescreen

    orchestrator = Orchestrator(
        sim=sim,
        clinic=clinic,
        prescreen=ScriptedPrescreen(),
        backend=ScriptedBackend(script=[[]]),
        knowledge=None,
    )

    blocks = orchestrator.system_blocks(Role.PATIENT)

    assert blocks[0]["text"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
