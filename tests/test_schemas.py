"""P1-T9 — argument models and enumerations.

Phase 1's exit test. No model, no network: these are pure contract checks.
"""

from __future__ import annotations

from datetime import date, time

import pytest
from pydantic import ValidationError

from app.tools import schemas as S

# ------------------------------------------------------------- inventory ---


SPEC_FUNCTIONS = frozenset(
    {
        "check_patient_exists",
        "verify_patient_identity",
        "get_patient_demographics",
        "get_patient_appointments",
        "create_new_patient_record",
        "search_available_appointments",
        "book_appointment",
        "cancel_appointment",
        "reschedule_appointment",
        "check_insurance_eligibility",
        "send_secure_text",
        "get_clinic_hours",
        "check_business_hours",
        "get_clinic_directions",
        "escalate_to_staff",
    }
)
EXTENSION_FUNCTIONS = frozenset({"suggest_appointment_type"})


def test_every_specification_function_has_an_argument_model():
    """spec §2 lists fifteen. All fifteen must still be there."""
    assert set(S.ARGUMENT_MODELS) >= SPEC_FUNCTIONS


def test_the_extension_adds_exactly_one_function():
    """Naming the delta rather than bumping a count: a function appearing
    without a test change is what this is meant to catch."""
    assert set(S.ARGUMENT_MODELS) == SPEC_FUNCTIONS | EXTENSION_FUNCTIONS


@pytest.mark.parametrize("name,model", sorted(S.ARGUMENT_MODELS.items()))
def test_every_model_forbids_unknown_fields(name, model):
    """extra=forbid becomes additionalProperties:false for strict tool use."""
    assert model.model_config["extra"] == "forbid"
    schema = model.model_json_schema()
    assert schema.get("additionalProperties") is False, name


# ------------------------------------------------------------------ enums ---

EXPECTED_ENUMS = {
    S.AppointmentType: {"new_patient", "follow_up", "sick_visit", "wellness", "telehealth"},
    S.Modality: {"in_person", "telehealth", "any"},
    S.TimePreference: {"morning", "afternoon", "any"},
    S.IdentifierType: {"dob", "phone", "address_zip"},
    S.MessageType: {
        "intake_forms",
        "appointment_confirmation",
        "telehealth_link",
        "directions",
        "portal_access",
    },
    S.Location: {"main_clinic", "satellite_office"},
    S.EscalationReason: {
        "complex_symptoms",
        "ada_accommodation",
        "provider_hold",
        "upset_patient",
        "billing_issue",
        "prescription_refill",
        "test_results",
        "other",
    },
    S.Priority: {"routine", "urgent", "emergency"},
}


@pytest.mark.parametrize(
    "enum,members",
    EXPECTED_ENUMS.items(),
    ids=lambda value: getattr(value, "__name__", ""),
)
def test_enum_members_match_the_specification(enum, members):
    assert {member.value for member in enum} == members


def test_enum_rejects_a_value_outside_the_specification():
    with pytest.raises(ValidationError):
        S.GetClinicDirectionsArgs(location="north_satellite_office")
    with pytest.raises(ValidationError):
        S.EscalateToStaffArgs(reason="lab_result", priority="routine", notes="x")
    with pytest.raises(ValidationError):
        S.EscalateToStaffArgs(reason="other", priority="critical", notes="x")


# -------------------------------------------------------- required fields ---


@pytest.mark.parametrize(
    "model,payload",
    [
        (S.CheckPatientExistsArgs, {"first_name": "Amara", "last_name": "Osei"}),
        (S.GetPatientAppointmentsArgs, {}),
        (S.CancelAppointmentArgs, {"patient_id": "PT-4101", "appointment_id": "AP-1"}),
        (S.CheckInsuranceEligibilityArgs, {"patient_id": "PT-4101"}),
        (S.GetClinicHoursArgs, {}),
        (S.EscalateToStaffArgs, {"reason": "other"}),
    ],
)
def test_missing_required_field_is_rejected(model, payload):
    with pytest.raises(ValidationError):
        model(**payload)


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        S.GetPatientAppointmentsArgs(patient_id="PT-4101", include_history=True)


# --------------------------------------------------------------- dates ---


def test_iso_date_is_accepted_and_typed():
    args = S.CheckPatientExistsArgs(
        first_name="Amara", last_name="Osei", date_of_birth="1978-03-04"
    )
    assert args.date_of_birth == date(1978, 3, 4)


@pytest.mark.parametrize("bad", ["March 4 1978", "04/03/1978", "1978-13-04", "not a date"])
def test_unnormalised_date_is_rejected(bad):
    """spec §4.1 — normalise to YYYY-MM-DD *before* calling."""
    with pytest.raises(ValidationError):
        S.CheckPatientExistsArgs(first_name="Amara", last_name="Osei", date_of_birth=bad)


def test_appointment_time_is_typed():
    args = S.BookAppointmentArgs(
        appointment_date="2026-09-08",
        appointment_time="09:30",
        reason_for_visit="Blood pressure review",
        patient_id="PT-4101",
    )
    assert args.appointment_time == time(9, 30)


# ------------------------------------------------- cross-field invariants ---


def test_verification_requires_two_different_identifier_types():
    """spec §3 rule 4."""
    with pytest.raises(ValidationError, match="must differ"):
        S.VerifyPatientIdentityArgs(
            patient_id="PT-4101",
            identifier_1_type="dob",
            identifier_1_value="1978-03-04",
            identifier_2_type="dob",
            identifier_2_value="1978-03-04",
        )


def test_verification_accepts_two_distinct_types():
    args = S.VerifyPatientIdentityArgs(
        patient_id="PT-4101",
        identifier_1_type="dob",
        identifier_1_value="1978-03-04",
        identifier_2_type="address_zip",
        identifier_2_value="98101",
    )
    assert args.identifier_1_type is S.IdentifierType.DOB
    assert args.identifier_2_type is S.IdentifierType.ADDRESS_ZIP


def test_search_rejects_a_backwards_date_range():
    """spec §4.5."""
    with pytest.raises(ValidationError, match="before"):
        S.SearchAvailableAppointmentsArgs(
            appointment_type="follow_up",
            date_range_start="2026-09-11",
            date_range_end="2026-09-07",
            modality="any",
        )


def test_search_accepts_a_single_day_range():
    args = S.SearchAvailableAppointmentsArgs(
        appointment_type="follow_up",
        date_range_start="2026-09-07",
        date_range_end="2026-09-07",
        modality="any",
    )
    assert args.time_preference is S.TimePreference.ANY


def test_booking_needs_an_id_or_a_full_name():
    """spec §4.6."""
    with pytest.raises(ValidationError, match="patient_id"):
        S.BookAppointmentArgs(
            appointment_date="2026-09-08",
            appointment_time="09:30",
            reason_for_visit="Blood pressure review",
        )
    with pytest.raises(ValidationError):
        S.BookAppointmentArgs(
            appointment_date="2026-09-08",
            appointment_time="09:30",
            reason_for_visit="Blood pressure review",
            patient_first_name="Amara",
        )
    ok = S.BookAppointmentArgs(
        appointment_date="2026-09-08",
        appointment_time="09:30",
        reason_for_visit="Blood pressure review",
        patient_first_name="Amara",
        patient_last_name="Osei",
    )
    assert ok.patient_id is None


def test_appointment_details_only_on_confirmations():
    """spec §4.10 — no unnecessary health detail in a text."""
    with pytest.raises(ValidationError, match="appointment_confirmation"):
        S.SendSecureTextArgs(
            phone_number="+12065550142",
            message_type="directions",
            appointment_details="Tue 8 Sep 09:30 with Dr. Alvarez",
        )
    ok = S.SendSecureTextArgs(
        phone_number="+12065550142",
        message_type="appointment_confirmation",
        appointment_details="Tue 8 Sep 09:30 with Dr. Alvarez",
    )
    assert ok.appointment_details is not None


def test_demographics_verified_flag_is_pinned_true():
    """The gate asserts this from session state; the model may not vary it."""
    assert S.GetPatientDemographicsArgs(patient_id="PT-4101").verified is True
    with pytest.raises(ValidationError):
        S.GetPatientDemographicsArgs(patient_id="PT-4101", verified=False)


# --------------------------------------------------------------- phone ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+12065550142", "+12065550142"),
        ("206-555-0142", "+12065550142"),
        ("(206) 555 0142", "+12065550142"),
        ("1 206 555 0142", "+12065550142"),
    ],
)
def test_phone_numbers_normalise(raw, expected):
    args = S.SendSecureTextArgs(phone_number=raw, message_type="directions")
    assert args.phone_number == expected


@pytest.mark.parametrize("bad", ["555-0142", "not a phone", "+44 20 7946 0958", ""])
def test_unusable_phone_numbers_are_rejected(bad):
    """A misrouted secure text is a disclosure — never guess at the number."""
    with pytest.raises(ValidationError):
        S.SendSecureTextArgs(phone_number=bad, message_type="directions")


def test_registration_rejects_a_malformed_email():
    with pytest.raises(ValidationError):
        S.CreateNewPatientRecordArgs(
            first_name="Ada",
            last_name="Nwosu",
            date_of_birth="1990-01-01",
            phone_number="+12065550999",
            email="not-an-email",
        )


def test_check_business_hours_takes_no_arguments():
    assert S.CheckBusinessHoursArgs().model_dump() == {}
    with pytest.raises(ValidationError):
        S.CheckBusinessHoursArgs(date="2026-09-07")
