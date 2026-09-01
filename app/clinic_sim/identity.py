"""In-memory identity provider stand-in — implements ``IdentityProvider``.

Answers one question: does this staff id plus this credential token correspond
to somebody in the clinic's directory, and if so, what does the directory say
about them (spec §3.2).

Three things it deliberately does not do.

It does not decide. Whether a shared account or a non-clinical role may hold a
Clinical Assistant session is clinic policy, and it lives in the tool layer
where the clinic's configuration is in scope. A directory reports; it does not
authorize.

It does not distinguish an unknown staff id from a wrong token. Both return
None. A caller who could tell them apart could enumerate the clinic's staff one
guess at a time, and "no such staff id" is exactly the disclosure §3.1 rule 5
forbids in the patient case for the same reason.

It does not keep the token. The comparison is constant-time and the string is
never stored, returned, or logged — ``StaffAssertion`` has no field that could
carry it.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from app.clinic_sim.faults import FaultInjector
from app.ports import StaffAssertion
from app.tools.schemas import ClinicalRole

FIXTURES = Path(__file__).parent / "fixtures"
PORT = "IdentityProvider"


class SimulatedIdentityProvider:
    """A staff directory that answers authentication requests."""

    def __init__(self, faults: FaultInjector, fixture_path: Path | None = None) -> None:
        self._faults = faults
        path = fixture_path or FIXTURES / "staff.json"
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        self._staff: dict[str, dict[str, Any]] = {
            record["staff_id"]: record
            for record in raw["staff"]
            if not str(record.get("staff_id", "")).startswith("_")
        }

    def directory_size(self) -> int:
        return len(self._staff)

    def authenticate(self, staff_id: str, credential_token: str) -> StaffAssertion | None:
        """Check a credential against the directory.

        A directory outage raises ``BackendError`` like any other port. §4.13
        says an authentication *failure* drops to the system role, and an outage
        is a failure — so the caller must not be able to mistake "the directory
        is down" for "this person is not a clinician".
        """
        self._faults.raise_if_error(PORT, "authenticate")

        record = self._staff.get(staff_id)
        if record is None:
            # No such staff id. Indistinguishable from a bad token, on purpose.
            return None

        expected = str(record["credential_token"])
        if not secrets.compare_digest(expected, credential_token):
            # Constant-time so the comparison cannot be turned into an oracle by
            # timing it. Overkill against a fixture; correct against a real one,
            # and this is the code an adapter will be read against.
            return None

        role = record["role"]
        return StaffAssertion(
            staff_id=record["staff_id"],
            display_name=str(record["display_name"]),
            role=ClinicalRole(role) if role else None,
            shared_account=bool(record["shared_account"]),
            credential_expired=bool(record["credential_expired"]),
            department=record.get("department"),
        )
