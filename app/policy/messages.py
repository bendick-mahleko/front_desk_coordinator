"""Patient-safe denial vocabulary.

Specification §3 rule 5: a failed verification must not reveal protected
information. That constrains the *wording* of every refusal, not just the
verification one — "that ZIP does not match the 98101 we have on file" fails the
rule while sounding helpful.

So denial text is drawn from a fixed vocabulary. Nothing here interpolates a
record value, and nothing may be added that does. ``test_redaction.py`` asserts
that no fixture value appears in any string in this module.

These strings are what the *model* receives as the denial. The model then says
something natural to the patient; it is not required to read these aloud. What
matters is that even a model repeating them verbatim discloses nothing.
"""

from __future__ import annotations

from enum import StrEnum


class DenialCode(StrEnum):
    """The four checks of design §7, in the order they run."""

    INVALID_ARGUMENTS = "invalid_arguments"
    VERIFICATION_REQUIRED = "verification_required"
    UNKNOWN_REFERENCE = "unknown_reference"
    PRECONDITION_FAILED = "precondition_failed"
    UNKNOWN_FUNCTION = "unknown_function"

    # spec r3. Role is a second axis, so it needs its own codes: "you are not
    # authenticated as clinical staff" is not a rung below "this patient is not
    # verified", and conflating them would tell a clinician to verify a patient.
    ROLE_REQUIRED = "role_required"
    SESSION_EXPIRED = "session_expired"


DENIAL_MESSAGES: dict[DenialCode, str] = {
    DenialCode.INVALID_ARGUMENTS: "The call was not valid for this function.",
    DenialCode.VERIFICATION_REQUIRED: (
        "This information is not available until the patient's identity has been verified."
    ),
    DenialCode.UNKNOWN_REFERENCE: (
        "That reference did not come from a result in this conversation."
    ),
    DenialCode.PRECONDITION_FAILED: "A required earlier step has not been completed.",
    DenialCode.UNKNOWN_FUNCTION: "That function is not available.",
    DenialCode.ROLE_REQUIRED: ("This function requires an authenticated clinical session."),
    DenialCode.SESSION_EXPIRED: (
        "The clinical session has expired. Clinical review is no longer available "
        "in this conversation."
    ),
}


class Remedy(StrEnum):
    """What the assistant should do next. One key per recoverable situation."""

    IDENTIFY_FIRST = "identify_first"
    VERIFY_FIRST = "verify_first"
    COLLECT_SECOND_IDENTIFIER = "collect_second_identifier"
    TRY_DIFFERENT_IDENTIFIERS = "try_different_identifiers"
    ESCALATE_LOCKED = "escalate_locked"
    ESCALATE_DUPLICATE = "escalate_duplicate"
    CHECK_EXISTENCE_FIRST = "check_existence_first"
    LOOK_UP_APPOINTMENTS = "look_up_appointments"
    SEARCH_SLOTS_FIRST = "search_slots_first"
    CONFIRM_SERVICE_DATE = "confirm_service_date"
    CONFIRM_PHONE_NUMBER = "confirm_phone_number"
    UNKNOWN_LOCATION = "unknown_location"
    NEW_PATIENT_FIRST_VISIT = "new_patient_first_visit"
    AUTHENTICATE_FIRST = "authenticate_first"
    REAUTHENTICATE = "reauthenticate"
    USE_CLINICAL_CHANNEL = "use_clinical_channel"
    FIX_ARGUMENTS = "fix_arguments"
    DISAMBIGUATE = "disambiguate"
    WRONG_SUBJECT = "wrong_subject"


REMEDIES: dict[Remedy, str] = {
    Remedy.IDENTIFY_FIRST: (
        "Collect the patient's first name, last name and date of birth, then call "
        "check_patient_exists."
    ),
    Remedy.VERIFY_FIRST: (
        "Verify the patient with verify_patient_identity before requesting protected information."
    ),
    Remedy.COLLECT_SECOND_IDENTIFIER: (
        "Ask the patient for a second identifier of a different type: date of birth, "
        "phone number or ZIP code."
    ),
    Remedy.TRY_DIFFERENT_IDENTIFIERS: (
        "That combination has already been tried. Offer a different pair of identifier "
        "types, or escalate to staff."
    ),
    Remedy.ESCALATE_LOCKED: (
        "The verification attempt limit has been reached. Do not ask for more "
        "identifiers. Call escalate_to_staff so a person can help."
    ),
    Remedy.ESCALATE_DUPLICATE: (
        "A possible duplicate record was found. Do not create a new record. Call "
        "escalate_to_staff so a person can resolve it."
    ),
    Remedy.CHECK_EXISTENCE_FIRST: (
        "Call check_patient_exists before creating a record, so a duplicate is not created."
    ),
    Remedy.LOOK_UP_APPOINTMENTS: (
        "Call get_patient_appointments to find the appointment before changing it."
    ),
    Remedy.SEARCH_SLOTS_FIRST: (
        "Call search_available_appointments and offer the patient a returned slot. Only "
        "a time from those results can be booked."
    ),
    Remedy.CONFIRM_SERVICE_DATE: (
        "Take the date of service from a booked appointment, or ask the patient to "
        "confirm it, before checking eligibility."
    ),
    Remedy.CONFIRM_PHONE_NUMBER: (
        "Confirm with the patient that the destination number is theirs before sending."
    ),
    Remedy.UNKNOWN_LOCATION: (
        "That location is not configured. Offer the clinic locations that are, or "
        "escalate to staff."
    ),
    Remedy.NEW_PATIENT_FIRST_VISIT: (
        "This patient registered during this conversation, so they have no earlier "
        "visit to follow up on. Their first appointment is a new_patient visit, or a "
        "sick_visit if they have described something acute. Do not ask the patient "
        "which they want — choose, tell them which you are booking, and continue."
    ),
    Remedy.AUTHENTICATE_FIRST: (
        "Call authenticate_clinical_user first. Clinical review is unavailable "
        "until the clinic's identity provider has confirmed who is asking."
    ),
    Remedy.REAUTHENTICATE: (
        "The clinical session has expired and cannot be extended from inside the "
        "conversation. Tell the clinician plainly, and ask them to establish a "
        "new session on the clinical channel. Do not answer the question from "
        "your own knowledge, and do not offer a partial or general answer."
    ),
    Remedy.USE_CLINICAL_CHANNEL: (
        "Clinical review is not available in this conversation and cannot be "
        "granted here. Direct the person to the clinic's clinical channel. Do "
        "not describe what the capability would have told them."
    ),
    Remedy.FIX_ARGUMENTS: "Correct the arguments and call the function again.",
    Remedy.WRONG_SUBJECT: (
        "That is not the patient whose identity was verified in this conversation. "
        "Use the verified patient's id, or verify the other patient separately "
        "before acting on their record."
    ),
    Remedy.DISAMBIGUATE: (
        "More than one record matched. Ask only for permitted disambiguating "
        "information, or transfer to staff. Do not choose between the records."
    ),
}


def denial_message(code: DenialCode) -> str:
    return DENIAL_MESSAGES[code]


def remedy_text(remedy: Remedy) -> str:
    return REMEDIES[remedy]


def all_patient_facing_strings() -> list[str]:
    """Everything this module can emit. The redaction tripwire test reads this."""
    return [*DENIAL_MESSAGES.values(), *REMEDIES.values()]
