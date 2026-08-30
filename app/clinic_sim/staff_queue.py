"""Staff escalation queue stand-in — implements ``StaffQueue``.

This port has no failure mode and takes no fault injector. "The assistant must
always honor a request to speak with a person" (spec §4.12), so escalation is
the one path that cannot fail: it is the fallback every other failure routes to,
and a fallback that can itself fail is not a fallback.

``SUPPORTED_FAULTS["StaffQueue"]`` is an empty frozenset, so arming a fault here
raises ``UnknownFaultError`` rather than quietly doing nothing.
"""

from __future__ import annotations

from datetime import datetime

from app.ports import EscalationTicket
from app.tools.schemas import EscalationReason, Priority


class SimulatedStaffQueue:
    def __init__(self) -> None:
        self._tickets: list[EscalationTicket] = []
        self._next_id = 5001

    def escalate(
        self,
        reason: EscalationReason,
        priority: Priority,
        notes: str,
        patient_id: str | None = None,
    ) -> EscalationTicket:
        ticket = EscalationTicket(
            ticket_id=f"ESC-{self._next_id}",
            reason=reason,
            priority=priority,
            notes=notes,
            # Attached only when known and appropriate (spec §4.12).
            patient_id=patient_id,
            created_at=datetime.now(),
        )
        self._next_id += 1
        self._tickets.append(ticket)
        return ticket

    def tickets(self) -> list[EscalationTicket]:
        return list(self._tickets)
