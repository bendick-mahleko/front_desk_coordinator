"""Conversation session state.

Everything the policy gate needs to make a decision lives here, and nothing
else does. The constraint is deliberate: if a fact is not on this object, no
rule may depend on it, which keeps the set of things that can affect an
authorization decision small enough to hold in your head.

No raw identifier value is ever stored. ``verify_methods`` holds type names,
``attempt_digests`` holds salted hashes. Specification §4.2 permits the result,
the timestamp and the method — this model can hold nothing more.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, date, datetime, time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.tools.schemas import IdentifierType


class SubjectStatus(StrEnum):
    """Who the assistant is talking to, as far as it has established."""

    NONE = "none"
    """Nobody identified yet."""

    IDENTIFIED = "identified"
    """check_patient_exists returned exactly one match. A patient_id is known,
    but nothing protected may be disclosed."""

    VERIFIED = "verified"
    """Two distinct identifiers accepted."""

    REGISTERED = "registered"
    """Created during this session.

    Its own state rather than a flavour of VERIFIED. Specification §3 lets a
    newly registered patient book without identity verification — they supplied
    every field themselves, so there is nothing to verify against. But folding
    that into VERIFIED would grant demographics and appointment history over a
    record that may yet turn out to be a duplicate. REGISTERED confers booking
    rights, over one patient_id, and nothing else.
    """

    LOCKED = "locked"
    """Verification attempt limit reached. Escalation is the only exit."""


class GateLevel(StrEnum):
    """Authorization levels. Three are ordered; one is not."""

    OPEN = "open"
    IDENTIFIED = "identified"
    VERIFIED = "verified"
    NUMBER_CONFIRMED = "number_confirmed"
    """Orthogonal to the ladder. Specification §4.10 lets directions go to a
    number the patient confirms as their own, with no verification at all —
    that is not a rung below VERIFIED, it is a different axis."""


LADDER: dict[GateLevel, int] = {
    GateLevel.OPEN: 0,
    GateLevel.IDENTIFIED: 1,
    GateLevel.VERIFIED: 2,
}

_STATUS_LEVEL: dict[SubjectStatus, GateLevel] = {
    SubjectStatus.NONE: GateLevel.OPEN,
    SubjectStatus.IDENTIFIED: GateLevel.IDENTIFIED,
    SubjectStatus.VERIFIED: GateLevel.VERIFIED,
    # Registration grants booking rights through Policy.accepts, not through the
    # ladder — so it must not read as VERIFIED here.
    SubjectStatus.REGISTERED: GateLevel.IDENTIFIED,
    # A locked session holds a patient_id but has forfeited access to it.
    SubjectStatus.LOCKED: GateLevel.OPEN,
}


def combination_key(first: IdentifierType, second: IdentifierType) -> str:
    """Order-independent key for a pair of identifier types."""
    return "+".join(sorted((first.value, second.value)))


def slot_time_key(day: date, at: time) -> str:
    """Key for a date and time that a search actually offered."""
    return f"{day.isoformat()}T{at.isoformat(timespec='minutes')}"


class Session(BaseModel):
    """One conversation."""

    model_config = ConfigDict(validate_assignment=True)

    session_id: str = Field(default_factory=lambda: f"s_{uuid.uuid4().hex[:12]}")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    channel: Literal["text"] = "text"
    turn_index: int = 0

    salt: str = Field(default_factory=lambda: secrets.token_hex(16))
    """Per-session salt for attempt digests. Never leaves the process."""

    # --- subject identity ------------------------------------------------
    status: SubjectStatus = SubjectStatus.NONE
    patient_id: str | None = None
    failed_attempts: int = 0
    verified_at: datetime | None = None
    verify_methods: list[IdentifierType] = Field(default_factory=list)
    """Types only. Never values (spec §4.2)."""

    attempted_combinations: set[str] = Field(default_factory=set)
    """Pairs of identifier types already tried, as ``"address_zip+dob"``."""

    attempt_digests: set[str] = Field(default_factory=set)
    """Salted hashes of tried values, so a repeat is recognisable but not
    recoverable."""

    # --- provenance ledger (design §9) -----------------------------------
    seen_patient_ids: set[str] = Field(default_factory=set)
    seen_appointment_ids: set[str] = Field(default_factory=set)
    seen_slot_ids: set[str] = Field(default_factory=set)
    offered_times: set[str] = Field(default_factory=set)
    """Date-and-time keys a search actually returned. book_appointment takes a
    date and a time rather than a slot id, so the ledger has to remember both
    shapes or booking could not be provenance-checked at all."""

    confirmed_phone: str | None = None
    """The number on the patient's record, set once identity is verified."""

    patient_asserted_phones: set[str] = Field(default_factory=set)
    """Numbers the patient stated in their own turns.

    Specification §4.10 lets directions go to a number the patient confirms as
    their own, with no verification at all. Their saying it is the confirmation,
    so it has to be captured — otherwise the assistant asks, the patient
    answers, the gate refuses anyway, and the conversation loops with no exit."""
    booked_service_dates: set[date] = Field(default_factory=set)

    # --- workflow preconditions ------------------------------------------
    existence_checked: bool = False
    duplicate_suspected: bool = False
    last_lookup_ambiguous: bool = False

    # --- conversation -----------------------------------------------------
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    """Populated in Phase 4, redacted before persistence."""

    # ------------------------------------------------------------ derived ---

    @property
    def attained_level(self) -> GateLevel:
        return _STATUS_LEVEL[self.status]

    @property
    def is_locked(self) -> bool:
        return self.status is SubjectStatus.LOCKED

    def satisfies(self, required: GateLevel) -> bool:
        """Ladder comparison. NUMBER_CONFIRMED is not on the ladder and is
        answered by its own precondition, so it is never satisfied here."""
        if required is GateLevel.NUMBER_CONFIRMED:
            return False
        return LADDER[self.attained_level] >= LADDER[required]

    def has_tried(self, first: IdentifierType, second: IdentifierType) -> bool:
        return combination_key(first, second) in self.attempted_combinations

    def was_offered(self, day: date, at: time) -> bool:
        return slot_time_key(day, at) in self.offered_times

    # ---------------------------------------------------------- mutations ---

    def mark_identified(self, patient_id: str) -> None:
        self.patient_id = patient_id
        self.seen_patient_ids = self.seen_patient_ids | {patient_id}
        if self.status is SubjectStatus.NONE:
            self.status = SubjectStatus.IDENTIFIED

    def mark_verified(self, methods: list[IdentifierType], at: datetime | None = None) -> None:
        self.status = SubjectStatus.VERIFIED
        self.verified_at = at or datetime.now(UTC)
        self.verify_methods = list(methods)

    def mark_registered(self, patient_id: str) -> None:
        self.patient_id = patient_id
        self.seen_patient_ids = self.seen_patient_ids | {patient_id}
        self.status = SubjectStatus.REGISTERED

    def mark_locked(self) -> None:
        self.status = SubjectStatus.LOCKED

    def confirm_phone(self, phone_number: str) -> None:
        self.confirmed_phone = phone_number

    def note_asserted_phone(self, phone_number: str) -> None:
        self.patient_asserted_phones = self.patient_asserted_phones | {phone_number}

    def phone_is_confirmed(self, phone_number: str, *, by_patient_only: bool = False) -> bool:
        """Whether this number may be texted.

        ``by_patient_only`` is the directions case: the patient stating the
        number is enough. Everything else must match the number on the record,
        which is only known after verification.
        """
        if self.confirmed_phone == phone_number:
            return True
        return by_patient_only and phone_number in self.patient_asserted_phones

    def reset_subject(self) -> None:
        """Forget who we were talking to, keeping the ledger of what was seen.

        Used when a lookup turns out ambiguous: an ambiguous match must not
        leave a patient_id sitting in the session (spec §4.1).
        """
        self.status = SubjectStatus.NONE
        self.patient_id = None
        self.verified_at = None
        self.verify_methods = []
