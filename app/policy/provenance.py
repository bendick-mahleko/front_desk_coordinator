"""The provenance ledger.

Specification §6 forbids inventing patient IDs, appointment IDs, slot IDs,
providers, availability, insurance status and delivery status. Four of those are
arguments the model supplies, and format validation cannot catch a plausible
fabrication — ``PT-40921`` has exactly the right shape and does not exist.

The rule is one sentence: **an identifier may only be passed into a function if
the system has previously handed it out.** After every successful call the
executor absorbs any identifier in the result; before every call the gate checks
each ID-shaped argument against what has been absorbed.

Three specification requirements then hold without needing their own logic:
booking a slot that was actually offered (§4.6), finding an appointment before
cancelling it (§4.7), and taking an eligibility service date from a real
appointment (§4.9).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from app.policy.messages import Remedy
from app.store.session import Session, slot_time_key

ID_ARGUMENTS: dict[str, str] = {
    "patient_id": "seen_patient_ids",
    "appointment_id": "seen_appointment_ids",
    "current_appointment_id": "seen_appointment_ids",
    "new_appointment_slot_id": "seen_slot_ids",
}

REMEDY_FOR_ARGUMENT: dict[str, Remedy] = {
    "patient_id": Remedy.IDENTIFY_FIRST,
    "appointment_id": Remedy.LOOK_UP_APPOINTMENTS,
    "current_appointment_id": Remedy.LOOK_UP_APPOINTMENTS,
    "new_appointment_slot_id": Remedy.SEARCH_SLOTS_FIRST,
}


def check(args: dict[str, Any], session: Session) -> tuple[str, Remedy] | None:
    """Return (argument, remedy) for the first fabricated reference, else None."""
    for argument, ledger_field in ID_ARGUMENTS.items():
        value = args.get(argument)
        if value is None:
            continue
        if value not in getattr(session, ledger_field):
            return argument, REMEDY_FOR_ARGUMENT[argument]
    return None


# ------------------------------------------------------------- absorption ---

_FIELD_TO_LEDGER: dict[str, str] = {
    "patient_id": "seen_patient_ids",
    "appointment_id": "seen_appointment_ids",
    "slot_id": "seen_slot_ids",
}


def absorb(result: Any, session: Session) -> None:
    """Record every identifier a successful tool result handed out.

    Walks models, lists and dicts, so a list of appointments or slots is
    absorbed as readily as a single object.
    """
    for item in _iterate(result):
        if not isinstance(item, BaseModel):
            continue
        data = item.model_dump()

        for field, ledger in _FIELD_TO_LEDGER.items():
            value = data.get(field)
            if isinstance(value, str) and value:
                setattr(session, ledger, getattr(session, ledger) | {value})

        # A search result is only usable if the *time* it offered is remembered:
        # book_appointment takes a date and a time, not a slot id.
        if "slot_date" in data and "slot_time" in data:
            session.offered_times = session.offered_times | {
                slot_time_key(data["slot_date"], data["slot_time"])
            }

        # A booked appointment fixes a date of service, which is what an
        # eligibility check is allowed to ask about (spec §4.9).
        if "appointment_date" in data and data.get("status") == "scheduled":
            session.booked_service_dates = session.booked_service_dates | {data["appointment_date"]}


def _iterate(value: Any) -> Iterable[Any]:
    if isinstance(value, list | tuple | set):
        for item in value:
            yield from _iterate(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iterate(item)
    else:
        yield value
