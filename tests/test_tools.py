"""Phase 3 exit test — the whole non-AI path, end to end.

The booking flow of specification §5 driven directly against the tool layer,
with the policy gate live and no model anywhere. If this passes, everything
Phase 4 adds is conversation.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.policy.decorator import session_scope
from app.policy.gates import PolicyGate
from app.store.session import Session, SubjectStatus
from app.tools import registry


@pytest.fixture
def tools():
    return registry.load()


@pytest.fixture
def session():
    return Session()


@pytest.fixture
def running(sim, clinic, session):
    """A bound turn: session, gate and clinic backends."""
    with session_scope(session, gate=PolicyGate(clinic)), registry.backend_scope(sim):
        yield session


def call(tools, name, **kwargs):
    """A tool returns the JSON string that goes into the tool_result block."""
    return json.loads(tools[name].call(kwargs))


# ------------------------------------------------------ the booking flow ---


def test_an_existing_patient_books_an_appointment(tools, running, sim, today):
    """spec §5 — lookup, verify, search, book. The Phase 3 exit test."""
    session = running

    # 1 — lookup
    found = call(
        tools,
        "check_patient_exists",
        first_name="Amara",
        last_name="Osei",
        date_of_birth="1978-03-04",
    )
    assert found["match_count"] == 1
    assert found["patient_id"] == "PT-4101"
    assert session.status is SubjectStatus.IDENTIFIED

    # 2 — verification
    verified = call(
        tools,
        "verify_patient_identity",
        patient_id="PT-4101",
        identifier_1_type="dob",
        identifier_1_value="1978-03-04",
        identifier_2_type="address_zip",
        identifier_2_value="98101",
    )
    assert verified["verified"] is True
    assert session.status is SubjectStatus.VERIFIED

    # 3 — search
    found_slots = call(
        tools,
        "search_available_appointments",
        appointment_type="follow_up",
        date_range_start=today.isoformat(),
        date_range_end=(today + timedelta(days=14)).isoformat(),
        modality="in_person",
    )
    slots = found_slots["slots"]
    assert 0 < len(slots) <= 3, "spec §4.5 — a limited number of choices"

    # 4 — book the slot the patient picked
    chosen = slots[0]
    booked = call(
        tools,
        "book_appointment",
        appointment_date=chosen["slot_date"],
        appointment_time=chosen["slot_time"],
        reason_for_visit="Blood pressure review",
        patient_id="PT-4101",
        provider=chosen["provider"],
        appointment_type="follow_up",
        modality="in_person",
        send_reminder=True,
    )
    assert booked["booked"] is True
    assert booked["appointment"]["appointment_date"] == chosen["slot_date"]

    # The booking is real in the backend, not just in the reply.
    ids = [a.appointment_id for a in sim.schedule.get_appointments("PT-4101")]
    assert booked["appointment"]["appointment_id"] in ids


def test_the_same_flow_is_refused_when_taken_out_of_order(tools, running):
    """Each step of §5 is enforced, not merely suggested."""
    session = running

    early = call(tools, "get_patient_appointments", patient_id="PT-4101")
    assert early["error"] == "verification_required"

    call(
        tools,
        "check_patient_exists",
        first_name="Amara",
        last_name="Osei",
        date_of_birth="1978-03-04",
    )

    still_early = call(tools, "get_patient_appointments", patient_id="PT-4101")
    assert still_early["error"] == "verification_required"
    assert session.status is SubjectStatus.IDENTIFIED


def test_booking_a_time_that_was_never_offered_is_refused(tools, running, today):
    call(
        tools,
        "check_patient_exists",
        first_name="Amara",
        last_name="Osei",
        date_of_birth="1978-03-04",
    )
    call(
        tools,
        "verify_patient_identity",
        patient_id="PT-4101",
        identifier_1_type="dob",
        identifier_1_value="1978-03-04",
        identifier_2_type="address_zip",
        identifier_2_value="98101",
    )

    invented = call(
        tools,
        "book_appointment",
        appointment_date=(today + timedelta(days=3)).isoformat(),
        appointment_time="03:15",
        reason_for_visit="Checkup",
        patient_id="PT-4101",
    )
    assert invented["error"] == "precondition_failed"
    assert "search_available_appointments" in invented["remedy"]


# ------------------------------------------------------------ new patient ---


def test_a_new_patient_registers_then_books(tools, running, today):
    """spec §5 — check, register, search, book. No verification step."""
    session = running

    missing = call(
        tools,
        "check_patient_exists",
        first_name="Ada",
        last_name="Nwosu",
        date_of_birth="1990-01-01",
    )
    assert missing["match_count"] == 0

    registered = call(
        tools,
        "create_new_patient_record",
        first_name="Ada",
        last_name="Nwosu",
        date_of_birth="1990-01-01",
        phone_number="206-555-0999",
        insurance_plan_name="BlueRidge PPO",
    )
    assert registered["registered"] is True
    assert session.status is SubjectStatus.REGISTERED

    slots = call(
        tools,
        "search_available_appointments",
        appointment_type="new_patient",
        date_range_start=today.isoformat(),
        date_range_end=(today + timedelta(days=20)).isoformat(),
        modality="in_person",
    )["slots"]

    booked = call(
        tools,
        "book_appointment",
        appointment_date=slots[0]["slot_date"],
        appointment_time=slots[0]["slot_time"],
        reason_for_visit="New patient visit",
        patient_id=registered["patient_id"],
        appointment_type="new_patient",
    )
    assert booked["booked"] is True


def test_a_new_patient_is_not_offered_a_follow_up(tools, running, session, today):
    """Reported from a live session: registered as a new patient, asked for an
    appointment, got "Follow-up Visit".

    Through the whole stack rather than the gate alone, because what matters is
    that the model receives a denial it can act on rather than an exception.
    """
    call(
        tools,
        "check_patient_exists",
        first_name="Ada",
        last_name="Nwosu",
        date_of_birth="1990-01-01",
    )
    call(
        tools,
        "create_new_patient_record",
        first_name="Ada",
        last_name="Nwosu",
        date_of_birth="1990-01-01",
        phone_number="206-555-0999",
    )

    refused = call(
        tools,
        "search_available_appointments",
        appointment_type="follow_up",
        date_range_start=today.isoformat(),
        date_range_end=(today + timedelta(days=20)).isoformat(),
        modality="any",
    )

    assert refused["error"] == "precondition_failed"
    assert "new_patient" in refused["remedy"]


def test_registration_says_what_the_first_visit_is(tools, running):
    """Read immediately before the model composes its reply, so the correction
    arrives before the wrong visit type is proposed rather than after."""
    call(
        tools,
        "check_patient_exists",
        first_name="Ada",
        last_name="Nwosu",
        date_of_birth="1990-01-01",
    )
    registered = call(
        tools,
        "create_new_patient_record",
        first_name="Ada",
        last_name="Nwosu",
        date_of_birth="1990-01-01",
        phone_number="206-555-0999",
    )

    assert "new_patient" in registered["next_step"]
    assert "follow_up" in registered["next_step"]


def test_registering_an_existing_patient_escalates_instead(tools, running):
    """spec §4.4 — escalate instead of creating a duplicate record."""
    call(
        tools,
        "check_patient_exists",
        first_name="Amara",
        last_name="Osei",
        date_of_birth="1978-03-04",
    )

    attempted = call(
        tools,
        "create_new_patient_record",
        first_name="Amara",
        last_name="Osei",
        date_of_birth="1978-03-04",
        phone_number="206-555-0142",
    )
    assert attempted["error"] == "duplicate_suspected"
    assert "escalate_to_staff" in attempted["remedy"]


def test_registration_without_a_lookup_is_refused(tools, running):
    attempted = call(
        tools,
        "create_new_patient_record",
        first_name="Ada",
        last_name="Nwosu",
        date_of_birth="1990-01-01",
        phone_number="206-555-0999",
    )
    assert attempted["error"] == "precondition_failed"


# ---------------------------------------------------------- verification ---


def test_a_failed_verification_does_not_say_which_identifier_was_wrong(tools, running):
    """spec §3 rule 5 — naming the wrong one confirms the other."""
    call(
        tools,
        "check_patient_exists",
        first_name="Amara",
        last_name="Osei",
        date_of_birth="1978-03-04",
    )

    failed = call(
        tools,
        "verify_patient_identity",
        patient_id="PT-4101",
        identifier_1_type="dob",
        identifier_1_value="1978-03-04",
        identifier_2_type="address_zip",
        identifier_2_value="00000",
    )

    assert failed["verified"] is False
    blob = str(failed).lower()
    assert "dob" not in blob.replace("untried_combinations", "").replace("dob+phone", "")
    assert "1978-03-04" not in str(failed)
    assert failed["attempts_remaining"] == 2


def test_exhausting_the_attempts_locks_and_directs_to_staff(tools, running):
    session = running
    call(
        tools,
        "check_patient_exists",
        first_name="Amara",
        last_name="Osei",
        date_of_birth="1978-03-04",
    )

    pairs = [("dob", "address_zip"), ("dob", "phone"), ("phone", "address_zip")]
    for first, second in pairs:
        result = call(
            tools,
            "verify_patient_identity",
            patient_id="PT-4101",
            identifier_1_type=first,
            identifier_1_value="wrong-1",
            identifier_2_type=second,
            identifier_2_value="wrong-2",
        )

    assert result["locked"] is True
    assert session.status is SubjectStatus.LOCKED
    assert "escalate_to_staff" in result["next_step"]

    # And escalation still works, because it always must.
    escalated = call(
        tools,
        "escalate_to_staff",
        reason="other",
        priority="routine",
        notes="Could not verify identity over chat.",
    )
    assert escalated["escalated"] is True


def test_verification_confirms_the_phone_on_file_for_texting(tools, running):
    session = running
    call(
        tools,
        "check_patient_exists",
        first_name="Amara",
        last_name="Osei",
        date_of_birth="1978-03-04",
    )
    call(
        tools,
        "verify_patient_identity",
        patient_id="PT-4101",
        identifier_1_type="dob",
        identifier_1_value="1978-03-04",
        identifier_2_type="address_zip",
        identifier_2_value="98101",
    )

    assert session.confirmed_phone == "+12065550142"


# -------------------------------------------------------- demographics ---


def test_demographics_ignores_a_verified_flag_from_the_model(tools, running):
    """P3-T5 — the flag is asserted from session state, never trusted.

    A tool that believed an argument named `verified` would be one prompt
    injection away from disclosure.
    """
    denied = call(tools, "get_patient_demographics", patient_id="PT-4101", verified=True)

    assert denied["error"] == "verification_required"
    assert "Amara" not in str(denied)


# --------------------------------------------------------------- errors ---


def test_a_backend_failure_becomes_a_result_not_an_exception(tools, running, sim):
    """P3-T4 — an exception would end the turn; a result lets the assistant explain."""
    sim.faults.arm("PatientRepo", "check_exists", "upstream_timeout")

    result = call(
        tools,
        "check_patient_exists",
        first_name="Amara",
        last_name="Osei",
        date_of_birth="1978-03-04",
    )

    assert result["error"] == "upstream_timeout"
    assert "try again" in result["remedy"] or "call them back" in result["remedy"]


def test_a_taken_slot_sends_the_assistant_back_to_search(tools, running, sim, today):
    """spec §4.6 — explain and return to search; never claim it was booked."""
    call(
        tools,
        "check_patient_exists",
        first_name="Amara",
        last_name="Osei",
        date_of_birth="1978-03-04",
    )
    call(
        tools,
        "verify_patient_identity",
        patient_id="PT-4101",
        identifier_1_type="dob",
        identifier_1_value="1978-03-04",
        identifier_2_type="address_zip",
        identifier_2_value="98101",
    )
    slots = call(
        tools,
        "search_available_appointments",
        appointment_type="follow_up",
        date_range_start=today.isoformat(),
        date_range_end=(today + timedelta(days=14)).isoformat(),
        modality="any",
    )["slots"]

    sim.faults.arm("ScheduleRepo", "book", "slot_unavailable")
    result = call(
        tools,
        "book_appointment",
        appointment_date=slots[0]["slot_date"],
        appointment_time=slots[0]["slot_time"],
        reason_for_visit="Review",
        patient_id="PT-4101",
    )

    assert result["error"] == "slot_unavailable"
    assert "booked" not in result
    assert "search_available_appointments" in result["remedy"]


# ---------------------------------------------------------- eligibility ---


def test_eligibility_states_the_disclaimer_and_declares_no_copay(tools, running, sim, today):
    """spec §4.9."""
    call(
        tools,
        "check_patient_exists",
        first_name="Amara",
        last_name="Osei",
        date_of_birth="1978-03-04",
    )
    call(
        tools,
        "verify_patient_identity",
        patient_id="PT-4101",
        identifier_1_type="dob",
        identifier_1_value="1978-03-04",
        identifier_2_type="address_zip",
        identifier_2_value="98101",
    )
    appointments = call(tools, "get_patient_appointments", patient_id="PT-4101")

    result = call(
        tools,
        "check_insurance_eligibility",
        patient_id="PT-4101",
        service_date=appointments[0]["appointment_date"],
    )

    assert result["status"] == "active"
    assert "not a guarantee" in result["disclaimer"]
    assert result["copay_available"] is False


def test_an_unconfirmed_service_date_is_refused(tools, running, today):
    call(
        tools,
        "check_patient_exists",
        first_name="Amara",
        last_name="Osei",
        date_of_birth="1978-03-04",
    )
    call(
        tools,
        "verify_patient_identity",
        patient_id="PT-4101",
        identifier_1_type="dob",
        identifier_1_value="1978-03-04",
        identifier_2_type="address_zip",
        identifier_2_value="98101",
    )

    result = call(
        tools,
        "check_insurance_eligibility",
        patient_id="PT-4101",
        service_date=(today + timedelta(days=99)).isoformat(),
    )

    assert result["error"] == "precondition_failed"


# ------------------------------------------------------------- messaging ---


def test_directions_go_to_a_confirmed_number_without_verification(tools, running, session):
    """spec §4.10 — directions carry no health information."""
    session.confirm_phone("+12065550142")

    sent = call(tools, "send_secure_text", phone_number="206-555-0142", message_type="directions")

    assert sent["delivery_status"] == "delivered"


def test_a_telehealth_link_needs_verification(tools, running, session):
    session.confirm_phone("+12065550142")

    denied = call(
        tools, "send_secure_text", phone_number="206-555-0142", message_type="telehealth_link"
    )

    assert denied["error"] == "verification_required"


def test_unconfirmed_delivery_is_reported(tools, running, session, sim):
    """spec §4.10 — tell the patient when delivery cannot be confirmed."""
    session.confirm_phone("+12065550142")
    sim.faults.arm("MessageGateway", "send", "delivery_unconfirmed")

    sent = call(tools, "send_secure_text", phone_number="206-555-0142", message_type="directions")

    assert sent["delivery_status"] == "unconfirmed"
    assert "could not be confirmed" in sent["next_step"]


def test_text_delivery_is_idempotent_within_a_session(tools, running, session, sim):
    """spec §6 — a retry must not send twice."""
    session.confirm_phone("+12065550142")

    first = call(tools, "send_secure_text", phone_number="206-555-0142", message_type="directions")
    second = call(tools, "send_secure_text", phone_number="206-555-0142", message_type="directions")

    assert first["message_id"] == second["message_id"]
    assert len(sim.messages.outbox()) == 1


# ----------------------------------------------------------- clinic info ---


def test_clinic_information_needs_no_identity(tools, running):
    friday = call(tools, "get_clinic_hours", date="2026-09-11")
    assert friday["open"] is True
    assert friday["opens"] == "08:00"

    saturday = call(tools, "get_clinic_hours", date="2026-09-12")
    assert saturday["open"] is True
    assert saturday["opens"] == "09:00", "weekend hours differ from weekdays"

    sunday = call(tools, "get_clinic_hours", date="2026-09-13")
    assert sunday["open"] is False

    holiday = call(tools, "get_clinic_hours", date="2026-12-25")
    assert holiday["reason"] == "holiday"

    now = call(tools, "check_business_hours")
    assert "open_now" in now

    directions = call(tools, "get_clinic_directions", location="main_clinic")
    assert directions["address"] == "1420 Cedar Street, Riverbend"
    assert "parking" in directions


def test_an_invalid_location_is_refused(tools, running):
    result = call(tools, "get_clinic_directions", location="north_satellite_office")
    assert result["error"] == "invalid_arguments"


# ------------------------------------------------------------ escalation ---


def test_escalation_strips_values_that_drifted_into_the_notes(tools, running, sim):
    call(
        tools,
        "escalate_to_staff",
        reason="billing_issue",
        priority="routine",
        notes="Patient asked about copay. Reachable on 206-555-0142.",
    )

    ticket = sim.staff.tickets()[0]
    assert "2065550142" not in ticket.notes.replace("-", "")
    assert "copay" in ticket.notes, "the note must still be readable by staff"


def test_an_emergency_escalation_tells_the_patient_to_call_for_help(tools, running):
    result = call(
        tools,
        "escalate_to_staff",
        reason="complex_symptoms",
        priority="emergency",
        notes="Chest pain reported.",
    )

    assert "emergency" in result["next_step"].lower()
    assert result["priority"] == "emergency"


def test_escalation_attaches_the_patient_id_only_when_known(tools, running, sim):
    call(tools, "escalate_to_staff", reason="other", priority="routine", notes="Wants a person.")
    assert sim.staff.tickets()[0].patient_id is None

    call(
        tools,
        "check_patient_exists",
        first_name="Amara",
        last_name="Osei",
        date_of_birth="1978-03-04",
    )
    call(tools, "escalate_to_staff", reason="other", priority="routine", notes="Wants a person.")
    assert sim.staff.tickets()[1].patient_id == "PT-4101"
