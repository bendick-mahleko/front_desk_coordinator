"""Date normalisation, in clinic time.

Specification §4.1 requires a natural-language birth date to be normalised to
YYYY-MM-DD before calling, and §4.5 requires "next week" to become explicit
dates before a search. Both happen here, before an argument model ever sees
the value.

Two separate entry points, because the direction of an ambiguous date depends
on what it is:

* a **birth date** is always in the past — "March 4th" cannot mean next March;
* an **appointment date** is always in the future — "Tuesday" means the coming
  Tuesday, not the one just gone.

Collapsing these into one function gets one of the two cases wrong every time.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import dateparser

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_BASE_SETTINGS: dict[str, object] = {
    "DATE_ORDER": "MDY",  # en-US only in the prototype
    "STRICT_PARSING": False,
    "RETURN_AS_TIMEZONE_AWARE": False,
}


def clinic_now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz).replace(tzinfo=None)


def clinic_today(tz: ZoneInfo) -> date:
    return clinic_now(tz).date()


def _parse(text: str, tz: ZoneInfo, prefer: str, today: date | None) -> date | None:
    text = text.strip()
    if not text:
        return None

    # An already-normalised date needs no interpretation, and running it through
    # a fuzzy parser risks it coming back as something else.
    if ISO_DATE.match(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None

    base = datetime.combine(today, datetime.min.time()) if today else clinic_now(tz)
    settings = {**_BASE_SETTINGS, "PREFER_DATES_FROM": prefer, "RELATIVE_BASE": base}
    parsed = dateparser.parse(text, languages=["en"], settings=settings)
    return parsed.date() if parsed else None


def normalise_birth_date(text: str, tz: ZoneInfo, today: date | None = None) -> date | None:
    """Normalise a spoken birth date. Never resolves into the future."""
    parsed = _parse(text, tz, prefer="past", today=today)
    if parsed is None:
        return None
    reference = today or clinic_today(tz)
    if parsed > reference:
        return None
    return parsed


def normalise_appointment_date(text: str, tz: ZoneInfo, today: date | None = None) -> date | None:
    """Normalise a requested appointment date. Resolves forward."""
    return _parse(text, tz, prefer="future", today=today)


def resolve_named_range(
    text: str, tz: ZoneInfo, today: date | None = None
) -> tuple[date, date] | None:
    """Turn a named period into explicit start and end dates.

    Handles the phrases patients actually use for scheduling. Anything else
    returns None, and the assistant asks rather than guessing at a range
    (spec §4.5).
    """
    reference = today or clinic_today(tz)
    phrase = " ".join(text.lower().split())

    monday = reference - timedelta(days=reference.weekday())

    if phrase in {"today"}:
        return reference, reference
    if phrase in {"tomorrow"}:
        return reference + timedelta(days=1), reference + timedelta(days=1)
    if phrase in {"this week", "the rest of this week"}:
        return reference, monday + timedelta(days=6)
    if phrase in {"next week"}:
        start = monday + timedelta(days=7)
        return start, start + timedelta(days=6)
    if phrase in {"the week after next"}:
        start = monday + timedelta(days=14)
        return start, start + timedelta(days=6)
    if phrase in {"this weekend"}:
        return monday + timedelta(days=5), monday + timedelta(days=6)
    if phrase in {"next month"}:
        first = (reference.replace(day=1) + timedelta(days=32)).replace(day=1)
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return first, last
    if phrase in {"next 2 weeks", "next two weeks", "the next two weeks"}:
        return reference, reference + timedelta(days=13)
    if phrase in {"next 30 days", "the next 30 days", "next month or so"}:
        return reference, reference + timedelta(days=30)
    return None


def to_iso(value: date) -> str:
    """Canonical wire format everywhere in the system (spec §6)."""
    return value.isoformat()
