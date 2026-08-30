"""Clinic Systems Simulator — deterministic stand-ins for the clinic estate.

Every backend the assistant talks to lives behind a port protocol (AD-07). This
package implements all five with fakes so the prototype exercises the real
policy, tool and audit paths without touching a live clinical system or any real
patient data.

``ClinicSimulator`` wires them together and is the single object the tool layer
depends on in Phase 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.clinic_sim.ehr import SimulatedPatientRepo
from app.clinic_sim.eligibility import SimulatedEligibilityGateway
from app.clinic_sim.faults import FaultInjector
from app.clinic_sim.scheduler import SimulatedScheduleRepo
from app.clinic_sim.sms_outbox import SimulatedMessageGateway
from app.clinic_sim.staff_queue import SimulatedStaffQueue
from app.config import ClinicConfig, get_clinic_config


@dataclass
class ClinicSimulator:
    """The five ports, wired and seeded."""

    faults: FaultInjector
    patients: SimulatedPatientRepo
    schedule: SimulatedScheduleRepo
    eligibility: SimulatedEligibilityGateway
    messages: SimulatedMessageGateway
    staff: SimulatedStaffQueue
    clinic: ClinicConfig

    @classmethod
    def build(
        cls,
        clinic: ClinicConfig | None = None,
        today: date | None = None,
        seed: int = 20260830,
    ) -> ClinicSimulator:
        clinic = clinic or get_clinic_config()
        faults = FaultInjector()
        patients = SimulatedPatientRepo(faults)
        return cls(
            faults=faults,
            patients=patients,
            schedule=SimulatedScheduleRepo(faults, clinic, today=today, seed=seed),
            eligibility=SimulatedEligibilityGateway(faults, patients),
            messages=SimulatedMessageGateway(faults),
            staff=SimulatedStaffQueue(),
            clinic=clinic,
        )

    def reset_faults(self) -> None:
        self.faults.clear()


__all__ = [
    "ClinicSimulator",
    "FaultInjector",
    "SimulatedEligibilityGateway",
    "SimulatedMessageGateway",
    "SimulatedPatientRepo",
    "SimulatedScheduleRepo",
    "SimulatedStaffQueue",
]
