"""Patient lookup, verification, access and registration — specification §4.1–§4.4.

Docstrings here are not internal documentation: they become the tool
descriptions Claude reads when deciding what to call. They are written for that
reader — what the function does, when to use it, and what it will refuse.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.config import get_clinic_config
from app.policy.decorator import current_session
from app.policy.verification import (
    attempts_remaining,
    available_combinations,
    record_lookup,
    register_attempt,
)
from app.store.session import SubjectStatus
from app.tools.registry import backends, key_for, tool
from app.tools.schemas import IdentifierType


@tool("check_patient_exists")
def check_patient_exists(first_name: str, last_name: str, date_of_birth: date) -> Any:
    """Check whether someone already has a record at this clinic.

    Use this first for any patient who may already be registered. Collect the
    name and date of birth one item at a time, and normalise the date of birth
    to YYYY-MM-DD before calling.

    Returns only how many records matched and, for a single match, the
    patient_id. It never returns demographics or appointments — those need
    identity verification first.

    If exactly one record matches, verify the patient next. If none match, offer
    new-patient registration. If more than one matches, ask only for permitted
    disambiguating information or hand over to staff; do not guess which record
    is theirs.
    """
    session = current_session()
    result = backends().patients.check_exists(first_name, last_name, date_of_birth)
    record_lookup(session, result)

    payload: dict[str, Any] = {
        "match_count": result.match_count,
        "patient_id": result.patient_id,
    }
    if result.ambiguous:
        payload["next_step"] = (
            "More than one record matched. Ask for permitted disambiguating "
            "information or escalate to staff. Do not choose between them."
        )
    elif result.match_count == 0:
        payload["next_step"] = "No record matched. Offer to register the patient as a new patient."
    else:
        payload["next_step"] = "Verify the patient's identity before disclosing anything."
    return payload


@tool("verify_patient_identity")
def verify_patient_identity(
    patient_id: str,
    identifier_1_type: IdentifierType,
    identifier_1_value: str,
    identifier_2_type: IdentifierType,
    identifier_2_value: str,
) -> Any:
    """Verify a patient's identity with two identifiers of different types.

    Required before any protected information is disclosed and before any
    appointment is changed. The two identifiers must be of different types:
    dob, phone or address_zip.

    Ask for the identifiers one at a time. When repeating a value back, mask it.

    Returns whether verification succeeded and how many attempts remain. It
    never says which identifier was wrong — that would confirm the other one.
    After the final attempt the session locks and escalate_to_staff is the only
    remaining action.
    """
    session = current_session()
    clinic = get_clinic_config()

    result = backends().patients.verify_identity(
        patient_id,
        {identifier_1_type: identifier_1_value, identifier_2_type: identifier_2_value},
    )
    outcome = register_attempt(
        session,
        identifier_1_type,
        identifier_1_value,
        identifier_2_type,
        identifier_2_value,
        result,
        clinic,
    )

    payload: dict[str, Any] = {
        "verified": outcome.verified,
        "attempts_remaining": outcome.attempts_remaining,
    }
    if outcome.verified:
        # The phone on file becomes the confirmed destination for secure texts.
        session.confirm_phone(backends().patients.phone_number(patient_id))
        payload["next_step"] = "Identity confirmed. Continue with what the patient asked for."
        return payload

    if outcome.locked:
        payload["locked"] = True
        payload["next_step"] = (
            "The attempt limit has been reached. Do not ask for more identifiers. "
            "Call escalate_to_staff so a person can help."
        )
        return payload

    # Deliberately does not say which identifier failed (spec §3 rule 5).
    payload["next_step"] = (
        "That did not match our records. Offer another permitted pair of identifier "
        "types, or escalate to staff."
    )
    payload["untried_combinations"] = available_combinations(session)
    payload["attempts_remaining"] = attempts_remaining(session, clinic)
    return payload


@tool("get_patient_demographics")
def get_patient_demographics(patient_id: str, verified: bool = True) -> Any:
    """Return a verified patient's demographic record.

    Only callable after successful identity verification. Read back only the
    specific detail the patient asked for — do not recite the whole record. If
    someone else may be able to overhear, confirm it is alright to continue
    before reading anything out.
    """
    session = current_session()
    # P3-T5: the flag is asserted from session state, never taken from the model.
    # The gate has already established this, but a tool that trusted an argument
    # named `verified` would be one prompt injection away from disclosure.
    if session.status is not SubjectStatus.VERIFIED:
        return {
            "error": "verification_required",
            "message": "This information is not available until identity is verified.",
            "remedy": "Verify the patient with verify_patient_identity first.",
        }
    return backends().patients.get_demographics(patient_id)


@tool("get_patient_appointments")
def get_patient_appointments(patient_id: str) -> Any:
    """List a verified patient's appointments.

    Only callable after successful identity verification. Use it before
    cancelling or rescheduling, so the right appointment is identified. Read
    back only what the patient asked about, and confirm privacy first if someone
    else may overhear.
    """
    return backends().schedule.get_appointments(patient_id)


@tool("create_new_patient_record")
def create_new_patient_record(
    first_name: str,
    last_name: str,
    date_of_birth: date,
    phone_number: str,
    email: str | None = None,
    insurance_plan_name: str | None = None,
) -> Any:
    """Register a new patient.

    Call check_patient_exists first — this function refuses to run otherwise, to
    avoid creating a duplicate. Confirm the spelling of the name, the date of
    birth and the phone number with the patient before calling.

    Collect the insurance plan name only. Do not ask for member numbers, group
    numbers or any other insurance detail.

    Once registered, the patient may book an appointment without further
    verification.
    """
    session = current_session()
    repo = backends().patients

    # spec §4.4 — on a possible duplicate, escalate instead of creating. The
    # check happens here rather than in the backend because "escalate instead"
    # means the record must not be created at all.
    existing = repo.check_exists(first_name, last_name, date_of_birth)
    if existing.match_count > 0:
        session.duplicate_suspected = True
        return {
            "error": "duplicate_suspected",
            "message": "A record matching these details already exists.",
            "remedy": (
                "Do not create a second record. Call escalate_to_staff with "
                "reason='other' so a person can resolve it."
            ),
        }

    result = repo.create_record(
        first_name,
        last_name,
        date_of_birth,
        phone_number,
        email=email,
        insurance_plan_name=insurance_plan_name,
        idempotency_key=key_for(
            "create_new_patient_record",
            first_name=first_name,
            last_name=last_name,
            date_of_birth=date_of_birth,
            phone_number=phone_number,
        ),
    )
    session.mark_registered(result.patient_id)
    session.confirm_phone(phone_number)

    return {
        "patient_id": result.patient_id,
        "registered": True,
        "next_step": (
            "Give the patient their registration result and what happens next. "
            "They may now book an appointment."
        ),
    }
