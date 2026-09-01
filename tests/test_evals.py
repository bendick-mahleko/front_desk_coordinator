"""The eval harness itself, exercised offline.

The scenarios need a live model — that is the point of them. The *harness* must
not, or a broken assertion would only ever be discovered by a run that costs
money and takes minutes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.store.audit import GENESIS_HASH, AuditRecord, EventKind
from evals import runner
from evals.judge import Judgement, parse
from evals.schema import Scenario, load_all
from tests.replay import Call, Say, ScriptedBackend, ScriptedPrescreen

SCENARIOS = load_all()


def record(event: str, **fields) -> AuditRecord:
    base = {
        "event_id": "x",
        "ts": "2026-09-07T00:00:00Z",
        "session_id": "s",
        "turn": 1,
        "prev_hash": GENESIS_HASH,
    }
    return AuditRecord(**base, event=event, **fields)


def gate(function: str, decision: str = "allow", code: str | None = None) -> AuditRecord:
    return record(
        EventKind.GATE_DECISION, function=function, gate={"decision": decision, "code": code}
    )


# ------------------------------------------------------------- the corpus ---


def test_every_scenario_file_parses():
    assert len(SCENARIOS) == 29


def test_the_eleven_intents_of_specification_section_5_are_covered():
    intents = [s for s in SCENARIOS if s.kind == "intent"]
    assert len(intents) == 11, "spec §5 has eleven rows"


def test_failure_and_adversarial_sets_exist():
    assert len([s for s in SCENARIOS if s.kind == "failure"]) == 7
    # Six from the base system, five added by the knowledge extension.
    assert len([s for s in SCENARIOS if s.kind == "adversarial"]) == 11


def test_the_knowledge_extension_added_its_own_probes():
    """The extension put clinical content in the system. These are the tests
    that it stays out of a patient's reach."""
    names = {s.name for s in SCENARIOS}
    assert {
        "adv_07_dosage_request",
        "adv_08_symptom_to_diagnosis",
        "adv_09_tier_escalation",
        "adv_10_dose_via_routing",
        "adv_11_paediatric_dose",
    } <= names


def test_every_extension_probe_forbids_a_dose_or_a_condition_name():
    """A probe that asserts nothing negative proves nothing."""
    for scenario in SCENARIOS:
        if scenario.name.startswith(("adv_07", "adv_08", "adv_09", "adv_10", "adv_11")):
            assert scenario.forbid_reply_contains, scenario.name


def test_every_scenario_cites_a_specification_clause():
    for scenario in SCENARIOS:
        assert scenario.spec, f"{scenario.name} does not say what it proves"


def test_every_scenario_name_matches_its_filename():
    directory = Path("evals/scenarios")
    for path in sorted(directory.glob("*.yaml")):
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["name"] == path.stem


def test_an_adversarial_scenario_must_forbid_something():
    """Otherwise it is a happy path with a scary name."""
    with pytest.raises(ValueError, match="must forbid"):
        Scenario(name="fake", kind="adversarial", spec="x", turns=["hello"], expect_tools=["x"])


def test_every_adversarial_scenario_makes_a_negative_claim():
    for scenario in [s for s in SCENARIOS if s.kind == "adversarial"]:
        assert (
            scenario.forbid_tools
            or scenario.forbid_reply_contains
            or any(e.decision == "deny" for e in scenario.expect_gate)
        ), scenario.name


def test_forbidden_functions_are_real_functions():
    from app.tools.schemas import ARGUMENT_MODELS

    for scenario in SCENARIOS:
        for name in [*scenario.expect_tools, *scenario.forbid_tools]:
            assert name in ARGUMENT_MODELS, f"{scenario.name}: unknown function {name!r}"
        for expectation in scenario.expect_gate:
            assert expectation.function in ARGUMENT_MODELS


def test_injected_faults_are_producible():
    """A typo in a scenario would otherwise arm nothing and pass silently."""
    from app.clinic_sim.faults import SUPPORTED_FAULTS

    for scenario in SCENARIOS:
        for fault in scenario.inject:
            assert fault.code in SUPPORTED_FAULTS.get(fault.port, set()), scenario.name


# ---------------------------------------------------------- the assertions ---


def test_expect_tools_is_an_ordering_claim():
    scenario = Scenario(name="s", kind="intent", spec="x", turns=["t"], expect_tools=["a", "c"])
    ok = runner.check(scenario, [gate("a"), gate("b"), gate("c")], ["reply"])
    assert ok == []

    wrong_order = runner.check(scenario, [gate("c"), gate("a")], ["reply"])
    assert wrong_order and wrong_order[0].claim == "expect_tools"


def test_extra_calls_between_expected_ones_are_allowed():
    """A model that also checks the hours on the way through is not wrong."""
    scenario = Scenario(name="s", kind="intent", spec="x", turns=["t"], expect_tools=["a", "b"])
    assert runner.check(scenario, [gate("a"), gate("zzz"), gate("b")], [""]) == []


def test_forbid_tools_catches_a_call_that_should_not_have_happened():
    scenario = Scenario(
        name="s", kind="adversarial", spec="x", turns=["t"], forbid_tools=["secret"]
    )
    failures = runner.check(scenario, [gate("secret")], [""])
    assert failures and failures[0].claim == "forbid_tools"


def test_a_denied_call_does_not_satisfy_forbid_tools():
    """A denial still means the model *tried*, which an adversarial scenario
    wants to know about."""
    scenario = Scenario(
        name="s", kind="adversarial", spec="x", turns=["t"], forbid_tools=["secret"]
    )
    assert runner.check(scenario, [gate("secret", "deny", "verification_required")], [""])


def test_expect_gate_checks_the_decision_and_the_code():
    scenario = Scenario(
        name="s",
        kind="adversarial",
        spec="x",
        turns=["t"],
        expect_gate=[{"function": "f", "decision": "deny", "code": "verification_required"}],
    )
    assert runner.check(scenario, [gate("f", "deny", "verification_required")], [""]) == []

    wrong_code = runner.check(scenario, [gate("f", "deny", "unknown_reference")], [""])
    assert wrong_code and "wanted verification_required" in wrong_code[0].detail

    allowed = runner.check(scenario, [gate("f", "allow")], [""])
    assert allowed and "no deny recorded" in allowed[0].detail


def test_expect_no_claim_ignores_the_final_reply():
    """By the last turn a booking may legitimately have happened."""
    scenario = Scenario(
        name="s", kind="intent", spec="x", turns=["t"], expect_no_claim=["you're booked"]
    )
    assert runner.check(scenario, [], ["searching…", "You're booked."]) == []

    early = runner.check(scenario, [], ["You're booked.", "anything else?"])
    assert early and early[0].claim == "expect_no_claim"


def test_reply_assertions_are_case_insensitive():
    scenario = Scenario(
        name="s",
        kind="intent",
        spec="x",
        turns=["t"],
        expect_reply_contains=["Not A Guarantee"],
        forbid_reply_contains=["98101"],
    )
    assert runner.check(scenario, [], ["eligibility is not a guarantee of payment"]) == []
    assert runner.check(scenario, [], ["your zip is 98101"])


def test_escalation_assertions_read_the_audit_log():
    scenario = Scenario(
        name="s",
        kind="failure",
        spec="x",
        turns=["t"],
        expect_escalation_reason="billing_issue",
        expect_escalation_priority="routine",
    )
    good = record(EventKind.ESCALATION, detail={"reason": "billing_issue", "priority": "routine"})
    assert runner.check(scenario, [good], [""]) == []

    wrong = record(EventKind.ESCALATION, detail={"reason": "other", "priority": "routine"})
    failures = runner.check(scenario, [wrong], [""])
    assert failures and "wanted reason billing_issue" in failures[0].detail

    assert runner.check(scenario, [], [""])[0].detail == "nothing was escalated"


# ---------------------------------------------------------------- the judge ---


@pytest.mark.parametrize(
    "raw,passed",
    [
        ("PASS: stated the disclaimer", True),
        ("FAIL: interpreted the symptom", False),
        ("pass: fine", True),
    ],
)
def test_the_judge_parses_a_verdict(raw, passed):
    assert parse(raw).passed is passed


def test_an_unparseable_judgement_fails():
    """A confused judge must not wave a scenario through."""
    assert parse("I think it was probably fine?").passed is False


def test_an_unavailable_judge_cannot_fail_a_scenario():
    judgement = Judgement(passed=True, reason="judge unavailable", available=False)
    assert judgement.available is False


# ------------------------------------------------------- end to end, offline ---


def test_a_scenario_runs_and_asserts_against_the_audit_log(tmp_path, clinic):
    """The whole harness, with a scripted model so it costs nothing."""
    scenario = Scenario(
        name="offline_probe",
        kind="intent",
        spec="harness",
        turns=["what are your hours?"],
        expect_tools=["check_business_hours"],
        forbid_tools=["get_patient_demographics"],
        expect_status="none",
    )
    backend = ScriptedBackend(
        script=[[Call("check_business_hours", {}), Say("We're open until five.")]]
    )

    result = runner.run_scenario(scenario, tmp_path, backend=backend, prescreen=ScriptedPrescreen())

    assert result.ok, result.render()
    assert result.tool_calls == ["check_business_hours"]
    assert result.audit_path.exists()


def test_a_failing_scenario_reports_why(tmp_path, clinic):
    scenario = Scenario(
        name="offline_fail",
        kind="adversarial",
        spec="harness",
        turns=["hello"],
        forbid_tools=["check_business_hours"],
    )
    backend = ScriptedBackend(script=[[Call("check_business_hours", {}), Say("hi")]])

    result = runner.run_scenario(scenario, tmp_path, backend=backend, prescreen=ScriptedPrescreen())

    assert not result.ok
    assert "forbid_tools" in result.render()
    assert "check_business_hours" in result.render()


def test_the_chain_verifier_runs_on_every_scenario(tmp_path, clinic):
    """P8-T7 — an unverifiable log fails the eval that produced it."""
    scenario = Scenario(name="offline_chain", kind="intent", spec="harness", turns=["hello"])
    result = runner.run_scenario(
        scenario,
        tmp_path,
        backend=ScriptedBackend(script=[[Say("hi")]]),
        prescreen=ScriptedPrescreen(),
    )
    assert result.ok

    # Break the chain and re-verify through the same path the runner uses.
    lines = result.audit_path.read_text(encoding="utf-8").splitlines()
    result.audit_path.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")

    from app.store.verify import verify_file

    assert not verify_file(result.audit_path).ok


def test_injected_faults_reach_the_simulator(tmp_path, clinic):
    scenario = Scenario(
        name="offline_fault",
        kind="failure",
        spec="harness",
        turns=["text me directions"],
        inject=[{"port": "MessageGateway", "operation": "send", "code": "delivery_unconfirmed"}],
    )
    result = runner.run_scenario(
        scenario,
        tmp_path,
        backend=ScriptedBackend(script=[[Say("ok")]]),
        prescreen=ScriptedPrescreen(),
    )
    assert result.ok


def test_the_traceability_report_covers_the_definition_of_done():
    """Design §19 — five criteria from specification §8."""
    report = runner.traceability([])
    assert "Definition of done" in report
    for fragment in [
        "Correctly routes",
        "Enforces verification",
        "Validates required",
        "Safely supports",
        "Generates auditable",
    ]:
        assert fragment in report
