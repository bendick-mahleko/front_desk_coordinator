"""P2-T1 / P2-T2 / P2-T10 — session state, persistence and the gate decorator."""

from __future__ import annotations

from datetime import date, time

import pytest
from sqlmodel import SQLModel, create_engine

from app.policy.decorator import (
    NoActiveSessionError,
    ToolDenial,
    current_session,
    gated,
    is_gated,
    session_scope,
)
from app.policy.gates import PolicyGate
from app.store.models import SessionStore
from app.store.session import GateLevel, Session, SubjectStatus, slot_time_key


@pytest.fixture
def store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    SQLModel.metadata.create_all(engine)
    return SessionStore(engine=engine)


# --------------------------------------------------------------- session ---


def test_a_new_session_is_anonymous():
    session = Session()

    assert session.status is SubjectStatus.NONE
    assert session.attained_level is GateLevel.OPEN
    assert session.patient_id is None
    assert session.session_id.startswith("s_")


def test_each_session_gets_its_own_salt():
    assert Session().salt != Session().salt


@pytest.mark.parametrize(
    "status,level",
    [
        (SubjectStatus.NONE, GateLevel.OPEN),
        (SubjectStatus.IDENTIFIED, GateLevel.IDENTIFIED),
        (SubjectStatus.VERIFIED, GateLevel.VERIFIED),
        (SubjectStatus.REGISTERED, GateLevel.IDENTIFIED),
        (SubjectStatus.LOCKED, GateLevel.OPEN),
    ],
)
def test_status_maps_to_a_gate_level(status, level):
    session = Session(status=status)
    assert session.attained_level is level


def test_registered_does_not_read_as_verified():
    """Otherwise it would grant demographics over a possible duplicate."""
    session = Session()
    session.mark_registered("PT-4900")

    assert not session.satisfies(GateLevel.VERIFIED)


def test_number_confirmed_is_never_satisfied_by_the_ladder():
    """It is a different axis, answered by its own precondition."""
    session = Session()
    session.mark_verified([])

    assert not session.satisfies(GateLevel.NUMBER_CONFIRMED)


def test_offered_times_round_trip():
    session = Session()
    session.offered_times = {slot_time_key(date(2026, 9, 14), time(10, 0))}

    assert session.was_offered(date(2026, 9, 14), time(10, 0))
    assert not session.was_offered(date(2026, 9, 14), time(10, 30))


# ------------------------------------------------------------ persistence ---


def test_a_session_round_trips(store):
    session = Session()
    session.mark_identified("PT-4101")
    session.mark_verified([])
    session.seen_appointment_ids = {"AP-77301"}
    session.turn_index = 4

    store.save(session)
    loaded = store.load(session.session_id)

    assert loaded is not None
    assert loaded.session_id == session.session_id
    assert loaded.status is SubjectStatus.VERIFIED
    assert loaded.seen_appointment_ids == {"AP-77301"}
    assert loaded.turn_index == 4


def test_saving_twice_updates_rather_than_duplicating(store):
    session = Session()
    store.save(session)
    session.turn_index = 7
    store.save(session)

    assert store.list_ids() == [session.session_id]
    assert store.load(session.session_id).turn_index == 7


def test_an_unknown_session_loads_as_none(store):
    assert store.load("s_nope") is None


def test_a_session_can_be_deleted(store):
    session = Session()
    store.save(session)
    store.delete(session.session_id)

    assert store.load(session.session_id) is None


def test_the_transcript_is_redacted_on_the_way_to_disk(store):
    """spec §4.2 — raw values must not be persisted in conversational logs."""
    session = Session()
    session.transcript = [
        {"role": "user", "content": "My date of birth is 1978-03-04 and my zip is 98101"},
        {"role": "user", "phone_number": "+12065550142"},
    ]
    store.save(session)

    loaded = store.load(session.session_id)
    persisted = loaded.model_dump_json()

    assert "1978-03-04" not in persisted
    assert "98101" not in persisted
    assert "2065550142" not in persisted
    assert "<dob>" in persisted


def test_no_salt_collision_survives_a_round_trip(store):
    session = Session()
    store.save(session)

    assert store.load(session.session_id).salt == session.salt


# -------------------------------------------------------------- decorator ---


def test_a_gated_function_is_marked_as_such():
    @gated("check_business_hours")
    def check_business_hours():
        return {"open": True}

    assert is_gated(check_business_hours)


def test_a_gated_call_outside_a_session_fails_loudly():
    """Defaulting to an empty session would silently grant OPEN access."""

    @gated("check_business_hours")
    def check_business_hours():
        return {"open": True}

    with pytest.raises(NoActiveSessionError):
        check_business_hours()


def test_an_allowed_call_executes(clinic):
    @gated("check_business_hours")
    def check_business_hours():
        return {"open": True}

    with session_scope(Session(), gate=PolicyGate(clinic)):
        assert check_business_hours() == {"open": True}


def test_a_denied_call_does_not_execute(clinic):
    """The denial is a returned result, not an exception — and the body never runs."""
    calls = []

    @gated("get_patient_demographics")
    def get_patient_demographics(patient_id: str, verified: bool = True):
        calls.append(patient_id)
        return {"first_name": "Amara"}

    with session_scope(Session(), gate=PolicyGate(clinic)):
        result = get_patient_demographics(patient_id="PT-4101")

    assert calls == [], "the gate must stop execution, not just annotate it"
    assert result["error"] == "verification_required"
    assert "Amara" not in str(result)


def test_a_denial_carries_a_remedy(clinic):
    @gated("get_patient_appointments")
    def get_patient_appointments(patient_id: str):
        return []

    with session_scope(Session(), gate=PolicyGate(clinic)):
        result = get_patient_appointments(patient_id="PT-4101")

    assert result["remedy"]
    assert result["required_level"] == "verified"
    assert result["current_level"] == "open"


def test_a_successful_result_is_absorbed_into_the_ledger(clinic):
    from app.ports import PatientLookupResult

    @gated("check_patient_exists")
    def check_patient_exists(first_name, last_name, date_of_birth):
        return PatientLookupResult(match_count=1, patient_id="PT-4101")

    session = Session()
    with session_scope(session, gate=PolicyGate(clinic)):
        check_patient_exists(first_name="Amara", last_name="Osei", date_of_birth="1978-03-04")

    assert "PT-4101" in session.seen_patient_ids


def test_the_function_receives_coerced_arguments(clinic):
    seen = {}

    @gated("check_patient_exists")
    def check_patient_exists(first_name, last_name, date_of_birth):
        seen["dob"] = date_of_birth
        return None

    with session_scope(Session(), gate=PolicyGate(clinic)):
        check_patient_exists(first_name="Amara", last_name="Osei", date_of_birth="1978-03-04")

    assert seen["dob"] == date(1978, 3, 4), "the gate coerces; the tool must not re-parse"


def test_the_session_scope_unbinds_on_exit(clinic):
    with session_scope(Session(), gate=PolicyGate(clinic)) as bound:
        assert current_session() is bound

    with pytest.raises(NoActiveSessionError):
        current_session()


def test_every_gate_decision_reaches_the_audit_sink(clinic):
    recorded = []

    class RecordingSink:
        def gate_decision(self, function, verdict, session):
            recorded.append((function, verdict.allowed))

        def tool_result(self, function, result, session):
            return None

    @gated("get_patient_appointments")
    def get_patient_appointments(patient_id: str):
        return []

    with session_scope(Session(), gate=PolicyGate(clinic), audit=RecordingSink()):
        get_patient_appointments(patient_id="PT-4101")

    assert recorded == [("get_patient_appointments", False)], "denials must be audited too"


def test_tool_denial_serialises_for_the_model():
    denial = ToolDenial(
        function="get_patient_demographics",
        code="verification_required",
        message="not available",
        remedy="verify first",
        required="verified",
        actual="open",
    )
    payload = denial.as_tool_result()

    assert payload["error"] == "verification_required"
    assert payload["remedy"] == "verify first"
