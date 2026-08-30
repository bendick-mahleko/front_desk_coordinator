"""PHI redaction and output masking.

Two different jobs that are easy to confuse:

* **Redaction** removes values before they are *written* — to the audit log or
  the persisted transcript. Specification §4.2 permits recording the result,
  the timestamp and the method of a verification, and nothing else.
* **Masking** shortens values before they are *shown* — when the assistant
  repeats an identifier back to the patient.

Redaction is field-aware first and pattern-based second. Field-aware alone
misses values that leaked into free text; patterns alone cannot tell a ZIP code
from a room number. Doing both means a value has to escape two mechanisms.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import date, datetime, time
from typing import Any

# Fields whose *values* are protected wherever they appear. Keyed by field name
# because the argument models name them consistently (AD-02).
SENSITIVE_FIELDS: dict[str, str] = {
    # Names identify a person as surely as a date of birth does. The audit log
    # keeps the clinic-issued patient_id, which gives an auditor everything they
    # need to trace a decision without the log itself becoming a patient index.
    "first_name": "<name>",
    "last_name": "<name>",
    "patient_first_name": "<name>",
    "patient_last_name": "<name>",
    "date_of_birth": "<dob>",
    "identifier_1_value": "<identifier>",
    "identifier_2_value": "<identifier>",
    "phone_number": "<phone>",
    "email": "<email>",
    "address_zip": "<zip>",
    "address_line": "<address>",
    "appointment_details": "<details>",
    "reason_for_visit": "<reason>",
    "cancellation_reason": "<reason>",
    "reschedule_reason": "<reason>",
    "notes": "<notes>",
}

# Free-text sweep. Deliberately broad: a false positive costs a token in a log,
# a false negative writes a date of birth to disk.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "<dob>"),
    (re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"), "<dob>"),
    (re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"), "<phone>"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "<email>"),
    (re.compile(r"\b\d{5}(?:-\d{4})?\b"), "<zip>"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "<ssn>"),
)

# Identifiers that are safe to log: they are references the clinic issued, not
# facts about a person.
SAFE_REFERENCE_FIELDS = frozenset(
    {
        "patient_id",
        "appointment_id",
        "current_appointment_id",
        "new_appointment_slot_id",
        "slot_id",
        "message_id",
        "ticket_id",
        "session_id",
    }
)


def redact_text(value: str) -> str:
    """Replace anything that looks like protected data with a type token."""
    for pattern, token in _PATTERNS:
        value = pattern.sub(token, value)
    return value


def redact_value(field: str, value: Any) -> Any:
    """Redact one field by name, then sweep whatever survives."""
    if field in SAFE_REFERENCE_FIELDS:
        return value
    if field in SENSITIVE_FIELDS:
        return None if value is None else SENSITIVE_FIELDS[field]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, date | datetime | time):
        # A bare date in a non-sensitive field is still a date. Keep the shape,
        # drop the precision that identifies.
        return "<date>"
    if isinstance(value, dict):
        return {key: redact_value(key, item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [redact_value(field, item) for item in value]
    return value


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """The log-safe view of a function's arguments."""
    return {key: redact_value(key, value) for key, value in args.items()}


def contains_protected_data(text: str) -> bool:
    """True if the sweep would change anything. Used by tests as a tripwire."""
    return redact_text(text) != text


# ------------------------------------------------------------- hashing ---


def digest(value: str, salt: str) -> str:
    """Salted digest of an identifier value.

    Lets a repeated attempt be recognised without the value being stored or
    recoverable. The salt is per-session, so digests cannot be correlated
    across sessions either.
    """
    return hashlib.sha256(f"{salt}|{value.strip().casefold()}".encode()).hexdigest()[:16]


# ------------------------------------------------------------- masking ---


def mask_phone(value: str) -> str:
    """+12065550142 -> (•••) •••-0142"""
    digits = re.sub(r"\D", "", value)
    if len(digits) < 4:
        return "•" * len(digits)
    return f"(•••) •••-{digits[-4:]}"


def mask_date(value: date | str) -> str:
    """1978-03-04 -> ••/••/1978 — enough to confirm, not enough to disclose."""
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            return "••/••/••••"
    return f"••/••/{value.year}"


def mask_zip(value: str) -> str:
    """98101 -> •••01"""
    if len(value) < 2:
        return "•" * len(value)
    return "•" * (len(value) - 2) + value[-2:]


def mask_email(value: str) -> str:
    """amara.osei@example.invalid -> a•••@example.invalid"""
    if "@" not in value:
        return "•" * len(value)
    local, _, domain = value.partition("@")
    head = local[0] if local else ""
    return f"{head}•••@{domain}"


_MASKERS = {
    "phone": mask_phone,
    "dob": mask_date,
    "address_zip": mask_zip,
    "email": mask_email,
}


# Output masking is deliberately narrower than redaction, and the difference
# matters. A redactor may over-fire — a token in a log costs nothing. A masker
# that over-fires corrupts what the patient reads.
#
# Phone numbers and email addresses have unambiguous shapes and no legitimate
# unmasked use in a reply, so they are masked. Dates and bare five-digit numbers
# are *not*: "September 13, 2026" is an appointment the patient needs, and
# AP-77301 is an appointment id. Masking those would make the assistant unable
# to confirm a booking.
#
# Dates of birth in output are handled by the system prompt, which instructs the
# model to mask them, and by the test that checks it does.
_OUTPUT_MASKS: tuple[tuple[re.Pattern[str], Callable[[str], str]], ...] = (
    (re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"), mask_phone),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), mask_email),
)


def mask_contact_details(text: str) -> str:
    """Mask phone numbers and email addresses the assistant echoes back.

    Defence in depth under the model: the prompt tells it to mask, and this
    makes sure a forgetful turn still cannot print a full number on screen.
    """
    for pattern, masker in _OUTPUT_MASKS:
        text = pattern.sub(_substituter(masker), text)
    return text


def _substituter(masker: Callable[[str], str]) -> Callable[[re.Match[str]], str]:
    """Bind the masker explicitly.

    A bare closure over the loop variable would resolve to whatever it held
    last if the callable ever outlived the iteration.
    """

    def replace(match: re.Match[str]) -> str:
        return masker(match.group(0))

    return replace


def mask(kind: str, value: Any) -> str:
    """Mask by identifier kind. Unknown kinds are fully masked, not passed through."""
    masker = _MASKERS.get(kind)
    if masker is None:
        return "•" * len(str(value))
    return masker(value)
