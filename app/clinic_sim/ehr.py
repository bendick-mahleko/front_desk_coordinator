"""In-memory EHR stand-in — implements ``PatientRepo``.

Deterministic and seeded. Loads the patient fixture once and answers from it.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.clinic_sim.faults import FaultInjector
from app.ports import (
    BackendError,
    PatientDemographics,
    PatientLookupResult,
    RegistrationResult,
    VerificationResult,
)
from app.tools.schemas import IdentifierType, normalise_phone

FIXTURES = Path(__file__).parent / "fixtures"
PORT = "PatientRepo"


class SimulatedPatientRepo:
    """A patient index that answers lookups, verification and registration."""

    def __init__(self, faults: FaultInjector, fixture_path: Path | None = None) -> None:
        self._faults = faults
        path = fixture_path or FIXTURES / "patients.json"
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        self._patients: dict[str, dict[str, Any]] = {
            record["patient_id"]: record for record in raw["patients"]
        }
        self._next_id = 4900
        self._registrations: dict[str, RegistrationResult] = {}

    # ------------------------------------------------------------ helpers ---

    def _record(self, patient_id: str) -> dict[str, Any]:
        record = self._patients.get(patient_id)
        if record is None:
            raise BackendError("not_found", f"no patient record for {patient_id}")
        return record

    def eligibility_status(self, patient_id: str) -> str:
        return str(self._record(patient_id)["eligibility_status"])

    def plan_name(self, patient_id: str) -> str | None:
        value = self._record(patient_id)["insurance_plan_name"]
        return str(value) if value else None

    def phone_number(self, patient_id: str) -> str:
        return str(self._record(patient_id)["phone_number"])

    def all_ids(self) -> list[str]:
        return list(self._patients)

    # --------------------------------------------------------------- ports ---

    def check_exists(
        self, first_name: str, last_name: str, date_of_birth: date
    ) -> PatientLookupResult:
        fault = self._faults.raise_if_error(PORT, "check_exists")
        if fault == "not_found":
            return PatientLookupResult(match_count=0)
        if fault == "multiple_match":
            return PatientLookupResult(match_count=2)

        matches = [
            record
            for record in self._patients.values()
            if record["first_name"].casefold() == first_name.casefold()
            and record["last_name"].casefold() == last_name.casefold()
            and date.fromisoformat(record["date_of_birth"]) == date_of_birth
        ]
        if len(matches) == 1:
            return PatientLookupResult(match_count=1, patient_id=matches[0]["patient_id"])
        # More than one match must not select a record, and zero must not
        # invent one (spec §4.1).
        return PatientLookupResult(match_count=len(matches))

    def verify_identity(
        self, patient_id: str, identifiers: dict[IdentifierType, str]
    ) -> VerificationResult:
        self._faults.raise_if_error(PORT, "verify_identity")
        record = self._record(patient_id)

        verified = all(
            self._identifier_matches(record, kind, value) for kind, value in identifiers.items()
        )
        return VerificationResult(
            verified=verified,
            patient_id=patient_id,
            methods=tuple(identifiers),
            checked_at=datetime.now(),
        )

    @staticmethod
    def _identifier_matches(record: dict[str, Any], kind: IdentifierType, value: str) -> bool:
        candidate = value.strip()
        if kind is IdentifierType.DOB:
            try:
                return date.fromisoformat(candidate) == date.fromisoformat(record["date_of_birth"])
            except ValueError:
                return False
        if kind is IdentifierType.PHONE:
            try:
                return normalise_phone(candidate) == record["phone_number"]
            except ValueError:
                return False
        if kind is IdentifierType.ADDRESS_ZIP:
            return candidate == record["address_zip"]
        return False

    def get_demographics(self, patient_id: str) -> PatientDemographics:
        self._faults.raise_if_error(PORT, "get_demographics")
        record = self._record(patient_id)
        return PatientDemographics(
            patient_id=record["patient_id"],
            first_name=record["first_name"],
            last_name=record["last_name"],
            date_of_birth=date.fromisoformat(record["date_of_birth"]),
            phone_number=record["phone_number"],
            email=record["email"],
            address_line=record["address_line"],
            city=record["city"],
            state=record["state"],
            address_zip=record["address_zip"],
            insurance_plan_name=record["insurance_plan_name"],
        )

    def create_record(
        self,
        first_name: str,
        last_name: str,
        date_of_birth: date,
        phone_number: str,
        email: str | None = None,
        insurance_plan_name: str | None = None,
        idempotency_key: str | None = None,
    ) -> RegistrationResult:
        self._faults.raise_if_error(PORT, "create_record")

        # Registration is idempotent (spec §6): a retried submission returns the
        # original record rather than creating a second one.
        if idempotency_key and idempotency_key in self._registrations:
            return self._registrations[idempotency_key]

        existing = self.check_exists(first_name, last_name, date_of_birth)
        duplicate = existing.match_count > 0

        patient_id = f"PT-{self._next_id}"
        self._next_id += 1
        normalised = normalise_phone(phone_number)
        self._patients[patient_id] = {
            "patient_id": patient_id,
            "first_name": first_name,
            "last_name": last_name,
            "date_of_birth": date_of_birth.isoformat(),
            "phone_number": normalised,
            "email": email,
            "address_line": "",
            "city": "",
            "state": "",
            "address_zip": "",
            "insurance_plan_name": insurance_plan_name,
            "eligibility_status": "indeterminate",
        }
        result = RegistrationResult(
            patient_id=patient_id,
            created_at=datetime.now(),
            duplicate_suspected=duplicate,
        )
        if idempotency_key:
            self._registrations[idempotency_key] = result
        return result
