"""Insurance eligibility — specification §4.9."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.ports import EligibilityStatus
from app.tools.registry import backends, tool

COVERAGE_DISCLAIMER = (
    "Eligibility is not a guarantee of coverage, payment, copay, authorisation or "
    "benefits. Say this to the patient whenever you report an eligibility result."
)


@tool("check_insurance_eligibility")
def check_insurance_eligibility(patient_id: str, service_date: date) -> Any:
    """Check whether a verified patient's insurance is active for a date of service.

    Take the date of service from a booked appointment, or ask the patient to
    confirm it. Requires identity verification first.

    This returns coverage status only. It does not return copay, deductible or
    benefit amounts. If the patient asks about a copay, explain that you cannot
    see that here and call escalate_to_staff with reason='billing_issue'.

    Always tell the patient that eligibility is not a guarantee of coverage or
    payment. If the result is indeterminate, escalate for manual review rather
    than interpreting it.
    """
    result = backends().eligibility.check(patient_id, service_date)

    payload: dict[str, Any] = {
        "status": result.status.value,
        "plan_name": result.plan_name,
        "payer": result.payer,
        "service_date": result.service_date,
        "disclaimer": COVERAGE_DISCLAIMER,
        # Named explicitly so the model does not assume the field was simply
        # omitted this time (spec §4.9).
        "copay_available": False,
    }

    if result.status is EligibilityStatus.INDETERMINATE:
        payload["next_step"] = (
            "The payer did not give a usable answer. Tell the patient it could not be "
            "confirmed and call escalate_to_staff with reason='billing_issue' for "
            "manual review. Do not interpret this as covered or not covered."
        )
    elif result.status is EligibilityStatus.INACTIVE:
        payload["next_step"] = (
            "Coverage is not active for that date. Tell the patient, include the "
            "disclaimer text above in your reply, and offer to have staff look "
            "into it."
        )
    else:
        payload["next_step"] = (
            "Coverage is active for that date. You must include the disclaimer text "
            "above in your reply, in your own sentence — the patient has to hear "
            "that eligibility is not a guarantee of coverage or payment. Reporting "
            "the status without it is not acceptable."
        )
    return payload
