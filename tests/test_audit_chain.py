"""P6-T7 — chain integrity, tamper detection, and no protected data in the log.

Phase 6's exit test: after a full booking conversation the verifier passes,
editing one byte makes it fail, and a scan for fixture values finds nothing.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlmodel import SQLModel, create_engine

from app.orchestrator import Orchestrator
from app.store.audit import (
    GENESIS_HASH,
    AuditRecord,
    AuditWriter,
    EventKind,
    extract_refs,
    summarise_outcome,
)
from app.store.models import AuditMirror
from app.store.session import Session
from app.store.verify import verify_directory, verify_file
from app.tools.schemas import AppointmentType, Modality
from tests.replay import Call, Say, ScriptedBackend, ScriptedPrescreen

# Values from the patient fixture. None may ever appear in the log.
FIXTURE_VALUES = ["1978-03-04", "2065550142", "98101", "amara.osei@example.invalid", "Amara"]


@pytest.fixture
def writer(tmp_path) -> AuditWriter:
    return AuditWriter(directory=tmp_path / "audit", day="2026-09-07")


@pytest.fixture
def mirror(tmp_path) -> AuditMirror:
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    SQLModel.metadata.create_all(engine)
    return AuditMirror(engine=engine)


# ------------------------------------------------------------- the chain ---


def test_the_first_record_links_to_genesis(writer):
    record = writer.turn_started("s_1", 1)

    assert record.prev_hash == GENESIS_HASH
    assert record.hash == record.compute_hash()


def test_each_record_links_to_the_one_before(writer):
    first = writer.turn_started("s_1", 1)
    second = writer.turn_completed("s_1", 1, outcome="ok")

    assert second.prev_hash == first.hash
    assert writer.last_hash == second.hash


def test_a_clean_chain_verifies(writer):
    for turn in range(1, 4):
        writer.turn_started("s_1", turn)
        writer.turn_completed("s_1", turn, outcome="ok")

    report = verify_file(writer.path)

    assert report.ok, report.render()
    assert report.records == 6
    assert "chain intact" in report.render()


def test_altering_one_byte_breaks_the_chain(writer):
    """AD-06 — "auditable" implies detectable tampering."""
    writer.turn_started("s_1", 1)
    writer.gate_decision("s_1", 1, "get_patient_appointments", {}, {"decision": "deny"})
    writer.turn_completed("s_1", 1, outcome="ok")

    lines = writer.path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["gate"]["decision"] = "allow"  # the edit an attacker would want
    lines[1] = json.dumps(tampered)
    writer.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = verify_file(writer.path)

    assert not report.ok
    kinds = {problem.kind for problem in report.problems}
    assert "altered" in kinds
    assert report.problems[0].line == 2, "the verifier localises the tampering"


def test_deleting_a_record_breaks_the_chain(writer):
    """Removing an inconvenient line is the other thing a chain must catch."""
    for turn in range(1, 4):
        writer.turn_started("s_1", turn)

    lines = writer.path.read_text(encoding="utf-8").splitlines()
    del lines[1]
    writer.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = verify_file(writer.path)

    assert not report.ok
    assert any(problem.kind == "broken_link" for problem in report.problems)


def test_appending_a_forged_record_is_caught(writer):
    writer.turn_started("s_1", 1)

    forged = AuditRecord(
        event_id="deadbeef",
        ts="2026-09-07T00:00:00Z",
        session_id="s_1",
        turn=1,
        event=EventKind.GATE_DECISION,
        outcome="allowed",
        prev_hash=GENESIS_HASH,  # does not follow the real last hash
    )
    forged.hash = forged.compute_hash()
    with writer.path.open("a", encoding="utf-8") as handle:
        handle.write(forged.model_dump_json(exclude_none=True) + "\n")

    report = verify_file(writer.path)
    assert any(problem.kind == "broken_link" for problem in report.problems)


def test_a_malformed_line_is_reported_not_skipped(writer):
    writer.turn_started("s_1", 1)
    with writer.path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")

    report = verify_file(writer.path)
    assert any(problem.kind == "malformed" for problem in report.problems)


def test_a_restart_continues_the_chain(writer, tmp_path):
    """A second writer over the same file must not start a second chain."""
    first = writer.turn_started("s_1", 1)

    resumed = AuditWriter(directory=tmp_path / "audit", day="2026-09-07")
    second = resumed.turn_started("s_1", 2)

    assert second.prev_hash == first.hash
    assert verify_file(writer.path).ok


def test_a_missing_file_is_a_problem_not_a_pass(tmp_path):
    report = verify_file(tmp_path / "nope.jsonl")
    assert not report.ok


# ------------------------------------------------------- what is recorded ---


def test_a_gate_decision_records_the_rule_and_both_levels(writer):
    record = writer.gate_decision(
        "s_1",
        1,
        "get_patient_appointments",
        {"patient_id": "PT-4101"},
        {
            "decision": "deny",
            "required": "verified",
            "actual": "identified",
            "code": "verification_required",
            "rule": "spec§3/get_scheduled_appointments",
        },
    )

    assert record.outcome == "denied"
    assert record.gate["rule"] == "spec§3/get_scheduled_appointments"
    assert record.refs == {"patient_id": "PT-4101"}


def test_arguments_are_redacted_before_they_are_written(writer):
    record = writer.gate_decision(
        "s_1",
        1,
        "create_new_patient_record",
        {"first_name": "Amara", "date_of_birth": "1978-03-04", "phone_number": "+12065550142"},
        {"decision": "allow"},
    )

    assert record.args["date_of_birth"] == "<dob>"
    assert record.args["phone_number"] == "<phone>"


def test_a_tool_result_records_its_shape_not_its_contents(writer):
    """spec §15 — the log says a demographics call happened, never what it returned."""
    record = writer.tool_result(
        "s_1",
        1,
        "get_patient_demographics",
        {
            "patient_id": "PT-4101",
            "first_name": "Amara",
            "date_of_birth": "1978-03-04",
            "address_zip": "98101",
        },
    )

    written = record.model_dump_json()
    assert record.outcome == "ok"
    assert record.refs == {"patient_id": "PT-4101"}
    for value in FIXTURE_VALUES:
        assert value not in written


def test_a_verification_records_the_method_but_never_the_values(writer):
    """spec §4.2."""
    record = writer.verification(
        "s_1",
        2,
        {"verified": True, "methods": ["dob", "address_zip"], "attempts_used": 0},
        patient_id="PT-4101",
    )

    assert record.outcome == "verified"
    assert record.detail["methods"] == ["dob", "address_zip"]
    written = record.model_dump_json()
    assert "1978-03-04" not in written and "98101" not in written


def test_an_escalation_records_the_ticket_and_priority(writer):
    record = writer.escalation(
        "s_1", 1, {"reason": "complex_symptoms", "priority": "emergency"}, ticket_id="ESC-5001"
    )

    assert record.outcome == "emergency"
    assert record.refs["ticket_id"] == "ESC-5001"


def test_errors_and_refusals_are_recorded(writer):
    assert writer.model_error("s_1", 1, "APITimeoutError").error == "APITimeoutError"
    assert writer.refusal("s_1", 1, "cyber").outcome == "refused"


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"error": "verification_required"}, "verification_required"),
        ({"ok": True}, "ok"),
        ('{"error": "slot_unavailable"}', "slot_unavailable"),
        ("not json at all", "ok"),
        (None, "ok"),
    ],
)
def test_outcome_summary(result, expected):
    assert summarise_outcome(result) == expected


def test_refs_are_pulled_from_nested_results():
    refs = extract_refs(
        [{"appointment_id": "AP-1", "patient_id": "PT-4101"}, {"appointment_id": "AP-2"}]
    )
    assert refs["patient_id"] == "PT-4101"
    assert refs["appointment_id"] == "AP-1"


# ------------------------------------------------------------- the mirror ---


def test_records_are_queryable_from_sqlite(writer, mirror):
    for record in [
        writer.turn_started("s_1", 1),
        writer.gate_decision("s_1", 1, "get_patient_appointments", {}, {"decision": "deny"}),
        writer.turn_completed("s_1", 1, outcome="ok"),
    ]:
        mirror.mirror(record)

    rows = mirror.for_session("s_1")
    assert len(rows) == 3
    assert [row.event for row in rows][0] == EventKind.TURN_STARTED
    assert len(mirror.denials("s_1")) == 1


def test_the_mirror_keeps_the_line_verbatim(writer, mirror):
    record = writer.gate_decision("s_1", 1, "book_appointment", {}, {"decision": "allow"})
    mirror.mirror(record)

    row = mirror.for_session("s_1")[0]
    assert json.loads(row.payload)["hash"] == record.hash


# ---------------------------------------------- the end-to-end exit test ---


def test_a_full_booking_conversation_produces_a_verifiable_log(
    sim, clinic, tmp_path, today, mirror
):
    """Phase 6 exit test."""
    slot = sim.schedule.search_slots(
        AppointmentType.FOLLOW_UP, today, today + timedelta(days=14), Modality.IN_PERSON
    )[0]
    audit = AuditWriter(directory=tmp_path / "audit", day="2026-09-07")

    orchestrator = Orchestrator(
        sim=sim,
        clinic=clinic,
        prescreen=ScriptedPrescreen(),
        audit=audit,
        mirror=mirror,
        backend=ScriptedBackend(
            script=[
                [
                    Call("get_patient_appointments", {"patient_id": "PT-4101"}),
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
                    Say("You're booked in."),
                ]
            ]
        ),
    )

    session = Session()
    orchestrator.run_turn(session, "I'm Amara Osei, born 1978-03-04, I'd like to book.")

    # 1 — the chain verifies
    report = verify_file(audit.path)
    assert report.ok, report.render()

    # 2 — every decision is present
    events = [record.event for record in audit.records()]
    assert events[0] == EventKind.TURN_STARTED
    assert events[-1] == EventKind.TURN_COMPLETED
    assert events.count(EventKind.GATE_DECISION) == 5
    assert EventKind.VERIFICATION in events
    assert EventKind.PRESCREEN in events

    # 3 — the denial is recorded, not just the successes
    denied = [r for r in audit.records() if r.outcome == "denied"]
    assert len(denied) == 1
    assert denied[0].function == "get_patient_appointments"
    assert denied[0].gate["code"] == "verification_required"

    # 4 — no protected value anywhere in the file
    raw = audit.path.read_text(encoding="utf-8")
    for value in FIXTURE_VALUES:
        assert value not in raw, f"{value} leaked into the audit log"

    # 5 — and the mirror agrees with the file
    assert len(mirror.for_session(session.session_id)) == len(list(audit.records()))


def test_the_verifier_scans_a_directory(writer, tmp_path):
    writer.turn_started("s_1", 1)
    reports = verify_directory(tmp_path / "audit")

    assert len(reports) == 1
    assert reports[0].ok


def test_a_five_digit_latency_is_not_a_zip_code(writer):
    """Regression: a slow turn used to trip the scan.

    Serialising the record to JSON and matching against the blob conflated
    types. An alarm that cries wolf on every slow turn is one nobody reads.
    """
    writer.turn_completed("s_1", 1, outcome="ok", latency_ms=12170)

    assert verify_file(writer.path).ok


def test_a_slot_id_containing_a_date_is_not_a_date_of_birth(writer):
    """Regression: slot ids embed a calendar date (SL-2026-09-07-1-1).

    They are clinic-issued references, not facts about a person, and `refs` is
    populated only from SAFE_REFERENCE_FIELDS.
    """
    writer.tool_result(
        "s_1", 1, "search_available_appointments", [{"slot_id": "SL-2026-09-07-1-1"}]
    )

    assert verify_file(writer.path).ok


def test_a_patient_name_never_reaches_the_log(writer):
    """A name identifies as surely as a date of birth does.

    The clinic-issued patient_id gives an auditor everything needed to trace a
    decision without the log itself becoming a patient index.
    """
    record = writer.gate_decision(
        "s_1",
        1,
        "check_patient_exists",
        {"first_name": "Amara", "last_name": "Osei", "date_of_birth": "1978-03-04"},
        {"decision": "allow"},
    )

    assert record.args["first_name"] == "<name>"
    assert record.args["last_name"] == "<name>"
    assert "Amara" not in writer.path.read_text(encoding="utf-8")


def test_a_planted_value_is_caught_by_the_pii_scan(writer):
    """The writer redacts on the way in; the verifier asserts on the way out."""
    writer.turn_started("s_1", 1)
    lines = writer.path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["detail"] = {"leaked": "patient dob is 1978-03-04"}
    lines[0] = json.dumps(record)
    writer.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = verify_file(writer.path)
    assert any(problem.kind == "pii" for problem in report.problems)
