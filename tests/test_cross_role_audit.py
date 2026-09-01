"""C6 — the audit record's role, and §7.3's verifier assertion.

§7.3: clinician-only material *"must never appear in a patient-facing turn, in a
secure text message, in an appointment record, or in any patient-visible
artifact. This is enforced at retrieval, asserted in the audit verifier, and
tested adversarially."* Three mechanisms; this file is the middle one.

The asymmetry is the whole point and is easy to get backwards: **a dose in a
clinical session's log is the feature working, and the same dose in a patient
session's log is a leak.** Only the record's role tells them apart, so the tests
below check both directions — that the scan fires on the second, and that it does
*not* fire on the first.

No model, no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from app.knowledge.chunking import chunk_all
from app.knowledge.corpus import load
from app.knowledge.embedding import HashingEmbedder
from app.knowledge.store import InMemoryKnowledgeBase
from app.orchestrator import TurnRecorder
from app.policy.clinical import CLINICIAN_MARKERS, DOSE, clinical_content
from app.policy.decorator import session_scope
from app.policy.gates import PolicyGate
from app.store.audit import AuditWriter, audit_role
from app.store.session import Role, Session
from app.store.verify import verify_file
from app.tools import registry
from app.tools.schemas import ClinicalRole

CLINICAL_CALLS = [
    (
        "authenticate_clinical_user",
        {
            "staff_id": "STAFF-2001",
            "credential_token": "fixture-token-alvarez",
            "asserted_role": "physician",
        },
    ),
    (
        "search_clinical_knowledge",
        {"query": "amoxicillin 500mg antibiotic dosage", "tier": "clinician_only", "k": 2},
    ),
    ("get_dosage_information", {"condition_name": "Cystitis", "cohort": "both"}),
    (
        "summarize_diagnostic_considerations",
        {"presentation": "productive cough, fever and rigors"},
    ),
]

PATIENT_CALLS = [
    (
        "check_patient_exists",
        {"first_name": "Amara", "last_name": "Osei", "date_of_birth": "1978-03-04"},
    ),
    (
        "escalate_to_staff",
        {
            "reason": "complex_symptoms",
            "priority": "routine",
            "notes": "productive cough, fever and rigors",
        },
    ),
]


@pytest.fixture(scope="module")
def kb():
    store = InMemoryKnowledgeBase(HashingEmbedder())
    store.index(chunk_all(load().records))
    return store


def clinical_session() -> Session:
    session = Session(role=Role.CLINICAL_ASSISTANT, channel="clinical")
    session.bind_clinical_authentication(
        "STAFF-2001", ClinicalRole.PHYSICIAN, datetime.now(UTC) + timedelta(minutes=30)
    )
    return session


@pytest.fixture
def run_turn(sim, clinic, kb, tmp_path):
    """Run tool calls through the real AuditWriter and return the log path."""

    def _run(session: Session, calls: list[tuple[str, dict[str, Any]]]) -> Path:
        directory = tmp_path / f"audit-{session.session_id}"
        writer = AuditWriter(directory=directory)
        recorder = TurnRecorder(session_id=session.session_id, turn=1, writer=writer)
        with (
            session_scope(session, gate=PolicyGate(clinic), audit=recorder),
            registry.backend_scope(sim),
            registry.knowledge_scope(kb),
        ):
            for name, args in calls:
                registry.load()[name].call(args)
        return next(directory.glob("audit-*.jsonl"))

    return _run


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ------------------------------------------------------ the role is recorded ---


def test_every_record_in_a_clinical_turn_names_the_clinical_role(run_turn):
    path = run_turn(clinical_session(), CLINICAL_CALLS)

    assert {record.get("role") for record in records(path)} == {"clinical_assistant"}


def test_every_record_in_a_patient_turn_names_the_patient_role(run_turn):
    session = Session()
    session.existence_checked = True

    path = run_turn(session, PATIENT_CALLS)

    assert {record.get("role") for record in records(path)} == {"patient"}


def test_the_role_is_bound_by_the_session_scope_not_by_each_call_site(tmp_path):
    """Eleven writer methods, one place that labels them. A ContextVar rather
    than a field on the writer, because one writer serves every session in the
    process and two concurrent requests must not label each other's records."""
    writer = AuditWriter(directory=tmp_path / "scoped")

    with audit_role("clinical_assistant"):
        inside = writer.turn_started("s_x", 1)
    outside = writer.turn_started("s_y", 1)

    assert inside.role == "clinical_assistant"
    assert outside.role is None


def test_a_record_written_outside_any_scope_gets_the_strict_scan(tmp_path):
    """Absent is treated as patient. A record nobody labelled is one nobody
    thought about, and the stricter reading is the right way for that to fail."""
    directory = tmp_path / "unscoped"
    writer = AuditWriter(directory=directory)
    writer.append(
        session_id="s_x", turn=1, event="tool_result", detail={"text": "Paracetamol 15mg/kg"}
    )

    report = verify_file(next(directory.glob("audit-*.jsonl")))

    assert not report.ok
    assert [problem.kind for problem in report.problems] == ["cross_role"]


# ----------------------------------------------- both directions of the scan ---


def test_a_clinical_turn_verifies_clean(run_turn):
    """The direction that is easy to get wrong. A clinician's query is logged
    verbatim by §4.14, dosage chunk ids name the clinician tier, and none of
    that is a leak — it is §4.14–§4.16 working."""
    report = verify_file(run_turn(clinical_session(), CLINICAL_CALLS))

    assert report.ok, [f"{p.kind}: {p.detail}" for p in report.problems]
    assert report.records == 12


def test_a_patient_turn_verifies_clean(run_turn):
    """Including the §4.12 escalation, which attaches clinician-only context to
    a staff ticket. The ticket carries it; the log does not."""
    session = Session()
    session.existence_checked = True

    report = verify_file(run_turn(session, PATIENT_CALLS))

    assert report.ok, [f"{p.kind}: {p.detail}" for p in report.problems]


def test_the_escalation_ticket_still_carries_the_clinician_context(run_turn, sim):
    """The other half of §4.12, asserted here so a later tightening of the log
    scan cannot be "fixed" by removing the context from the ticket."""
    session = Session()
    session.existence_checked = True
    run_turn(session, PATIENT_CALLS)

    assert DOSE.search(sim.staff.tickets()[-1].notes)


def test_a_dose_in_a_patient_record_is_caught(tmp_path):
    """The tripwire. Worth being straight that it catches nothing today —
    measured, a patient escalation puts no dose in the log, because the tool
    result records an outcome rather than a body and the notes argument is
    redacted on the way in. It fires the moment somebody starts logging a result
    body, which is a change that would otherwise look harmless."""
    directory = tmp_path / "leak"
    writer = AuditWriter(directory=directory)
    with audit_role("patient"):
        writer.append(
            session_id="s_x",
            turn=1,
            event="tool_result",
            function="escalate_to_staff",
            detail={"notes": "Amoxicillin 20-40mg/kg/day divided into 2-3 doses"},
        )

    report = verify_file(next(directory.glob("audit-*.jsonl")))

    assert not report.ok
    assert report.problems[0].kind == "cross_role"
    assert "dose figure" in report.problems[0].detail


def test_a_clinician_tier_marker_in_a_patient_record_is_caught(tmp_path):
    """Structural evidence, not content. A chunk id ending ``::management``
    names the tier it came from even when the text beside it is innocuous."""
    directory = tmp_path / "marker"
    writer = AuditWriter(directory=directory)
    with audit_role("patient"):
        writer.append(
            session_id="s_x",
            turn=1,
            event="clinical_retrieval",
            detail={"chunks": ["pneumonia::management"]},
        )

    report = verify_file(next(directory.glob("audit-*.jsonl")))

    assert not report.ok
    assert "clinician-tier marker" in report.problems[0].detail


def test_the_same_content_under_the_clinical_role_is_not_a_problem(tmp_path):
    """The asymmetry, stated as a test. If this failed, the scan would be
    forbidding the feature rather than the leak."""
    directory = tmp_path / "allowed"
    writer = AuditWriter(directory=directory)
    with audit_role("clinical_assistant"):
        writer.append(
            session_id="s_x",
            turn=1,
            event="clinical_retrieval",
            detail={
                "chunks": ["pneumonia::management"],
                "query": "Amoxicillin 20-40mg/kg/day",
            },
        )

    assert verify_file(next(directory.glob("audit-*.jsonl"))).ok


def test_the_two_scans_are_independent(tmp_path):
    """Patient data leaking and a role boundary being crossed are different
    failures with different causes, and a record can have one without the
    other."""
    directory = tmp_path / "both"
    writer = AuditWriter(directory=directory)
    with audit_role("patient"):
        writer.append(
            session_id="s_x",
            turn=1,
            event="tool_result",
            detail={"free_text": "call 206-555-0142 about Paracetamol 15mg/kg"},
        )

    kinds = {
        problem.kind for problem in verify_file(next(directory.glob("audit-*.jsonl"))).problems
    }

    assert kinds == {"pii", "cross_role"}


# ------------------------------------------------- what the scan skips, and why ---


def test_a_function_name_containing_dosage_is_not_a_leak(tmp_path):
    """``get_dosage_information`` contains the word "dosage" and no dose. A scan
    that cried wolf on the function name would be turned off within a week."""
    directory = tmp_path / "names"
    writer = AuditWriter(directory=directory)
    with audit_role("patient"):
        writer.append(
            session_id="s_x",
            turn=1,
            event="gate_decision",
            function="get_dosage_information",
            gate={"decision": "deny", "code": "unknown_function"},
        )

    assert verify_file(next(directory.glob("audit-*.jsonl"))).ok


def test_a_denial_naming_a_clinical_function_verifies_clean(run_turn, clinic):
    """The realistic case: a patient session names a clinical function, the gate
    answers unknown_function, and the refusal is logged. That must not read as a
    leak, or every probe would poison the log it is recorded in."""
    session = Session()
    session.existence_checked = True

    path = run_turn(
        session,
        [("get_dosage_information", {"condition_name": "Cystitis", "cohort": "both"})],
    )

    assert verify_file(path).ok


# ----------------------------------------------------- the shared definition ---


def test_the_dose_pattern_has_one_definition():
    """It was copied into three test files before C6. One definition too few for
    something three layers depend on."""
    assert clinical_content("Paracetamol 15mg/kg every 4-6 hours") == "a dose figure"
    assert clinical_content("Amoxicillin (500mg) twice daily") == "a dose figure"
    assert clinical_content("Digoxin 10 mcg/kg/day") == "a dose figure"
    assert clinical_content("ORS 50-100ml/kg over 4 hours") == "a dose figure"
    assert clinical_content("Methotrexate 10-15mg/m2 weekly") == "a dose figure"


def test_ordinary_front_desk_text_is_not_a_dose():
    """The false-positive direction. A scan nobody trusts is a scan nobody
    reads, and this one runs on every record of every eval."""
    for text in (
        "Tuesday, 9:30 AM with Dr. Chen",
        "AP-77301",
        "latency 12170",
        "sore throat and a headache",
        "98101",
        "+12065550142",
    ):
        assert clinical_content(text) is None, text


@pytest.mark.parametrize("marker", CLINICIAN_MARKERS)
def test_every_declared_marker_is_detected(marker):
    assert clinical_content(f"something {marker} something") is not None


# ----------------------------------------------------------- the db mirror ---


def test_the_role_reaches_the_database_mirror(sim, clinic, kb, tmp_path):
    """The mirror is what the staff UI reads. A role missing there would make
    the same record answerable one way through the file and another through the
    database."""
    from app.store.models import AuditMirror

    engine = create_engine(f"sqlite:///{tmp_path / 'mirror.db'}")
    SQLModel.metadata.create_all(engine)
    mirror = AuditMirror(engine=engine)
    writer = AuditWriter(directory=tmp_path / "mirrored")
    session = clinical_session()
    recorder = TurnRecorder(session_id=session.session_id, turn=1, writer=writer, mirror=mirror)

    with (
        session_scope(session, gate=PolicyGate(clinic), audit=recorder),
        registry.backend_scope(sim),
        registry.knowledge_scope(kb),
    ):
        registry.load()["authenticate_clinical_user"].call(dict(CLINICAL_CALLS[0][1]))

    rows = mirror.for_session(session.session_id)

    assert rows
    assert all(json.loads(row.payload)["role"] == "clinical_assistant" for row in rows)
