"""P2-T13 — the provenance ledger."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from app.policy import provenance
from app.policy.messages import Remedy
from app.ports import (
    Appointment,
    AppointmentStatus,
    CancellationResult,
    PatientLookupResult,
    RegistrationResult,
    Slot,
)
from app.store.session import Session, slot_time_key
from app.tools.schemas import AppointmentType, Modality


def an_appointment(appointment_id: str = "AP-77301") -> Appointment:
    return Appointment(
        appointment_id=appointment_id,
        patient_id="PT-4101",
        appointment_date=date(2026, 9, 13),
        appointment_time=time(9, 30),
        provider="Dr. Alvarez",
        appointment_type=AppointmentType.FOLLOW_UP,
        modality=Modality.IN_PERSON,
        reason_for_visit="Blood pressure review",
    )


def a_slot(slot_id: str = "SL-2026-09-14-0-1") -> Slot:
    return Slot(
        slot_id=slot_id,
        slot_date=date(2026, 9, 14),
        slot_time=time(10, 0),
        provider="Dr. Chen",
        modality=Modality.IN_PERSON,
    )


# ----------------------------------------------------------------- check ---


def test_an_unseen_identifier_is_refused():
    """spec §6 — a well-formed id is not the same as a real one."""
    result = provenance.check({"patient_id": "PT-40921"}, Session())

    assert result is not None
    argument, remedy = result
    assert argument == "patient_id"
    assert remedy is Remedy.IDENTIFY_FIRST


def test_a_seen_identifier_passes():
    session = Session()
    session.seen_patient_ids = {"PT-4101"}

    assert provenance.check({"patient_id": "PT-4101"}, session) is None


def test_absent_optional_identifiers_are_not_checked():
    """escalate_to_staff may omit patient_id entirely (spec §4.12)."""
    assert provenance.check({"patient_id": None, "reason": "other"}, Session()) is None


def test_each_identifier_kind_has_its_own_remedy():
    session = Session()

    assert provenance.check({"appointment_id": "AP-1"}, session)[1] is Remedy.LOOK_UP_APPOINTMENTS
    assert (
        provenance.check({"current_appointment_id": "AP-1"}, session)[1]
        is Remedy.LOOK_UP_APPOINTMENTS
    )
    assert (
        provenance.check({"new_appointment_slot_id": "SL-1"}, session)[1]
        is Remedy.SEARCH_SLOTS_FIRST
    )


def test_the_ledger_is_scoped_to_one_session():
    first = Session()
    first.seen_patient_ids = {"PT-4101"}
    second = Session()

    assert provenance.check({"patient_id": "PT-4101"}, first) is None
    assert provenance.check({"patient_id": "PT-4101"}, second) is not None


# ---------------------------------------------------------------- absorb ---


def test_a_lookup_result_contributes_its_patient_id():
    session = Session()
    provenance.absorb(PatientLookupResult(match_count=1, patient_id="PT-4101"), session)

    assert "PT-4101" in session.seen_patient_ids


def test_an_ambiguous_lookup_contributes_nothing():
    session = Session()
    provenance.absorb(PatientLookupResult(match_count=2), session)

    assert session.seen_patient_ids == set()


def test_a_list_of_appointments_is_absorbed():
    session = Session()
    provenance.absorb([an_appointment("AP-1"), an_appointment("AP-2")], session)

    assert session.seen_appointment_ids == {"AP-1", "AP-2"}
    assert session.seen_patient_ids == {"PT-4101"}


def test_slots_contribute_both_an_id_and_an_offered_time():
    """book_appointment takes a date and a time, not a slot id — so both shapes
    have to be remembered or booking could not be provenance-checked."""
    session = Session()
    provenance.absorb([a_slot()], session)

    assert "SL-2026-09-14-0-1" in session.seen_slot_ids
    assert session.was_offered(date(2026, 9, 14), time(10, 0))
    assert slot_time_key(date(2026, 9, 14), time(10, 0)) in session.offered_times


def test_a_booked_appointment_fixes_a_service_date():
    """spec §4.9 — eligibility may ask about a date the patient actually has."""
    session = Session()
    provenance.absorb(an_appointment(), session)

    assert date(2026, 9, 13) in session.booked_service_dates


def test_a_cancelled_appointment_does_not_fix_a_service_date():
    session = Session()
    cancelled = an_appointment().model_copy(update={"status": AppointmentStatus.CANCELLED})
    provenance.absorb(cancelled, session)

    assert session.booked_service_dates == set()
    assert "AP-77301" in session.seen_appointment_ids


def test_registration_contributes_the_new_patient_id():
    session = Session()
    provenance.absorb(
        RegistrationResult(patient_id="PT-4900", created_at=datetime.now(UTC)), session
    )

    assert "PT-4900" in session.seen_patient_ids


def test_a_cancellation_result_is_absorbed():
    session = Session()
    provenance.absorb(
        CancellationResult(
            appointment_id="AP-77301", cancelled_at=datetime.now(UTC), late_cancellation=False
        ),
        session,
    )

    assert "AP-77301" in session.seen_appointment_ids


def test_absorbing_a_non_model_result_is_harmless():
    session = Session()
    provenance.absorb({"error": "slot_unavailable"}, session)
    provenance.absorb(None, session)
    provenance.absorb("a string", session)

    assert session.seen_patient_ids == set()


def test_absorption_accumulates_across_calls():
    session = Session()
    provenance.absorb(a_slot("SL-A"), session)
    provenance.absorb(a_slot("SL-B"), session)

    assert session.seen_slot_ids == {"SL-A", "SL-B"}


# ------------------------------------------------- the three spec rules ---


def test_booking_a_slot_that_was_offered_becomes_possible():
    session = Session()
    assert not session.was_offered(date(2026, 9, 14), time(10, 0))

    provenance.absorb([a_slot()], session)

    assert session.was_offered(date(2026, 9, 14), time(10, 0))
    assert not session.was_offered(date(2026, 9, 14), time(11, 0))


def test_cancelling_needs_the_lookup_first():
    session = Session()
    assert provenance.check({"appointment_id": "AP-77301"}, session) is not None

    provenance.absorb([an_appointment()], session)

    assert provenance.check({"appointment_id": "AP-77301"}, session) is None
