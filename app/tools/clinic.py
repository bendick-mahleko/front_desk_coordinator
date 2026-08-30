"""Clinic hours and directions — specification §4.11.

Everything here is read from configuration or the clinic knowledge base. None
of it is composed by the model: an invented address or a guessed accessibility
detail is as harmful as an invented appointment.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.config import get_clinic_config
from app.tools.registry import tool
from app.tools.schemas import Location


def _hours_for(day: date) -> dict[str, Any]:
    clinic = get_clinic_config()
    if day in clinic.holidays:
        return {"date": day.isoformat(), "open": False, "reason": "holiday"}

    weekday = day.strftime("%A").lower()
    hours = clinic.hours[weekday]
    if hours.is_closed:
        return {"date": day.isoformat(), "open": False, "reason": "closed that day"}
    return {
        "date": day.isoformat(),
        "weekday": weekday,
        "open": True,
        "opens": hours.open,
        "closes": hours.close,
    }


@tool("get_clinic_hours")
def get_clinic_hours(date: date) -> Any:
    """Return the clinic's opening hours for a specific date.

    Use this for a future date, a weekend, a holiday or a named day. For "are
    you open right now?", use check_business_hours instead.
    """
    return _hours_for(date)


@tool("check_business_hours")
def check_business_hours() -> Any:
    """Report whether the clinic is open at this moment.

    This is the correct function for "are you open now?" — it accounts for the
    current time in the clinic's own timezone, which a hours lookup does not.
    """
    clinic = get_clinic_config()
    now = datetime.now(clinic.tz)
    today = _hours_for(now.date())

    if not today["open"]:
        return {**today, "open_now": False, "local_time": now.strftime("%H:%M")}

    opens = datetime.strptime(today["opens"], "%H:%M").time()
    closes = datetime.strptime(today["closes"], "%H:%M").time()
    return {
        **today,
        "open_now": opens <= now.time() <= closes,
        "local_time": now.strftime("%H:%M"),
    }


@tool("get_clinic_directions")
def get_clinic_directions(location: Location) -> Any:
    """Return the address, parking and accessibility information for a clinic site.

    Only two locations exist: main_clinic and satellite_office. If a patient
    uses an informal name, only map it to one of these if that mapping is
    configured — otherwise ask which site they mean.

    Read back only what this returns. Do not add directions, landmarks or
    accessibility details from general knowledge.
    """
    clinic = get_clinic_config()
    site = clinic.locations[location.value]
    return {
        "location": location.value,
        "name": site.name,
        "address": site.address,
        "parking": site.parking,
        "accessibility": site.accessibility,
    }
