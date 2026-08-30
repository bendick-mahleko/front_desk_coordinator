"""SMS gateway stand-in — implements ``MessageGateway``.

Nothing leaves the machine. Messages are appended to an in-memory outbox the UI
renders, which is the demo surface for specification §4.10.

Delivery status is modelled rather than assumed: a real gateway sometimes
cannot confirm delivery, and §4.10 requires the assistant to say so when that
happens. A simulator that always reported success would make that rule dead
code.
"""

from __future__ import annotations

from datetime import datetime

from app.clinic_sim.faults import FaultInjector
from app.ports import DeliveryStatus, MessageReceipt
from app.tools.schemas import MessageType

PORT = "MessageGateway"


class SimulatedMessageGateway:
    def __init__(self, faults: FaultInjector) -> None:
        self._faults = faults
        self._outbox: list[MessageReceipt] = []
        self._sent: dict[str, MessageReceipt] = {}
        self._next_id = 9001

    def send(
        self,
        phone_number: str,
        message_type: MessageType,
        appointment_details: str | None = None,
        idempotency_key: str | None = None,
    ) -> MessageReceipt:
        fault = self._faults.raise_if_error(PORT, "send")

        # Text delivery is idempotent (spec §6): a retry must not send twice.
        if idempotency_key and idempotency_key in self._sent:
            return self._sent[idempotency_key]

        status = (
            DeliveryStatus.UNCONFIRMED
            if fault == "delivery_unconfirmed"
            else DeliveryStatus.DELIVERED
        )
        receipt = MessageReceipt(
            message_id=f"MSG-{self._next_id}",
            phone_number=phone_number,
            message_type=message_type,
            delivery_status=status,
            sent_at=datetime.now(),
        )
        self._next_id += 1
        self._outbox.append(receipt)
        if idempotency_key:
            self._sent[idempotency_key] = receipt
        return receipt

    def outbox(self) -> list[MessageReceipt]:
        return list(self._outbox)
