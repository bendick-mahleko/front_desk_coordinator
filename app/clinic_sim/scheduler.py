"""In-memory scheduler stand-in — implements ``ScheduleRepo``.

The slot grid is generated deterministically from a seed rather than stored, so
it rolls forward with the calendar while staying reproducible: the same seed and
the same ``today`` always produce the same availability, which is what makes an
eval that passes today pass tomorrow.
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from app.clinic_sim.faults import FaultInjector
from app.config import ClinicConfig
from app.ports import (
    Appointment,
    AppointmentStatus,
    BackendError,
    CancellationResult,
    Slot,
)
from app.tools.schemas import AppointmentType, Modality

FIXTURES = Path(__file__).parent / "fixtures"
PORT = "ScheduleRepo"

SLOT_TIMES: tuple[time, ...] = (
    time(9, 0),
    time(9, 30),
    time(10, 0),
    time(10, 30),
    time(11, 0),
    time(13, 30),
    time(14, 0),
    time(14, 30),
    time(15, 0),
    time(15, 30),
    time(16, 0),
)

HORIZON_DAYS = 30

# Which visit types a slot can host. A new-patient visit needs the patient in
# the room; a telehealth visit needs a telehealth slot.
_IN_PERSON_ONLY = frozenset({AppointmentType.NEW_PATIENT})
_TELEHEALTH_ONLY = frozenset({AppointmentType.TELEHEALTH})


class SimulatedScheduleRepo:
    def __init__(
        self,
        faults: FaultInjector,
        clinic: ClinicConfig,
        today: date | None = None,
        seed: int = 20260830,
        fixture_path: Path | None = None,
    ) -> None:
        self._faults = faults
        self._clinic = clinic
        self._today = today or date.today()
        self._seed = seed
        self._providers = list(clinic.providers)
        self._holidays = set(clinic.holidays)

        path = fixture_path or FIXTURES / "appointments.json"
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        self._appointments: dict[str, Appointment] = {}
        for record in raw["appointments"]:
            appointment = Appointment(
                appointment_id=record["appointment_id"],
                patient_id=record["patient_id"],
                appointment_date=self._today + timedelta(days=record["days_from_today"]),
                appointment_time=time.fromisoformat(record["time"]),
                provider=record["provider"],
                appointment_type=AppointmentType(record["appointment_type"]),
                modality=Modality(record["modality"]),
                reason_for_visit=record["reason_for_visit"],
                status=AppointmentStatus(record["status"]),
            )
            self._appointments[appointment.appointment_id] = appointment

        self._next_appointment = 77400
        self._booked: dict[str, Appointment] = {}
        self._cancellations: dict[str, CancellationResult] = {}
        self._slots: dict[str, Slot] = {}
        self._taken: set[str] = set()
        self._build_grid()

    # ------------------------------------------------------------- grid ---

    def _is_open(self, day: date) -> bool:
        if day in self._holidays:
            return False
        weekday = day.strftime("%A").lower()
        hours = self._clinic.hours.get(weekday)
        return hours is not None and not hours.is_closed

    def _build_grid(self) -> None:
        rng = random.Random(self._seed)
        for offset in range(HORIZON_DAYS):
            day = self._today + timedelta(days=offset)
            if not self._is_open(day):
                continue
            for provider_index, provider in enumerate(self._providers):
                for time_index, slot_time in enumerate(SLOT_TIMES):
                    slot_id = f"SL-{day.isoformat()}-{provider_index}-{time_index}"
                    modality = Modality.TELEHEALTH if rng.random() < 0.3 else Modality.IN_PERSON
                    self._slots[slot_id] = Slot(
                        slot_id=slot_id,
                        slot_date=day,
                        slot_time=slot_time,
                        provider=provider,
                        modality=modality,
                    )
                    if rng.random() < 0.35:
                        self._taken.add(slot_id)

        # Seeded appointments occupy their slots, so a search cannot offer a
        # time the patient already holds.
        for appointment in self._appointments.values():
            if appointment.status is AppointmentStatus.SCHEDULED:
                self._occupy(appointment.appointment_date, appointment.appointment_time)

    def _occupy(self, day: date, at: time) -> None:
        for slot in self._slots.values():
            if slot.slot_date == day and slot.slot_time == at:
                self._taken.add(slot.slot_id)

    @staticmethod
    def _slot_supports(slot: Slot, appointment_type: AppointmentType) -> bool:
        if appointment_type in _IN_PERSON_ONLY:
            return slot.modality is Modality.IN_PERSON
        if appointment_type in _TELEHEALTH_ONLY:
            return slot.modality is Modality.TELEHEALTH
        return True

    def slot_by_id(self, slot_id: str) -> Slot | None:
        return self._slots.get(slot_id)

    # -------------------------------------------------------------- ports ---

    def get_appointments(self, patient_id: str) -> list[Appointment]:
        self._faults.raise_if_error(PORT, "get_appointments")
        everything = {**self._appointments, **self._booked}
        return sorted(
            (
                appointment
                for appointment in everything.values()
                if appointment.patient_id == patient_id
            ),
            key=lambda item: (item.appointment_date, item.appointment_time),
        )

    def search_slots(
        self,
        appointment_type: AppointmentType,
        date_range_start: date,
        date_range_end: date,
        modality: Modality,
        preferred_provider: str | None = None,
        morning_only: bool | None = None,
        limit: int = 3,
    ) -> list[Slot]:
        self._faults.raise_if_error(PORT, "search_slots")

        candidates = [
            slot
            for slot in self._slots.values()
            if slot.slot_id not in self._taken
            and date_range_start <= slot.slot_date <= date_range_end
            and self._slot_supports(slot, appointment_type)
            and (modality is Modality.ANY or slot.modality is modality)
            and (preferred_provider is None or slot.provider == preferred_provider)
            and (morning_only is None or slot.is_morning == morning_only)
        ]
        candidates.sort(key=lambda slot: (slot.slot_date, slot.slot_time, slot.provider))
        # "Present a limited number of suitable choices at once" (spec §4.5).
        return candidates[:limit]

    def book(
        self,
        patient_id: str,
        appointment_date: date,
        appointment_time: time,
        reason_for_visit: str,
        provider: str | None = None,
        appointment_type: AppointmentType | None = None,
        modality: Modality | None = None,
        idempotency_key: str | None = None,
    ) -> Appointment:
        self._faults.raise_if_error(PORT, "book")

        if idempotency_key and idempotency_key in self._booked:
            return self._booked[idempotency_key]

        matching = [
            slot
            for slot in self._slots.values()
            if slot.slot_date == appointment_date
            and slot.slot_time == appointment_time
            and (provider is None or slot.provider == provider)
        ]
        free = [slot for slot in matching if slot.slot_id not in self._taken]
        if not matching or not free:
            # spec §4.6 — explain and return to search; never claim a booking.
            raise BackendError("slot_unavailable", "that appointment time is no longer available")

        clash = [
            appointment
            for appointment in self.get_appointments(patient_id)
            if appointment.status is AppointmentStatus.SCHEDULED
            and appointment.appointment_date == appointment_date
            and appointment.appointment_time == appointment_time
        ]
        if clash:
            raise BackendError(
                "double_booking", "the patient already holds an appointment at that time"
            )

        slot = free[0]
        appointment = Appointment(
            appointment_id=f"AP-{self._next_appointment}",
            patient_id=patient_id,
            appointment_date=appointment_date,
            appointment_time=appointment_time,
            provider=slot.provider,
            appointment_type=appointment_type or AppointmentType.FOLLOW_UP,
            modality=modality if modality and modality is not Modality.ANY else slot.modality,
            reason_for_visit=reason_for_visit,
        )
        self._next_appointment += 1
        self._taken.add(slot.slot_id)
        self._appointments[appointment.appointment_id] = appointment
        if idempotency_key:
            self._booked[idempotency_key] = appointment
        return appointment

    def cancel(
        self,
        patient_id: str,
        appointment_id: str,
        cancellation_reason: str,
        idempotency_key: str | None = None,
    ) -> CancellationResult:
        self._faults.raise_if_error(PORT, "cancel")

        if idempotency_key and idempotency_key in self._cancellations:
            return self._cancellations[idempotency_key]

        appointment = self._appointments.get(appointment_id)
        if appointment is None or appointment.patient_id != patient_id:
            # Not revealing whether the ID exists for someone else (spec §3).
            raise BackendError("appointment_not_found", "no such appointment for this patient")

        window = timedelta(hours=self._clinic.policy.late_cancellation_hours)
        starts_at = datetime.combine(appointment.appointment_date, appointment.appointment_time)
        late = starts_at - datetime.now() < window

        self._appointments[appointment_id] = appointment.model_copy(
            update={"status": AppointmentStatus.CANCELLED}
        )
        self._release(appointment.appointment_date, appointment.appointment_time)

        result = CancellationResult(
            appointment_id=appointment_id,
            cancelled_at=datetime.now(),
            late_cancellation=late,
        )
        if idempotency_key:
            self._cancellations[idempotency_key] = result
        return result

    def _release(self, day: date, at: time) -> None:
        for slot in self._slots.values():
            if slot.slot_date == day and slot.slot_time == at:
                self._taken.discard(slot.slot_id)

    def reschedule(
        self,
        patient_id: str,
        current_appointment_id: str,
        new_slot_id: str,
        reschedule_reason: str,
        idempotency_key: str | None = None,
    ) -> Appointment:
        self._faults.raise_if_error(PORT, "reschedule")

        if idempotency_key and idempotency_key in self._booked:
            return self._booked[idempotency_key]

        current = self._appointments.get(current_appointment_id)
        if current is None or current.patient_id != patient_id:
            raise BackendError("appointment_not_found", "no such appointment for this patient")

        slot = self._slots.get(new_slot_id)
        if slot is None or new_slot_id in self._taken:
            raise BackendError("slot_unavailable", "that slot is no longer available")

        # One atomic move. The old appointment is never cancelled separately
        # (spec §4.8) — that would leave the patient with nothing if the second
        # half failed.
        moved = current.model_copy(
            update={
                "appointment_date": slot.slot_date,
                "appointment_time": slot.slot_time,
                "provider": slot.provider,
                "modality": slot.modality,
            }
        )
        self._appointments[current_appointment_id] = moved
        self._taken.add(new_slot_id)
        self._release(current.appointment_date, current.appointment_time)
        if idempotency_key:
            self._booked[idempotency_key] = moved
        return moved
