"""Phase 1 exit test — every port is callable and returns fixture data."""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from app.ports import (
    Appointment,
    AppointmentStatus,
    BackendError,
    DeliveryStatus,
    EligibilityGateway,
    EligibilityResult,
    EligibilityStatus,
    MessageGateway,
    PatientRepo,
    ScheduleRepo,
    Slot,
    StaffQueue,
)
from app.tools.schemas import (
    AppointmentType,
    EscalationReason,
    IdentifierType,
    MessageType,
    Modality,
    Priority,
)


def test_simulator_satisfies_every_port_protocol(sim):
    """AD-07 — the fakes are substitutable for a real adapter."""
    assert isinstance(sim.patients, PatientRepo)
    assert isinstance(sim.schedule, ScheduleRepo)
    assert isinstance(sim.eligibility, EligibilityGateway)
    assert isinstance(sim.messages, MessageGateway)
    assert isinstance(sim.staff, StaffQueue)


def test_fixture_holds_twenty_four_patients(sim):
    assert len(sim.patients.all_ids()) == 24


# ------------------------------------------------------------- lookup ---


def test_exact_match_returns_one_patient(sim):
    result = sim.patients.check_exists("Amara", "Osei", date(1978, 3, 4))

    assert result.found
    assert result.patient_id == "PT-4101"
    # Minimal disclosure: nothing but a count and an id (spec §4.1).
    assert set(result.model_dump()) == {"match_count", "patient_id"}


def test_lookup_is_case_insensitive(sim):
    assert sim.patients.check_exists("amara", "OSEI", date(1978, 3, 4)).patient_id == "PT-4101"


def test_duplicate_pair_returns_two_matches_and_no_id(sim):
    """PT-4106 / PT-4107 — the same person entered twice (spec §4.1)."""
    result = sim.patients.check_exists("Maria", "Gonzalez", date(1985, 6, 14))

    assert result.ambiguous
    assert result.match_count == 2
    assert result.patient_id is None, "an ambiguous lookup must not select a record"


def test_shared_name_is_disambiguated_by_date_of_birth(sim):
    """PT-4108 / PT-4109 — two different people, one name."""
    older = sim.patients.check_exists("James", "Carter", date(1962, 2, 11))
    younger = sim.patients.check_exists("James", "Carter", date(1990, 9, 3))

    assert older.patient_id == "PT-4108"
    assert younger.patient_id == "PT-4109"
    assert older.patient_id != younger.patient_id


def test_unknown_patient_returns_zero_matches(sim):
    result = sim.patients.check_exists("Nobody", "Here", date(1980, 1, 1))

    assert result.match_count == 0
    assert result.patient_id is None


# -------------------------------------------------------- verification ---


def test_two_correct_identifiers_verify(sim):
    result = sim.patients.verify_identity(
        "PT-4101",
        {IdentifierType.DOB: "1978-03-04", IdentifierType.ADDRESS_ZIP: "98101"},
    )

    assert result.verified
    assert result.methods == (IdentifierType.DOB, IdentifierType.ADDRESS_ZIP)


def test_verification_result_carries_no_identifier_values(sim):
    """spec §4.2 — record the result, timestamp and method. Not the values."""
    result = sim.patients.verify_identity(
        "PT-4101", {IdentifierType.DOB: "1978-03-04", IdentifierType.PHONE: "206-555-0142"}
    )

    serialised = result.model_dump_json()
    assert "1978-03-04" not in serialised
    assert "5550142" not in serialised


def test_a_wrong_identifier_fails_verification(sim):
    result = sim.patients.verify_identity(
        "PT-4101",
        {IdentifierType.DOB: "1978-03-04", IdentifierType.ADDRESS_ZIP: "00000"},
    )
    assert not result.verified


def test_phone_identifier_matches_in_any_format(sim):
    result = sim.patients.verify_identity(
        "PT-4101",
        {IdentifierType.DOB: "1978-03-04", IdentifierType.PHONE: "(206) 555-0142"},
    )
    assert result.verified


# ------------------------------------------------------- demographics ---


def test_demographics_returns_the_full_record(sim):
    record = sim.patients.get_demographics("PT-4101")

    assert record.first_name == "Amara"
    assert record.address_zip == "98101"
    assert record.insurance_plan_name == "BlueRidge PPO"


def test_demographics_for_an_unknown_id_fails_cleanly(sim):
    with pytest.raises(BackendError) as exc:
        sim.patients.get_demographics("PT-0000")
    assert exc.value.code == "not_found"


# ------------------------------------------------------- registration ---


def test_registration_creates_a_record(sim):
    result = sim.patients.create_record(
        "Ada", "Nwosu", date(1990, 1, 1), "+12065550999", email="ada@example.invalid"
    )

    assert result.patient_id.startswith("PT-")
    assert not result.duplicate_suspected
    assert sim.patients.check_exists("Ada", "Nwosu", date(1990, 1, 1)).found


def test_registering_an_existing_patient_flags_a_duplicate(sim):
    """spec §4.4 — escalate instead of creating a second record."""
    result = sim.patients.create_record("Amara", "Osei", date(1978, 3, 4), "+12065550142")

    assert result.duplicate_suspected


def test_registration_is_idempotent(sim):
    first = sim.patients.create_record(
        "Ada", "Nwosu", date(1990, 1, 1), "+12065550999", idempotency_key="k1"
    )
    second = sim.patients.create_record(
        "Ada", "Nwosu", date(1990, 1, 1), "+12065550999", idempotency_key="k1"
    )

    assert first.patient_id == second.patient_id


# ---------------------------------------------------------- scheduling ---


def test_seeded_appointments_load_relative_to_today(sim, today):
    appointments = sim.schedule.get_appointments("PT-4101")

    assert len(appointments) == 2
    assert all(isinstance(item, Appointment) for item in appointments)
    assert appointments[0].appointment_date == today + timedelta(days=6)
    assert appointments[0].appointment_date < appointments[1].appointment_date


def test_search_returns_at_most_the_configured_number_of_slots(sim, today, clinic):
    slots = sim.schedule.search_slots(
        AppointmentType.FOLLOW_UP,
        today,
        today + timedelta(days=14),
        Modality.ANY,
        limit=clinic.policy.max_slots_presented,
    )

    assert 0 < len(slots) <= clinic.policy.max_slots_presented
    assert all(isinstance(slot, Slot) for slot in slots)


def test_search_honours_modality(sim, today):
    slots = sim.schedule.search_slots(
        AppointmentType.FOLLOW_UP, today, today + timedelta(days=20), Modality.TELEHEALTH
    )
    assert slots and all(slot.modality is Modality.TELEHEALTH for slot in slots)


def test_new_patient_visits_are_never_offered_a_telehealth_slot(sim, today):
    slots = sim.schedule.search_slots(
        AppointmentType.NEW_PATIENT, today, today + timedelta(days=20), Modality.ANY
    )
    assert slots and all(slot.modality is Modality.IN_PERSON for slot in slots)


def test_search_honours_provider_and_time_preference(sim, today):
    slots = sim.schedule.search_slots(
        AppointmentType.FOLLOW_UP,
        today,
        today + timedelta(days=25),
        Modality.ANY,
        preferred_provider="Dr. Chen",
        morning_only=True,
    )
    assert slots
    assert all(slot.provider == "Dr. Chen" and slot.is_morning for slot in slots)


def test_search_skips_days_the_clinic_is_closed(sim, today):
    slots = sim.schedule.search_slots(
        AppointmentType.FOLLOW_UP, today, today + timedelta(days=29), Modality.ANY, limit=500
    )
    assert slots
    assert all(slot.slot_date.strftime("%A").lower() != "sunday" for slot in slots)


def test_booking_a_searched_slot_succeeds(sim, today):
    slot = sim.schedule.search_slots(
        AppointmentType.FOLLOW_UP, today, today + timedelta(days=14), Modality.ANY
    )[0]

    appointment = sim.schedule.book(
        "PT-4103", slot.slot_date, slot.slot_time, "Blood pressure review", provider=slot.provider
    )

    assert appointment.appointment_id.startswith("AP-")
    assert appointment.appointment_date == slot.slot_date


def test_booking_a_time_that_was_never_offered_fails(sim, today):
    """spec §4.6 — the assistant may not invent availability."""
    with pytest.raises(BackendError) as exc:
        sim.schedule.book("PT-4103", today + timedelta(days=3), time(3, 15), "Checkup")
    assert exc.value.code == "slot_unavailable"


def test_booking_is_idempotent(sim, today):
    slot = sim.schedule.search_slots(
        AppointmentType.FOLLOW_UP, today, today + timedelta(days=14), Modality.ANY
    )[0]

    first = sim.schedule.book(
        "PT-4103", slot.slot_date, slot.slot_time, "Review", idempotency_key="b1"
    )
    second = sim.schedule.book(
        "PT-4103", slot.slot_date, slot.slot_time, "Review", idempotency_key="b1"
    )

    assert first.appointment_id == second.appointment_id


def test_a_keyed_booking_is_listed_exactly_once(sim, today):
    """Regression: the idempotency cache is keyed by request, not appointment id,
    so merging it into the listing showed every keyed booking twice."""
    slot = sim.schedule.search_slots(
        AppointmentType.FOLLOW_UP, today, today + timedelta(days=14), Modality.ANY
    )[0]
    booked = sim.schedule.book(
        "PT-4103", slot.slot_date, slot.slot_time, "Review", idempotency_key="k-once"
    )

    listed = [a.appointment_id for a in sim.schedule.get_appointments("PT-4103")]
    assert listed.count(booked.appointment_id) == 1


def test_cancellation_marks_the_appointment_and_reports_lateness(sim):
    result = sim.schedule.cancel("PT-4101", "AP-77301", "Schedule conflict")

    assert result.appointment_id == "AP-77301"
    assert result.late_cancellation is False
    remaining = sim.schedule.get_appointments("PT-4101")
    cancelled = [a for a in remaining if a.appointment_id == "AP-77301"]
    assert cancelled[0].status is AppointmentStatus.CANCELLED


def test_cancelling_someone_elses_appointment_is_refused(sim):
    """The error must not reveal that the ID exists for another patient."""
    with pytest.raises(BackendError) as exc:
        sim.schedule.cancel("PT-4103", "AP-77301", "Wrong patient")
    assert exc.value.code == "appointment_not_found"


def test_reschedule_moves_one_appointment_without_a_separate_cancel(sim, today):
    """spec §4.8 — never cancel the old appointment separately."""
    slot = sim.schedule.search_slots(
        AppointmentType.FOLLOW_UP,
        today + timedelta(days=10),
        today + timedelta(days=20),
        Modality.IN_PERSON,
    )[0]

    moved = sim.schedule.reschedule("PT-4101", "AP-77301", slot.slot_id, "Work conflict")

    assert moved.appointment_id == "AP-77301"
    assert moved.appointment_date == slot.slot_date
    assert moved.status is AppointmentStatus.SCHEDULED


# ----------------------------------------------------------- eligibility ---


def test_eligibility_returns_a_determination(sim, today):
    result = sim.eligibility.check("PT-4101", today)

    assert isinstance(result, EligibilityResult)
    assert result.status is EligibilityStatus.ACTIVE
    assert result.payer == "BlueRidge Health Services"


def test_eligibility_never_returns_copay_data(sim, today):
    """P1-T7 / design §13 — the omission is the fixture, not an oversight."""
    result = sim.eligibility.check("PT-4101", today)

    fields = set(result.model_dump())
    assert "copay" not in fields
    assert not any("copay" in name or "deductible" in name for name in fields)


def test_patient_without_a_plan_is_indeterminate(sim, today):
    result = sim.eligibility.check("PT-4120", today)
    assert result.status is EligibilityStatus.INDETERMINATE


def test_inactive_coverage_is_reported_as_such(sim, today):
    assert sim.eligibility.check("PT-4115", today).status is EligibilityStatus.INACTIVE


# ------------------------------------------------------------- messaging ---


def test_sending_appends_to_the_outbox(sim):
    receipt = sim.messages.send("+12065550142", MessageType.DIRECTIONS)

    assert receipt.delivery_status is DeliveryStatus.DELIVERED
    assert receipt.confirmed
    assert len(sim.messages.outbox()) == 1


def test_text_delivery_is_idempotent(sim):
    first = sim.messages.send("+12065550142", MessageType.INTAKE_FORMS, idempotency_key="m1")
    second = sim.messages.send("+12065550142", MessageType.INTAKE_FORMS, idempotency_key="m1")

    assert first.message_id == second.message_id
    assert len(sim.messages.outbox()) == 1


# ------------------------------------------------------------ escalation ---


def test_escalation_creates_a_ticket(sim):
    ticket = sim.staff.escalate(
        EscalationReason.BILLING_ISSUE,
        Priority.ROUTINE,
        "Patient asked about copay; eligibility does not provide it.",
        patient_id="PT-4101",
    )

    assert ticket.ticket_id.startswith("ESC-")
    assert ticket.patient_id == "PT-4101"
    assert len(sim.staff.tickets()) == 1


def test_escalation_omits_patient_id_when_not_known(sim):
    """spec §4.12 — include it only when available and appropriate."""
    ticket = sim.staff.escalate(EscalationReason.OTHER, Priority.ROUTINE, "Caller wants a person.")
    assert ticket.patient_id is None
