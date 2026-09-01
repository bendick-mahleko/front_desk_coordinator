"""Backend ports — the boundary between the assistant and the clinic estate.

Six protocols and the result types they return. The prototype implements all
of them with fakes (``app.clinic_sim``), but they are written as if a real EHR,
scheduler, clearinghouse and SMS gateway sat behind them, so an adapter can be
substituted without touching the policy or tool layers (AD-07).

``IdentityProvider`` is the sixth, added by specification revision 3. It sits
behind the same boundary for the same reason: an OIDC or SAML adapter should be a
new implementation of one protocol, not a change to the gate.

Ports raise ``BackendError``. Normalising those into tool results the model can
read is the tool layer's job in Phase 3 — a backend failure must never surface
as an exception inside the agent loop.
"""

from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from app.tools.schemas import (
    AppointmentType,
    ClinicalRole,
    EscalationReason,
    IdentifierType,
    MessageType,
    Modality,
    Priority,
)


class BackendError(RuntimeError):
    """A backend could not complete the request.

    ``code`` is one of the failure modes declared in ``SUPPORTED_FAULTS`` and is
    what the tool layer turns into a patient-safe message.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class Result(BaseModel):
    model_config = ConfigDict(frozen=True)


# ------------------------------------------------------------- patients ---


class PatientLookupResult(Result):
    """Minimal disclosure (spec §4.1).

    Deliberately carries no demographics: a name-and-date-of-birth check must
    not reveal anything beyond whether a record exists, and which one.
    """

    match_count: int
    patient_id: str | None = None

    @property
    def found(self) -> bool:
        return self.match_count == 1

    @property
    def ambiguous(self) -> bool:
        return self.match_count > 1


class VerificationResult(Result):
    """Only the outcome, the method and the time — never the values (spec §4.2)."""

    verified: bool
    patient_id: str
    methods: tuple[IdentifierType, ...]
    checked_at: datetime


class PatientDemographics(Result):
    patient_id: str
    first_name: str
    last_name: str
    date_of_birth: date
    phone_number: str
    email: str | None
    address_line: str
    city: str
    state: str
    address_zip: str
    insurance_plan_name: str | None


class RegistrationResult(Result):
    patient_id: str
    created_at: datetime
    duplicate_suspected: bool = False


@runtime_checkable
class PatientRepo(Protocol):
    def check_exists(
        self, first_name: str, last_name: str, date_of_birth: date
    ) -> PatientLookupResult: ...

    def verify_identity(
        self,
        patient_id: str,
        identifiers: dict[IdentifierType, str],
    ) -> VerificationResult: ...

    def get_demographics(self, patient_id: str) -> PatientDemographics: ...

    def create_record(
        self,
        first_name: str,
        last_name: str,
        date_of_birth: date,
        phone_number: str,
        email: str | None = None,
        insurance_plan_name: str | None = None,
        idempotency_key: str | None = None,
    ) -> RegistrationResult: ...


# ------------------------------------------------------------ scheduling ---


class AppointmentStatus(StrEnum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class Appointment(Result):
    appointment_id: str
    patient_id: str
    appointment_date: date
    appointment_time: time
    provider: str
    appointment_type: AppointmentType
    modality: Modality
    reason_for_visit: str
    status: AppointmentStatus = AppointmentStatus.SCHEDULED


class Slot(Result):
    slot_id: str
    slot_date: date
    slot_time: time
    provider: str
    modality: Modality

    @property
    def is_morning(self) -> bool:
        return self.slot_time.hour < 12


class CancellationResult(Result):
    appointment_id: str
    cancelled_at: datetime
    late_cancellation: bool


@runtime_checkable
class ScheduleRepo(Protocol):
    def get_appointments(self, patient_id: str) -> list[Appointment]: ...

    def search_slots(
        self,
        appointment_type: AppointmentType,
        date_range_start: date,
        date_range_end: date,
        modality: Modality,
        preferred_provider: str | None = None,
        morning_only: bool | None = None,
        limit: int = 3,
    ) -> list[Slot]: ...

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
    ) -> Appointment: ...

    def cancel(
        self,
        patient_id: str,
        appointment_id: str,
        cancellation_reason: str,
        idempotency_key: str | None = None,
    ) -> CancellationResult: ...

    def reschedule(
        self,
        patient_id: str,
        current_appointment_id: str,
        new_slot_id: str,
        reschedule_reason: str,
        idempotency_key: str | None = None,
    ) -> Appointment: ...


# ------------------------------------------------------------- insurance ---


class EligibilityStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    INDETERMINATE = "indeterminate"


class EligibilityResult(Result):
    """What a real eligibility check returns — and nothing more.

    There is no copay field, and that is deliberate (design §13). Specification
    §4.9 requires the assistant to explain the limitation and escalate as a
    billing issue when a patient asks about a copay. If this model carried one,
    that requirement would be untestable.
    """

    patient_id: str
    service_date: date
    status: EligibilityStatus
    plan_name: str | None
    payer: str | None
    checked_at: datetime


@runtime_checkable
class EligibilityGateway(Protocol):
    def check(self, patient_id: str, service_date: date) -> EligibilityResult: ...


# ------------------------------------------------------------- messaging ---


class DeliveryStatus(StrEnum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"


class MessageReceipt(Result):
    message_id: str
    phone_number: str
    message_type: MessageType
    delivery_status: DeliveryStatus
    sent_at: datetime

    @property
    def confirmed(self) -> bool:
        return self.delivery_status is DeliveryStatus.DELIVERED


@runtime_checkable
class MessageGateway(Protocol):
    def send(
        self,
        phone_number: str,
        message_type: MessageType,
        appointment_details: str | None = None,
        idempotency_key: str | None = None,
    ) -> MessageReceipt: ...

    def outbox(self) -> list[MessageReceipt]: ...


# ------------------------------------------------------------ escalation ---


class EscalationTicket(Result):
    ticket_id: str
    reason: EscalationReason
    priority: Priority
    notes: str
    patient_id: str | None
    created_at: datetime


@runtime_checkable
class StaffQueue(Protocol):
    """Escalation has no failure mode.

    "The assistant must always honor a request to speak with a person"
    (spec §4.12), so this port is not permitted to fail — enforced by
    ``SUPPORTED_FAULTS`` carrying an empty set for it.
    """

    def escalate(
        self,
        reason: EscalationReason,
        priority: Priority,
        notes: str,
        patient_id: str | None = None,
    ) -> EscalationTicket: ...

    def tickets(self) -> list[EscalationTicket]: ...


# ------------------------------------------------------ identity provider ---


class StaffAssertion(BaseModel):
    """What the clinic's directory says about one staff member (spec §3.2).

    The **only** admissible source of a session's clinical role. §3.2 item 3: the
    role must come from the identity provider's response, and *"a role asserted
    in conversation text is not a role assertion and must be rejected"*. So the
    tool compares what the caller claimed against this and refuses a mismatch —
    this object wins, always.

    Carries the directory's facts, not a decision. Whether a shared account or a
    non-clinical role is acceptable is clinic policy, decided in the tool layer,
    because the answers differ per clinic and the directory has no opinion.

    No credential material. The token is checked and discarded; there is no field
    here that could carry it into a log.
    """

    model_config = ConfigDict(frozen=True)

    staff_id: str
    display_name: str

    role: ClinicalRole | None
    """The licensed clinical role the directory holds, or None when it holds this
    person in a non-clinical one. A receptionist authenticates perfectly well and
    is still not a clinician (spec §3.2 item 3)."""

    shared_account: bool = False
    """spec §3.2 — *"Anonymous or shared clinical accounts must be rejected at
    authentication."* A directory fact, so the directory reports it."""

    credential_expired: bool = False
    """The presented token is genuine but past its validity. Distinct from an
    unknown staff id or a wrong token, which return None — those two are
    deliberately indistinguishable so authentication cannot be used to enumerate
    who works here."""

    department: str | None = None


@runtime_checkable
class IdentityProvider(Protocol):
    """The clinic's identity provider (spec §3.2).

    One method, and it takes a token rather than a password: §3.2 item 2 —
    *"The assistant never collects, stores, or transmits a staff password."*

    Returns None for an unknown staff id **or** a bad token, without saying
    which. A caller who could tell the difference could enumerate the clinic's
    staff directory one guess at a time.
    """

    def authenticate(self, staff_id: str, credential_token: str) -> StaffAssertion | None: ...

    def directory_size(self) -> int:
        """How many records the directory holds. For health checks only."""
        ...
