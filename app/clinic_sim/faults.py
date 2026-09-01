"""Deterministic fault injection for the clinic simulator.

Specification §6 requires the assistant to handle backend errors, ambiguous
results and unconfirmed delivery. Those paths are only testable if a failure can
be *asked for* — random flakiness would make the eval suite non-deterministic
and the failures unreproducible.

A scenario arms a fault; the next matching call consumes it.

Two kinds of fault, because not every failure is an exception:

* **error faults** raise ``BackendError`` — the backend could not answer.
* **outcome faults** force a specific *valid* result — two matches rather than
  one, a delivery the gateway will not confirm. These are business outcomes the
  assistant must handle, not errors.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from app.ports import BackendError

SUPPORTED_FAULTS: dict[str, frozenset[str]] = {
    "PatientRepo": frozenset({"multiple_match", "not_found", "upstream_timeout"}),
    "ScheduleRepo": frozenset({"slot_unavailable", "double_booking", "appointment_not_found"}),
    "EligibilityGateway": frozenset({"payer_unavailable", "ambiguous_response", "rejected"}),
    "MessageGateway": frozenset({"delivery_unconfirmed", "invalid_number", "send_failed"}),
    # Escalation must always succeed (spec §4.12), so no fault may be armed on
    # it. The empty set is the enforcement, not a comment.
    "StaffQueue": frozenset(),
    # spec §4.13 — "On expiry or failure, drop to the system role." An outage is
    # a failure, and it must be reachable in a test, or the branch that
    # distinguishes "the directory is down" from "you are not a clinician" is
    # never exercised.
    "IdentityProvider": frozenset({"directory_unavailable"}),
}

ERROR_FAULTS: frozenset[str] = frozenset(
    {
        "upstream_timeout",
        "appointment_not_found",
        "slot_unavailable",
        "double_booking",
        "payer_unavailable",
        "rejected",
        "invalid_number",
        "send_failed",
        "directory_unavailable",
    }
)
"""Faults that raise. Everything else in SUPPORTED_FAULTS shapes a valid result."""

FAULT_MESSAGES: dict[str, str] = {
    "upstream_timeout": "the patient record system did not respond in time",
    "appointment_not_found": "no such appointment for this patient",
    "slot_unavailable": "that appointment time is no longer available",
    "double_booking": "the patient already holds an appointment at that time",
    "payer_unavailable": "the payer did not respond to the eligibility request",
    "rejected": "the payer rejected the eligibility request",
    "invalid_number": "the destination number was rejected by the gateway",
    "send_failed": "the message gateway refused the send",
    "directory_unavailable": "the clinic's identity provider did not respond",
}


class UnknownFaultError(ValueError):
    """A scenario armed a fault the port cannot produce.

    Raised loudly so a typo in a scenario file fails the test rather than
    silently arming nothing and passing.
    """


@dataclass
class _ArmedFault:
    code: str
    once: bool
    fired: bool = False


@dataclass
class FaultInjector:
    """Holds armed faults. One instance per simulator; cleared between tests."""

    _armed: dict[tuple[str, str], _ArmedFault] = field(default_factory=dict)

    def arm(self, port: str, operation: str, code: str, *, once: bool = True) -> None:
        """Arm ``code`` on the next call to ``port.operation``."""
        supported = SUPPORTED_FAULTS.get(port)
        if supported is None:
            raise UnknownFaultError(
                f"unknown port {port!r}; expected one of {sorted(SUPPORTED_FAULTS)}"
            )
        if code not in supported:
            available = sorted(supported) or "none — this port may not fail"
            raise UnknownFaultError(f"{port} cannot produce fault {code!r}. Available: {available}")
        self._armed[(port, operation)] = _ArmedFault(code=code, once=once)

    def take(self, port: str, operation: str) -> str | None:
        """Consume an armed fault for this call, if any."""
        armed = self._armed.get((port, operation))
        if armed is None:
            return None
        if armed.once:
            if armed.fired:
                return None
            armed.fired = True
        return armed.code

    def raise_if_error(self, port: str, operation: str) -> str | None:
        """Take a fault; raise it if it is an error fault, else return the code.

        The common shape in the simulator: ``code = faults.raise_if_error(...)``
        then branch on the remaining outcome codes.
        """
        code = self.take(port, operation)
        if code is None:
            return None
        if code in ERROR_FAULTS:
            raise BackendError(code, FAULT_MESSAGES.get(code, code))
        return code

    def clear(self) -> None:
        self._armed.clear()

    @property
    def armed(self) -> dict[tuple[str, str], str]:
        return {key: value.code for key, value in self._armed.items()}

    @contextmanager
    def armed_with(
        self, port: str, operation: str, code: str, *, once: bool = True
    ) -> Iterator[None]:
        """Arm a fault for the duration of a block, then clear it."""
        self.arm(port, operation, code, once=once)
        try:
            yield
        finally:
            self._armed.pop((port, operation), None)


def all_fault_codes() -> set[str]:
    """Every failure mode any port can produce. Used by the coverage test."""
    return {code for codes in SUPPORTED_FAULTS.values() for code in codes}
