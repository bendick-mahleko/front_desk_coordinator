"""P1-T8 — date normalisation in clinic time."""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from app.util.dates import (
    normalise_appointment_date,
    normalise_birth_date,
    resolve_named_range,
    to_iso,
)

TZ = ZoneInfo("America/New_York")
TODAY = date(2026, 9, 7)  # a Monday


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1978-03-04", date(1978, 3, 4)),
        ("March 4, 1978", date(1978, 3, 4)),
        ("4 March 1978", date(1978, 3, 4)),
        ("03/04/1978", date(1978, 3, 4)),  # MDY — en-US only in the prototype
    ],
)
def test_birth_dates_normalise_to_iso(text, expected):
    """spec §4.1 — normalise before calling."""
    assert normalise_birth_date(text, TZ, today=TODAY) == expected


def test_a_birth_date_never_resolves_into_the_future():
    """ "March 4th" cannot mean next March when it is a date of birth."""
    parsed = normalise_birth_date("March 4", TZ, today=TODAY)
    assert parsed is None or parsed <= TODAY


@pytest.mark.parametrize("text", ["", "   ", "sometime in the 80s", "banana"])
def test_unparseable_birth_dates_return_none(text):
    """None means ask the patient — never guess at a date of birth."""
    assert normalise_birth_date(text, TZ, today=TODAY) is None


def test_appointment_dates_resolve_forward():
    """ "Tuesday" means the coming Tuesday, not the one just gone."""
    parsed = normalise_appointment_date("Tuesday", TZ, today=TODAY)
    assert parsed is not None
    assert parsed >= TODAY
    assert parsed.strftime("%A") == "Tuesday"


def test_appointment_date_accepts_iso_unchanged():
    assert normalise_appointment_date("2026-09-15", TZ, today=TODAY) == date(2026, 9, 15)


def test_the_two_directions_disagree_on_purpose():
    """The same text resolves differently depending on what it is."""
    birth = normalise_birth_date("September 20", TZ, today=TODAY)
    appointment = normalise_appointment_date("September 20", TZ, today=TODAY)

    assert appointment == date(2026, 9, 20)
    assert birth is None or birth < TODAY


@pytest.mark.parametrize(
    "phrase,start,end",
    [
        ("today", date(2026, 9, 7), date(2026, 9, 7)),
        ("tomorrow", date(2026, 9, 8), date(2026, 9, 8)),
        ("this week", date(2026, 9, 7), date(2026, 9, 13)),
        ("next week", date(2026, 9, 14), date(2026, 9, 20)),
        ("the week after next", date(2026, 9, 21), date(2026, 9, 27)),
        ("this weekend", date(2026, 9, 12), date(2026, 9, 13)),
        ("next month", date(2026, 10, 1), date(2026, 10, 31)),
        ("next two weeks", date(2026, 9, 7), date(2026, 9, 20)),
    ],
)
def test_named_ranges_become_explicit_dates(phrase, start, end):
    """spec §4.5 — convert "next week" before the call, not during it."""
    assert resolve_named_range(phrase, TZ, today=TODAY) == (start, end)


def test_named_ranges_are_whitespace_and_case_insensitive():
    assert resolve_named_range("  NEXT   Week ", TZ, today=TODAY) == (
        date(2026, 9, 14),
        date(2026, 9, 20),
    )


@pytest.mark.parametrize("phrase", ["soon", "whenever", "in a bit", "next Thursday-ish"])
def test_unknown_ranges_return_none_rather_than_guessing(phrase):
    assert resolve_named_range(phrase, TZ, today=TODAY) is None


def test_named_ranges_are_ordered():
    for phrase in ["this week", "next week", "next month", "next two weeks"]:
        start, end = resolve_named_range(phrase, TZ, today=TODAY)
        assert start <= end, phrase


def test_to_iso_is_the_wire_format():
    assert to_iso(date(2026, 9, 7)) == "2026-09-07"
