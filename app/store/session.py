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

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from app.channel import is_patient_facing
from app.tools.schemas import ClinicalRole, IdentifierType


class Role(StrEnum):
    """The principal bound to a session (spec §1.1).

    Bound at session establishment and *"never inferred from, or changed by,
    anything said inside the conversation"*. That sentence is the reason this is
    a field with no setter rather than something a tool can raise.

    Distinct from ``SubjectStatus``, which is about *whose record* may be
    touched, and from ``ClinicalRole``, which is a licensed job title asserted by
    the identity provider. §3.2: the two questions are independent and *"neither
    substitutes for the other"*.
    """

    SYSTEM = "system"
    """The clinic's own automation. Not a conversational participant (§1.1), so
    a session whose effective role is SYSTEM can do nothing but authenticate."""

    PATIENT = "patient"
    CLINICAL_ASSISTANT = "clinical_assistant"


class RoleImmutable(RuntimeError):
    """An attempt to change what a session is after it was established.

    Not a denial the model can recover from — a denial is a verdict about a
    proposed call, and this is a programming error in the process. §3.2 makes
    role, staff identifier and expiry *"read-only for the session's lifetime"*;
    code that tries anyway has misunderstood something and should stop.
    """


BOUND_AT_ESTABLISHMENT = frozenset({"session_id", "created_at", "channel", "role", "salt"})
"""Fields fixed when the session is created. §1.1 for ``role``; the rest are
identity and would invalidate the audit chain or the attempt digests."""

WRITE_ONCE = frozenset({"staff_id", "asserted_role", "authenticated_at", "expires_at"})
"""Written once, at authentication, then read-only (spec §3.2 item 4).

Write-once rather than establishment-bound because they are unknown when the
session starts. Re-authentication after expiry needs a *new* session, which is
what §3.2's "require re-authentication" means given the role is fixed."""


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
    channel: Literal["text", "clinical"] = "text"
    turn_index: int = 0

    # --- principal (spec §1.1, §3.2) --------------------------------------
    role: Role = Role.PATIENT
    """What this session is. Immutable — see BOUND_AT_ESTABLISHMENT."""

    staff_id: str | None = None
    asserted_role: ClinicalRole | None = None
    """The role the identity provider returned. Never the one a caller claimed."""

    authenticated_at: datetime | None = None
    expires_at: datetime | None = None

    salt: str = Field(default_factory=lambda: secrets.token_hex(16))
    """Per-session salt for attempt digests. Never leaves the process."""

    _established: bool = PrivateAttr(default=False)
    """False only while pydantic is building the object. Guards __setattr__ so
    construction and rehydration from the store can set what nothing else may."""

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

    # ------------------------------------------------------- establishment ---

    def model_post_init(self, context: Any, /) -> None:
        """Close the session to structural change.

        Everything before this point is construction, including rehydration from
        the session store, which must be able to restore fields nothing else may
        write. Everything after it goes through __setattr__ below.
        """
        self._established = True

    @model_validator(mode="after")
    def _clinical_sessions_are_not_patient_facing(self) -> Session:
        """spec §3.2 — *"A Clinical Assistant session is never established on a
        patient-facing channel."*

        A structural invariant, so it holds for every construction path: an
        endpoint, a test, a rehydration from the store. Which channels are
        *eligible* is clinic configuration (``ClinicalConfig.channels``) and is
        checked when a session is established; that a patient-facing channel is
        never eligible is not configurable, and is checked here.
        """
        if self.role is Role.CLINICAL_ASSISTANT and is_patient_facing(self.channel):
            raise ValueError(
                f"a clinical_assistant session cannot be established on the "
                f"patient-facing channel {self.channel!r} (spec §3.2)"
            )
        return self

    def __setattr__(self, name: str, value: Any) -> None:
        """Refuse the writes §1.1 and §3.2 forbid.

        RoleImmutable rather than a gate denial: a denial is a verdict about
        something the *model* proposed and can recover from, and no model can
        reach this. Code that tries has misunderstood the design, and the loudest
        possible failure is the kindest one.
        """
        if getattr(self, "_established", False):
            if name in BOUND_AT_ESTABLISHMENT:
                raise RoleImmutable(
                    f"{name!r} is bound when the session is established and cannot "
                    f"be changed (spec §1.1)"
                )
            if name in WRITE_ONCE and getattr(self, name, None) is not None:
                raise RoleImmutable(
                    f"{name!r} is written once at authentication and is read-only for "
                    f"the session's lifetime (spec §3.2)"
                )
        super().__setattr__(name, value)

    # ---------------------------------------------------------- principal ---

    @property
    def clinical_authentication_valid(self) -> bool:
        """Is a clinical authentication in force *right now*?

        Derived rather than stored. A boolean field would be one clock tick away
        from disagreeing with ``expires_at``, and the disagreement would grant
        access rather than deny it.
        """
        if self.role is not Role.CLINICAL_ASSISTANT:
            return False
        if self.authenticated_at is None or self.expires_at is None:
            return False
        return datetime.now(UTC) < self.expires_at

    @property
    def effective_role(self) -> Role:
        """The role whose capabilities are live now.

        This reconciles two sentences that look contradictory. §3.2: *"The role
        is fixed for the lifetime of the session."* §4.13: *"On expiry or
        failure, drop to the system role. Do not fall back to the patient
        role."*

        Both hold if the *established* principal is immutable while the
        *effective* capability lapses. An unauthenticated or expired clinical
        session reads as SYSTEM — which §1.1 defines as not a conversational
        participant, so nothing is callable but authentication. It never reads
        as PATIENT, so expiry cannot hand a clinician a patient's workflows.
        """
        if self.role is Role.CLINICAL_ASSISTANT and not self.clinical_authentication_valid:
            return Role.SYSTEM
        return self.role

    @property
    def is_patient_facing(self) -> bool:
        """Whether output from this session may reach a member of the public.

        Read by the audit verifier: a dose in a patient-facing session's log is
        a §7.3 leak, and the same dose in a clinical session's log is the
        feature working.
        """
        return is_patient_facing(self.channel)

    def bind_clinical_authentication(
        self,
        staff_id: str,
        asserted_role: ClinicalRole,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> None:
        """Record a successful §3.2 authentication. Callable once.

        Takes the role the *identity provider* asserted. Nothing here checks a
        credential — that is the provider's job behind the port (C1) — and
        nothing here sets ``role``, which was bound when the session was
        established. This only records the outcome §3.2 item 4 requires.

        A second call raises rather than refreshing: §3.2 makes these read-only
        for the session's lifetime, so extending a session by re-authenticating
        into it is exactly what must not be possible.
        """
        if self.role is not Role.CLINICAL_ASSISTANT:
            raise RoleImmutable(
                "authentication cannot be bound to a session that was not "
                "established as clinical_assistant (spec §3.2)"
            )
        self.staff_id = staff_id
        self.asserted_role = asserted_role
        self.authenticated_at = now or datetime.now(UTC)
        self.expires_at = expires_at

    # ---------------------------------------------------------- mutations ---

    def mark_identified(self, patient_id: str) -> None:
        """Point the session at a patient record.

        If this is a *different* patient from the one already established, any
        verification is dropped. Identity is verified per person, not per
        conversation: a session verified as one patient that then looks up
        another must not carry that verification across, or the second person's
        record becomes readable to the first.
        """
        if self.patient_id is not None and patient_id != self.patient_id:
            self.reset_subject()

        self.patient_id = patient_id
        # The ledger keeps every id the system handed out — re-verifying as an
        # earlier patient later must still be possible. Authorization is what
        # narrows to one subject, not this.
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
