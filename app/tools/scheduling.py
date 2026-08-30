"""Appointment search, booking, cancellation and rescheduling — spec §4.5–§4.8."""

from __future__ import annotations

from datetime import date, time
from typing import Any

from app.config import get_clinic_config
from app.policy.decorator import current_session
from app.tools.registry import backends, key_for, tool
from app.tools.schemas import AppointmentType, Modality, TimePreference


@tool("search_available_appointments")
def search_available_appointments(
    appointment_type: AppointmentType,
    date_range_start: date,
    date_range_end: date,
    modality: Modality,
    preferred_provider: str | None = None,
    time_preference: TimePreference = TimePreference.ANY,
) -> Any:
    """Find open appointment slots.

    Convert relative requests into explicit dates before calling — "next week"
    must become a start and end date. Confirm the visit type and whether the
    patient wants to be seen in person or by telehealth if it is not clear.

    Offer the patient the returned slots and let them choose. Only a slot from
    these results can then be booked, so do not suggest a time that is not here.

    Do not interpret symptoms when choosing a visit type. If anything the
    patient says suggests an emergency, stop scheduling and follow the
    escalation path instead.
    """
    clinic = get_clinic_config()
    morning_only = {
        TimePreference.MORNING: True,
        TimePreference.AFTERNOON: False,
        TimePreference.ANY: None,
    }[time_preference]

    slots = backends().schedule.search_slots(
        appointment_type,
        date_range_start,
        date_range_end,
        modality,
        preferred_provider=preferred_provider,
        morning_only=morning_only,
        limit=clinic.policy.max_slots_presented,
    )
    if not slots:
        return {
            "slots": [],
            "next_step": (
                "Nothing is available with those preferences. Offer a wider date range, "
                "a different provider, or a different modality."
            ),
        }
    return {
        "slots": slots,
        "next_step": (
            "Offer these times to the patient. Before booking, confirm the date and "
            "time, the provider, the visit type and modality, a brief reason for the "
            "visit, and whether they want a reminder."
        ),
    }


@tool("book_appointment")
def book_appointment(
    appointment_date: date,
    appointment_time: time,
    reason_for_visit: str,
    patient_id: str | None = None,
    patient_first_name: str | None = None,
    patient_last_name: str | None = None,
    provider: str | None = None,
    appointment_type: AppointmentType | None = None,
    modality: Modality | None = None,
    send_reminder: bool | None = None,
) -> Any:
    """Book an appointment at a time returned by a previous search.

    Confirm all of the following with the patient immediately before calling:
    the date and time, the provider if one was chosen, the visit type and
    modality, a brief reason for the visit, and their reminder preference.

    Never tell a patient an appointment is booked until this function has
    returned successfully. If the time has been taken, say so and search again.

    After a successful telehealth booking, offer to send the telehealth link
    with send_secure_text.
    """
    session = current_session()
    subject = patient_id or session.patient_id
    if subject is None:
        return {
            "error": "no_patient",
            "message": "There is no identified patient to book for.",
            "remedy": (
                "Look the patient up with check_patient_exists, or register them with "
                "create_new_patient_record, before booking."
            ),
        }

    appointment = backends().schedule.book(
        subject,
        appointment_date,
        appointment_time,
        reason_for_visit,
        provider=provider,
        appointment_type=appointment_type,
        modality=modality,
        idempotency_key=key_for(
            "book_appointment",
            patient_id=subject,
            appointment_date=appointment_date,
            appointment_time=appointment_time,
        ),
    )

    payload: dict[str, Any] = {
        "appointment": appointment,
        "booked": True,
        "send_reminder": send_reminder,
    }
    if appointment.modality is Modality.TELEHEALTH:
        payload["next_step"] = (
            "Confirm the booking to the patient, then offer to text them the "
            "telehealth link with send_secure_text."
        )
    else:
        payload["next_step"] = "Confirm the date, time, provider and location to the patient."
    return payload


@tool("cancel_appointment")
def cancel_appointment(patient_id: str, appointment_id: str, cancellation_reason: str) -> Any:
    """Cancel a booked appointment.

    Find the appointment with get_patient_appointments first, then confirm which
    one the patient means and that they want it cancelled. Collect a brief
    reason.

    If the appointment is inside the late-cancellation window, tell the patient
    what that means for them before you confirm the cancellation, not after.
    """
    clinic = get_clinic_config()
    result = backends().schedule.cancel(
        patient_id,
        appointment_id,
        cancellation_reason,
        idempotency_key=key_for(
            "cancel_appointment", patient_id=patient_id, appointment_id=appointment_id
        ),
    )

    payload: dict[str, Any] = {
        "appointment_id": result.appointment_id,
        "cancelled": True,
        "late_cancellation": result.late_cancellation,
    }
    if result.late_cancellation:
        payload["policy_notice"] = clinic.policy.late_cancellation_notice
    payload["next_step"] = (
        "Confirm the cancellation to the patient, and offer to text them a "
        "confirmation and to book a replacement appointment."
    )
    return payload


@tool("reschedule_appointment")
def reschedule_appointment(
    patient_id: str,
    current_appointment_id: str,
    new_appointment_slot_id: str,
    reschedule_reason: str,
) -> Any:
    """Move an existing appointment to a new slot.

    Find the current appointment with get_patient_appointments, find a
    replacement with search_available_appointments, and confirm both with the
    patient before calling.

    This moves the appointment in one step. Do not cancel the old one
    separately — if this fails, the patient still has their original
    appointment, which is the safe outcome.
    """
    moved = backends().schedule.reschedule(
        patient_id,
        current_appointment_id,
        new_appointment_slot_id,
        reschedule_reason,
        idempotency_key=key_for(
            "reschedule_appointment",
            patient_id=patient_id,
            current_appointment_id=current_appointment_id,
            new_appointment_slot_id=new_appointment_slot_id,
        ),
    )
    return {
        "appointment": moved,
        "rescheduled": True,
        "next_step": (
            "Tell the patient the new date, time, provider and whether it is in "
            "person or telehealth."
        ),
    }
