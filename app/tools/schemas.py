"""Function argument models and enumerations — the single schema source.

One Pydantic model per function (AD-02). Its JSON Schema is what Claude sees;
the same model validates at execution time. There is no second hand-written
schema to drift out of step, and every enum in the specification is declared
exactly once here.

Every model sets ``extra="forbid"``, which becomes ``additionalProperties:
false`` in the emitted schema — required for strict tool use.

Argument-level invariants live here. Anything needing *session* state (has this
patient been verified? did this slot come from a search?) is not an argument
invariant and belongs to the policy gate in Phase 2.
"""

from __future__ import annotations

import re
from datetime import date, time
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ------------------------------------------------------------- enums ---
# Specification §4.5, §4.10, §4.11, §4.12 and §3. Declared once, referenced
# everywhere. A value that is not a member cannot reach a backend.


class AppointmentType(StrEnum):
    NEW_PATIENT = "new_patient"
    FOLLOW_UP = "follow_up"
    SICK_VISIT = "sick_visit"
    WELLNESS = "wellness"
    TELEHEALTH = "telehealth"


class Modality(StrEnum):
    IN_PERSON = "in_person"
    TELEHEALTH = "telehealth"
    ANY = "any"


class TimePreference(StrEnum):
    MORNING = "morning"
    AFTERNOON = "afternoon"
    ANY = "any"


class IdentifierType(StrEnum):
    DOB = "dob"
    PHONE = "phone"
    ADDRESS_ZIP = "address_zip"


class MessageType(StrEnum):
    INTAKE_FORMS = "intake_forms"
    APPOINTMENT_CONFIRMATION = "appointment_confirmation"
    TELEHEALTH_LINK = "telehealth_link"
    DIRECTIONS = "directions"
    PORTAL_ACCESS = "portal_access"


class Location(StrEnum):
    MAIN_CLINIC = "main_clinic"
    SATELLITE_OFFICE = "satellite_office"


class EscalationReason(StrEnum):
    COMPLEX_SYMPTOMS = "complex_symptoms"
    ADA_ACCOMMODATION = "ada_accommodation"
    PROVIDER_HOLD = "provider_hold"
    UPSET_PATIENT = "upset_patient"
    BILLING_ISSUE = "billing_issue"
    PRESCRIPTION_REFILL = "prescription_refill"
    TEST_RESULTS = "test_results"
    OTHER = "other"


class Priority(StrEnum):
    ROUTINE = "routine"
    URGENT = "urgent"
    EMERGENCY = "emergency"


class ClinicalRole(StrEnum):
    """The licensed directory roles §4.13 accepts.

    This is the *claim* a clinician presents and the *assertion* the identity
    provider returns. It is not the session's principal — that is
    ``app.store.session.Role``, and only the provider's response may set it
    (spec §3.2 item 3). An enum here so a role that is not licensed cannot even
    be named in a call.
    """

    PHYSICIAN = "physician"
    NURSE_PRACTITIONER = "nurse_practitioner"
    PHYSICIAN_ASSISTANT = "physician_assistant"
    REGISTERED_NURSE = "registered_nurse"
    CLINICAL_PHARMACIST = "clinical_pharmacist"


# --------------------------------------------------------- primitives ---

PHONE_DIGITS = re.compile(r"\D")
ZIP_PATTERN = re.compile(r"^\d{5}$")

Name = Annotated[str, Field(min_length=1, max_length=80)]
ShortText = Annotated[str, Field(min_length=1, max_length=200)]
Notes = Annotated[str, Field(min_length=1, max_length=1000)]


def normalise_phone(value: str) -> str:
    """Reduce a US phone number to +1XXXXXXXXXX.

    Prototype scope is US numbers. Anything else is rejected rather than
    guessed at — a misrouted secure text is a disclosure.
    """
    digits = PHONE_DIGITS.sub("", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError(f"expected a 10-digit US phone number, got {value!r}")
    return f"+1{digits}"


class StrictArgs(BaseModel):
    """Base for every function's arguments."""

    model_config = ConfigDict(
        extra="forbid",  # -> additionalProperties: false
        str_strip_whitespace=True,
        use_enum_values=False,
        validate_assignment=True,
    )


# ------------------------------------------- patient lookup and access ---


class CheckPatientExistsArgs(StrictArgs):
    """spec §4.1 — minimal-disclosure existence check."""

    first_name: Name
    last_name: Name
    date_of_birth: date


class VerifyPatientIdentityArgs(StrictArgs):
    """spec §4.2 / §3 — two identifiers, of two different permitted types."""

    patient_id: ShortText
    identifier_1_type: IdentifierType
    identifier_1_value: ShortText
    identifier_2_type: IdentifierType
    identifier_2_value: ShortText

    @model_validator(mode="after")
    def _types_must_differ(self) -> VerifyPatientIdentityArgs:
        # spec §3 rule 4. A pure argument invariant — no session needed — so it
        # is enforced here rather than in the policy gate.
        if self.identifier_1_type == self.identifier_2_type:
            raise ValueError(
                "identifier_1_type and identifier_2_type must differ; "
                f"both were {self.identifier_1_type.value!r}"
            )
        return self


class GetPatientDemographicsArgs(StrictArgs):
    """spec §4.3 — callable only after successful verification."""

    patient_id: ShortText
    # The specification writes this call as verified=True. The flag is asserted
    # from session state by the gate, never trusted from the model (design §10).
    verified: Literal[True] = True


class GetPatientAppointmentsArgs(StrictArgs):
    """spec §4.3."""

    patient_id: ShortText


# --------------------------------------------- new-patient registration ---


class CreateNewPatientRecordArgs(StrictArgs):
    """spec §4.4."""

    first_name: Name
    last_name: Name
    date_of_birth: date
    phone_number: ShortText
    email: str | None = None
    # Plan name only. Collecting member IDs or group numbers needs a compliant
    # integration that does not exist (spec §4.4).
    insurance_plan_name: str | None = Field(default=None, max_length=120)

    @field_validator("phone_number")
    @classmethod
    def _normalise_phone(cls, value: str) -> str:
        return normalise_phone(value)

    @field_validator("email")
    @classmethod
    def _basic_email_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError(f"not a usable email address: {value!r}")
        return value


# ------------------------------------------------------------ scheduling ---


class SearchAvailableAppointmentsArgs(StrictArgs):
    """spec §4.5."""

    appointment_type: AppointmentType
    date_range_start: date
    date_range_end: date
    modality: Modality
    preferred_provider: str | None = Field(default=None, max_length=80)
    time_preference: TimePreference = TimePreference.ANY

    @model_validator(mode="after")
    def _range_must_be_ordered(self) -> SearchAvailableAppointmentsArgs:
        # spec §4.5 — "validate that date-range end is not before date-range
        # start". Cross-field, but needs no session state.
        if self.date_range_end < self.date_range_start:
            raise ValueError(
                f"date_range_end ({self.date_range_end}) is before "
                f"date_range_start ({self.date_range_start})"
            )
        return self


class BookAppointmentArgs(StrictArgs):
    """spec §4.6."""

    appointment_date: date
    appointment_time: time
    reason_for_visit: ShortText

    patient_id: str | None = None
    patient_first_name: str | None = None
    patient_last_name: str | None = None

    provider: str | None = Field(default=None, max_length=80)
    appointment_type: AppointmentType | None = None
    modality: Modality | None = None
    send_reminder: bool | None = None

    @model_validator(mode="after")
    def _patient_must_be_identifiable(self) -> BookAppointmentArgs:
        # spec §4.6 — "patient_id, or patient_first_name and patient_last_name".
        if self.patient_id:
            return self
        if self.patient_first_name and self.patient_last_name:
            return self
        raise ValueError("supply patient_id, or both patient_first_name and patient_last_name")


class CancelAppointmentArgs(StrictArgs):
    """spec §4.7."""

    patient_id: ShortText
    appointment_id: ShortText
    cancellation_reason: ShortText


class RescheduleAppointmentArgs(StrictArgs):
    """spec §4.8."""

    patient_id: ShortText
    current_appointment_id: ShortText
    new_appointment_slot_id: ShortText
    reschedule_reason: ShortText


# ------------------------------------------------------------- insurance ---


class CheckInsuranceEligibilityArgs(StrictArgs):
    """spec §4.9."""

    patient_id: ShortText
    service_date: date


# ------------------------------------------------------------- messaging ---


class SendSecureTextArgs(StrictArgs):
    """spec §4.10."""

    phone_number: ShortText
    message_type: MessageType
    appointment_details: str | None = Field(default=None, max_length=300)

    @field_validator("phone_number")
    @classmethod
    def _normalise_phone(cls, value: str) -> str:
        return normalise_phone(value)

    @model_validator(mode="after")
    def _details_only_for_confirmations(self) -> SendSecureTextArgs:
        # spec §4.10 — "use appointment_details only for appointment-confirmation
        # messages". Anywhere else it is unnecessary health detail in a text.
        if (
            self.appointment_details is not None
            and self.message_type is not MessageType.APPOINTMENT_CONFIRMATION
        ):
            raise ValueError(
                "appointment_details is only permitted with message_type='appointment_confirmation'"
            )
        return self


# ------------------------------------------------------- clinic information ---


class GetClinicHoursArgs(StrictArgs):
    """spec §4.11 — future dates, weekends, holidays, a named day."""

    date: date


class CheckBusinessHoursArgs(StrictArgs):
    """spec §4.11 — the only correct answer to "are you open now?"."""


class GetClinicDirectionsArgs(StrictArgs):
    """spec §4.11 — the location enum admits exactly two values."""

    location: Location


# ------------------------------------------------------- knowledge base ---


class SuggestAppointmentTypeArgs(StrictArgs):
    """R3 — a complaint in the patient's own words, for routing only."""

    complaint: Annotated[str, Field(min_length=3, max_length=500)]


# --------------------------------------------------------------- escalation ---


class EscalateToStaffArgs(StrictArgs):
    """spec §4.12."""

    reason: EscalationReason
    priority: Priority = Priority.ROUTINE
    notes: Notes
    # Attached only when known and appropriate (spec §4.12).
    patient_id: str | None = None


# ------------------------------------------------------------------ registry ---

ARGUMENT_MODELS: dict[str, type[StrictArgs]] = {
    "check_patient_exists": CheckPatientExistsArgs,
    "verify_patient_identity": VerifyPatientIdentityArgs,
    "get_patient_demographics": GetPatientDemographicsArgs,
    "get_patient_appointments": GetPatientAppointmentsArgs,
    "create_new_patient_record": CreateNewPatientRecordArgs,
    "search_available_appointments": SearchAvailableAppointmentsArgs,
    "book_appointment": BookAppointmentArgs,
    "cancel_appointment": CancelAppointmentArgs,
    "reschedule_appointment": RescheduleAppointmentArgs,
    "check_insurance_eligibility": CheckInsuranceEligibilityArgs,
    "send_secure_text": SendSecureTextArgs,
    "get_clinic_hours": GetClinicHoursArgs,
    "check_business_hours": CheckBusinessHoursArgs,
    "get_clinic_directions": GetClinicDirectionsArgs,
    "escalate_to_staff": EscalateToStaffArgs,
    "suggest_appointment_type": SuggestAppointmentTypeArgs,
}
"""The functions the assistant may call.

Fifteen from specification §2, plus suggest_appointment_type from the knowledge
extension. The extension adds an *administrative* function — it routes a
complaint to a visit type — and deliberately adds nothing that returns clinical
content to a patient."""
