"""Phase 1 exit test — fault injection produces every failure mode in design §13.

The point of this file is coverage: if a failure mode listed in the design
cannot actually be produced, an error-handling requirement in specification §6
is untestable, and we would not find that out until Phase 8.
"""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from app.clinic_sim.faults import (
    ERROR_FAULTS,
    SUPPORTED_FAULTS,
    UnknownFaultError,
    all_fault_codes,
)
from app.ports import BackendError, DeliveryStatus, EligibilityStatus
from app.tools.schemas import (
    AppointmentType,
    EscalationReason,
    IdentifierType,
    MessageType,
    Modality,
    Priority,
)

DESIGN_SECTION_13 = {
    "PatientRepo": {"multiple_match", "not_found", "upstream_timeout"},
    "ScheduleRepo": {"slot_unavailable", "double_booking", "appointment_not_found"},
    "EligibilityGateway": {"payer_unavailable", "ambiguous_response", "rejected"},
    "MessageGateway": {"delivery_unconfirmed", "invalid_number", "send_failed"},
    "StaffQueue": set(),
}


def test_catalogue_matches_the_design():
    assert {port: set(codes) for port, codes in SUPPORTED_FAULTS.items()} == DESIGN_SECTION_13


def test_escalation_cannot_be_made_to_fail(sim):
    """spec §4.12 — a fallback that can itself fail is not a fallback."""
    with pytest.raises(UnknownFaultError, match="may not fail"):
        sim.faults.arm("StaffQueue", "escalate", "upstream_timeout")

    ticket = sim.staff.escalate(EscalationReason.OTHER, Priority.URGENT, "Wants a person.")
    assert ticket.ticket_id


def test_arming_an_unsupported_fault_fails_loudly(sim):
    """A typo in a scenario must fail the test, not silently arm nothing."""
    with pytest.raises(UnknownFaultError, match="cannot produce"):
        sim.faults.arm("PatientRepo", "check_exists", "slot_unavailable")
    with pytest.raises(UnknownFaultError, match="unknown port"):
        sim.faults.arm("BillingRepo", "check", "rejected")


# ---------------------------------------------------------- error faults ---


def test_patient_repo_upstream_timeout(sim):
    sim.faults.arm("PatientRepo", "check_exists", "upstream_timeout")

    with pytest.raises(BackendError) as exc:
        sim.patients.check_exists("Amara", "Osei", date(1978, 3, 4))
    assert exc.value.code == "upstream_timeout"


def test_schedule_repo_slot_unavailable(sim, today):
    sim.faults.arm("ScheduleRepo", "book", "slot_unavailable")

    with pytest.raises(BackendError) as exc:
        sim.schedule.book("PT-4103", today + timedelta(days=3), time(9, 0), "x")
    assert exc.value.code == "slot_unavailable"


def test_schedule_repo_double_booking(sim, today):
    sim.faults.arm("ScheduleRepo", "book", "double_booking")

    with pytest.raises(BackendError) as exc:
        sim.schedule.book("PT-4103", today + timedelta(days=3), time(9, 0), "x")
    assert exc.value.code == "double_booking"


def test_schedule_repo_appointment_not_found(sim):
    sim.faults.arm("ScheduleRepo", "cancel", "appointment_not_found")

    with pytest.raises(BackendError) as exc:
        sim.schedule.cancel("PT-4101", "AP-77301", "reason")
    assert exc.value.code == "appointment_not_found"


def test_eligibility_payer_unavailable(sim, today):
    sim.faults.arm("EligibilityGateway", "check", "payer_unavailable")

    with pytest.raises(BackendError) as exc:
        sim.eligibility.check("PT-4101", today)
    assert exc.value.code == "payer_unavailable"


def test_eligibility_rejected(sim, today):
    sim.faults.arm("EligibilityGateway", "check", "rejected")

    with pytest.raises(BackendError) as exc:
        sim.eligibility.check("PT-4101", today)
    assert exc.value.code == "rejected"


def test_message_gateway_invalid_number(sim):
    sim.faults.arm("MessageGateway", "send", "invalid_number")

    with pytest.raises(BackendError) as exc:
        sim.messages.send("+12065550142", MessageType.DIRECTIONS)
    assert exc.value.code == "invalid_number"


def test_message_gateway_send_failed(sim):
    sim.faults.arm("MessageGateway", "send", "send_failed")

    with pytest.raises(BackendError) as exc:
        sim.messages.send("+12065550142", MessageType.DIRECTIONS)
    assert exc.value.code == "send_failed"


# -------------------------------------------------------- outcome faults ---


def test_multiple_match_is_an_outcome_not_an_error(sim):
    """Two matches is a valid answer the assistant must handle (spec §4.1)."""
    sim.faults.arm("PatientRepo", "check_exists", "multiple_match")

    result = sim.patients.check_exists("Amara", "Osei", date(1978, 3, 4))
    assert result.ambiguous
    assert result.patient_id is None


def test_not_found_is_an_outcome_not_an_error(sim):
    sim.faults.arm("PatientRepo", "check_exists", "not_found")

    result = sim.patients.check_exists("Amara", "Osei", date(1978, 3, 4))
    assert result.match_count == 0


def test_ambiguous_eligibility_returns_indeterminate(sim, today):
    """spec §4.9 — escalate for manual review rather than guessing."""
    sim.faults.arm("EligibilityGateway", "check", "ambiguous_response")

    result = sim.eligibility.check("PT-4101", today)
    assert result.status is EligibilityStatus.INDETERMINATE


def test_delivery_unconfirmed_is_reported_on_the_receipt(sim):
    """spec §4.10 — inform the patient when delivery cannot be confirmed."""
    sim.faults.arm("MessageGateway", "send", "delivery_unconfirmed")

    receipt = sim.messages.send("+12065550142", MessageType.TELEHEALTH_LINK)
    assert receipt.delivery_status is DeliveryStatus.UNCONFIRMED
    assert not receipt.confirmed


# ------------------------------------------------------------- mechanics ---


def test_once_faults_fire_exactly_once(sim):
    sim.faults.arm("PatientRepo", "check_exists", "upstream_timeout", once=True)

    with pytest.raises(BackendError):
        sim.patients.check_exists("Amara", "Osei", date(1978, 3, 4))

    # The retry the assistant offers after a failure must be able to succeed.
    assert sim.patients.check_exists("Amara", "Osei", date(1978, 3, 4)).found


def test_persistent_faults_keep_firing(sim):
    sim.faults.arm("PatientRepo", "check_exists", "upstream_timeout", once=False)

    for _ in range(3):
        with pytest.raises(BackendError):
            sim.patients.check_exists("Amara", "Osei", date(1978, 3, 4))


def test_faults_are_scoped_to_one_operation(sim, today):
    sim.faults.arm("ScheduleRepo", "book", "slot_unavailable")

    # A fault on book must not affect search.
    assert sim.schedule.search_slots(
        AppointmentType.FOLLOW_UP, today, today + timedelta(days=14), Modality.ANY
    )


def test_armed_with_clears_on_exit(sim):
    with sim.faults.armed_with("PatientRepo", "check_exists", "not_found"):
        assert sim.patients.check_exists("Amara", "Osei", date(1978, 3, 4)).match_count == 0

    assert sim.faults.armed == {}
    assert sim.patients.check_exists("Amara", "Osei", date(1978, 3, 4)).found


def test_every_declared_fault_code_is_exercised_somewhere():
    """Guards against a code being declared but never producible."""
    exercised = {
        "multiple_match",
        "not_found",
        "upstream_timeout",
        "slot_unavailable",
        "double_booking",
        "appointment_not_found",
        "payer_unavailable",
        "ambiguous_response",
        "rejected",
        "delivery_unconfirmed",
        "invalid_number",
        "send_failed",
    }
    assert all_fault_codes() == exercised
    assert all_fault_codes() >= ERROR_FAULTS
    assert not (
        ERROR_FAULTS & {"multiple_match", "not_found", "ambiguous_response", "delivery_unconfirmed"}
    )


def test_verification_identity_is_not_a_fault_surface(sim):
    """Fixture-driven mismatch, not an injected fault — a wrong answer is data."""
    result = sim.patients.verify_identity(
        "PT-4101", {IdentifierType.DOB: "1900-01-01", IdentifierType.ADDRESS_ZIP: "00000"}
    )
    assert not result.verified
