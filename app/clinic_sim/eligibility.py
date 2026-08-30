"""Eligibility clearinghouse stand-in — implements ``EligibilityGateway``.

Returns active, inactive or indeterminate, and **never a copay** (P1-T7).

That omission is the fixture. Specification §4.9 requires the assistant to
explain the limitation and call escalate_to_staff(reason="billing_issue") when a
patient asks about a copay the function does not provide. If this gateway
returned copay data, that requirement could not be tested — so the field does
not exist here or on ``EligibilityResult``.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.clinic_sim.ehr import SimulatedPatientRepo
from app.clinic_sim.faults import FaultInjector
from app.ports import EligibilityResult, EligibilityStatus

FIXTURES = Path(__file__).parent / "fixtures"
PORT = "EligibilityGateway"


class SimulatedEligibilityGateway:
    def __init__(
        self,
        faults: FaultInjector,
        patients: SimulatedPatientRepo,
        fixture_path: Path | None = None,
    ) -> None:
        self._faults = faults
        self._patients = patients
        path = fixture_path or FIXTURES / "plans.json"
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        self._payers: dict[str, str] = {plan["plan_name"]: plan["payer"] for plan in raw["plans"]}

    def check(self, patient_id: str, service_date: date) -> EligibilityResult:
        fault = self._faults.raise_if_error(PORT, "check")

        plan_name = self._patients.plan_name(patient_id)

        if fault == "ambiguous_response":
            # The payer answered, but not with a usable determination. Spec §4.9
            # requires escalation for manual review rather than a guess.
            return EligibilityResult(
                patient_id=patient_id,
                service_date=service_date,
                status=EligibilityStatus.INDETERMINATE,
                plan_name=plan_name,
                payer=self._payers.get(plan_name or ""),
                checked_at=datetime.now(),
            )

        status = EligibilityStatus(self._patients.eligibility_status(patient_id))
        if plan_name is None:
            status = EligibilityStatus.INDETERMINATE

        return EligibilityResult(
            patient_id=patient_id,
            service_date=service_date,
            status=status,
            plan_name=plan_name,
            payer=self._payers.get(plan_name or ""),
            checked_at=datetime.now(),
        )
