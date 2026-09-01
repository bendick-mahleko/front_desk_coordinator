"""The policy gate — specification §3, expressed once.

This is the component the whole safety argument rests on. The model proposes;
this decides. It is deliberately boring: a lookup, four checks and a verdict.

Four checks, in order. Cheap and non-disclosing first, so a denial reveals as
little as possible about why:

1. **Schema**        — required parameters, enums, dates, argument invariants
2. **Authorization** — is the session at the level this function needs
3. **Provenance**    — did this identifier come from a real result
4. **Preconditions** — has the required earlier step happened

A denial is not an exception. It is a verdict the tool layer turns into a tool
result the model reads and recovers from.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.config import ClinicConfig, get_clinic_config
from app.policy import provenance
from app.policy.messages import DenialCode, Remedy, denial_message, remedy_text
from app.policy.redaction import redact_args
from app.store.session import GateLevel, Session, SubjectStatus
from app.tools.schemas import ARGUMENT_MODELS, AppointmentType, MessageType, StrictArgs

# ---------------------------------------------------------------- verdict ---


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    rule: str
    required: GateLevel | None = None
    actual: GateLevel | None = None
    code: DenialCode | None = None
    message: str = ""
    remedy: str = ""
    remedy_key: Remedy | None = None
    detail: str = ""
    """Non-disclosing technical detail for the audit log — never for the patient."""
    args: StrictArgs | None = None
    """The validated, coerced arguments. Present only when allowed."""

    @classmethod
    def allow(cls, rule: str, args: StrictArgs, required: GateLevel, actual: GateLevel) -> Verdict:
        return cls(allowed=True, rule=rule, required=required, actual=actual, args=args)

    @classmethod
    def deny(
        cls,
        code: DenialCode,
        rule: str,
        remedy: Remedy,
        required: GateLevel | None = None,
        actual: GateLevel | None = None,
        detail: str = "",
    ) -> Verdict:
        return cls(
            allowed=False,
            rule=rule,
            required=required,
            actual=actual,
            code=code,
            message=denial_message(code),
            remedy=remedy_text(remedy),
            remedy_key=remedy,
            detail=detail,
        )


# ----------------------------------------------------------- preconditions ---

PreconditionCheck = Callable[[Any, Session, ClinicConfig], Remedy | None]
"""Returns a remedy when the precondition fails, None when it holds.

Arguments are typed ``Any`` because a precondition only ever runs for the one
function that names it, and reads fields that exist on that function's model
alone. The gate has already validated them by this point."""


def _existence_checked(args: Any, session: Session, clinic: ClinicConfig) -> Remedy | None:
    # spec §4.4 — always check before creating, to reduce duplicates.
    return None if session.existence_checked else Remedy.CHECK_EXISTENCE_FIRST


def _no_duplicate(args: Any, session: Session, clinic: ClinicConfig) -> Remedy | None:
    # spec §4.4 — on a possible duplicate, escalate instead of creating.
    return Remedy.ESCALATE_DUPLICATE if session.duplicate_suspected else None


def _not_locked(args: Any, session: Session, clinic: ClinicConfig) -> Remedy | None:
    # spec §4.2 — after the limit, escalate rather than asking again.
    return Remedy.ESCALATE_LOCKED if session.is_locked else None


def _combination_unused(args: Any, session: Session, clinic: ClinicConfig) -> Remedy | None:
    first = args.identifier_1_type
    second = args.identifier_2_type
    if session.has_tried(first, second):
        return Remedy.TRY_DIFFERENT_IDENTIFIERS
    return None


def _time_from_search(args: Any, session: Session, clinic: ClinicConfig) -> Remedy | None:
    # spec §4.6 — "use the precise date and time selected from appointment
    # search results". A time nobody offered cannot be booked.
    day = args.appointment_date
    at = args.appointment_time
    return None if session.was_offered(day, at) else Remedy.SEARCH_SLOTS_FIRST


def _service_date_confirmed(args: Any, session: Session, clinic: ClinicConfig) -> Remedy | None:
    # spec §4.9 — the date of service comes from a booked appointment or an
    # explicit patient confirmation, never from the model's imagination.
    service_date = args.service_date
    return None if service_date in session.booked_service_dates else Remedy.CONFIRM_SERVICE_DATE


def _visit_type_possible(args: Any, session: Session, clinic: ClinicConfig) -> Remedy | None:
    """A follow-up needs an earlier visit to follow up on.

    A patient registered during this conversation has no history with the
    clinic, so ``follow_up`` names a visit that cannot exist. The model has no
    particular reason to notice — it picks a visit type from the conversation,
    and "I'd like an appointment about my blood pressure" reads as a review.

    This is not a clinic policy question and it is not a judgement call, so it
    is settled here rather than asked of the model. It fires only on REGISTERED,
    the one status that *proves* there is no history: an unidentified caller may
    well be an existing patient who has not said so yet, and blocking their
    search would be worse than the wrong label.
    """
    if session.status is not SubjectStatus.REGISTERED:
        return None
    if args.appointment_type is not AppointmentType.FOLLOW_UP:
        return None
    return Remedy.NEW_PATIENT_FIRST_VISIT


def _phone_confirmed(args: Any, session: Session, clinic: ClinicConfig) -> Remedy | None:
    # spec §4.10 — confirm the destination number before sending.
    #
    # Directions may go to a number the patient states as their own. Everything
    # else carries or implies health information and must go to the number on
    # the record, which is only known once identity is verified.
    directions_only = SEND_TEXT_RULE(args) is GateLevel.NUMBER_CONFIRMED
    if session.phone_is_confirmed(args.phone_number, by_patient_only=directions_only):
        return None
    return Remedy.CONFIRM_PHONE_NUMBER


def _known_location(args: Any, session: Session, clinic: ClinicConfig) -> Remedy | None:
    # The enum guarantees membership; this checks the clinic actually has it
    # configured, so a location with no address cannot be read out.
    location = args.location
    return None if location.value in clinic.locations else Remedy.UNKNOWN_LOCATION


PRECONDITIONS: dict[str, PreconditionCheck] = {
    "EXISTENCE_CHECKED": _existence_checked,
    "NO_DUPLICATE": _no_duplicate,
    "NOT_LOCKED": _not_locked,
    "COMBINATION_UNUSED": _combination_unused,
    "TIME_FROM_SEARCH": _time_from_search,
    "SERVICE_DATE_CONFIRMED": _service_date_confirmed,
    "VISIT_TYPE_POSSIBLE": _visit_type_possible,
    "PHONE_CONFIRMED": _phone_confirmed,
    "KNOWN_LOCATION": _known_location,
}


# ---------------------------------------------------------------- policies ---


@dataclass(frozen=True)
class Policy:
    level: GateLevel
    rule: str
    """Where in the specification this comes from. Written into every audit
    record, so a reviewer can trace a decision back to the sentence."""

    accepts: tuple[SubjectStatus, ...] = ()
    """Statuses that satisfy this function despite not reaching ``level``."""

    requires: tuple[str, ...] = ()
    conditional: Callable[[Any], GateLevel] | None = None
    redact: tuple[str, ...] = ()
    minimal_disclosure: bool = False

    enforced_by_schema: tuple[str, ...] = field(default_factory=tuple)
    """Specification rules for this function that the argument model already
    enforces. Listed rather than re-implemented: duplicating them would create a
    second source of truth, but omitting them entirely would make this table an
    incomplete picture of §3 for anyone reviewing it here."""

    def required_level(self, args: Any) -> GateLevel:
        return self.conditional(args) if self.conditional else self.level


def SEND_TEXT_RULE(args: Any) -> GateLevel:
    """spec §4.10.

    Directions carry no health information, so they may go to a number the
    patient confirms as their own without verification. Everything else — intake
    forms, telehealth links, appointment confirmations, portal access — either
    carries PHI or proves a relationship with the clinic, and requires a
    verified subject.
    """
    message_type = args.message_type
    if message_type is MessageType.DIRECTIONS:
        return GateLevel.NUMBER_CONFIRMED
    return GateLevel.VERIFIED


TOOL_POLICY: dict[str, Policy] = {
    "check_patient_exists": Policy(
        GateLevel.OPEN,
        rule="spec§3/check_whether_a_patient_exists",
        minimal_disclosure=True,
    ),
    "create_new_patient_record": Policy(
        GateLevel.OPEN,
        rule="spec§3/create_a_new_patient_record",
        requires=("EXISTENCE_CHECKED", "NO_DUPLICATE"),
        redact=("date_of_birth", "phone_number", "email"),
    ),
    "verify_patient_identity": Policy(
        GateLevel.IDENTIFIED,
        rule="spec§3/verification_requirements",
        requires=("NOT_LOCKED", "COMBINATION_UNUSED"),
        redact=("identifier_1_value", "identifier_2_value"),
        enforced_by_schema=("two identifier types must differ (spec §3 rule 4)",),
    ),
    "get_patient_demographics": Policy(
        GateLevel.VERIFIED,
        rule="spec§3/get_demographics",
    ),
    "get_patient_appointments": Policy(
        GateLevel.VERIFIED,
        rule="spec§3/get_scheduled_appointments",
    ),
    "check_insurance_eligibility": Policy(
        GateLevel.VERIFIED,
        rule="spec§3/check_insurance_eligibility",
        requires=("SERVICE_DATE_CONFIRMED",),
    ),
    "search_available_appointments": Policy(
        GateLevel.OPEN,
        rule="spec§4.5/appointment_search",
        requires=("VISIT_TYPE_POSSIBLE",),
        enforced_by_schema=("date_range_end must not precede date_range_start (spec §4.5)",),
    ),
    "book_appointment": Policy(
        GateLevel.VERIFIED,
        rule="spec§3/book_an_appointment",
        accepts=(SubjectStatus.REGISTERED,),
        requires=("TIME_FROM_SEARCH", "VISIT_TYPE_POSSIBLE"),
        enforced_by_schema=("patient_id, or first and last name (spec §4.6)",),
    ),
    "cancel_appointment": Policy(
        GateLevel.VERIFIED,
        rule="spec§3/cancel_or_reschedule_an_appointment",
    ),
    "reschedule_appointment": Policy(
        GateLevel.VERIFIED,
        rule="spec§3/cancel_or_reschedule_an_appointment",
    ),
    "send_secure_text": Policy(
        GateLevel.VERIFIED,
        rule="spec§3/send_texts",
        conditional=SEND_TEXT_RULE,
        requires=("PHONE_CONFIRMED",),
        redact=("phone_number", "appointment_details"),
        enforced_by_schema=("appointment_details only with a confirmation (spec §4.10)",),
    ),
    "get_clinic_hours": Policy(GateLevel.OPEN, rule="spec§3/clinic_hours_and_directions"),
    "check_business_hours": Policy(GateLevel.OPEN, rule="spec§3/clinic_hours_and_directions"),
    "get_clinic_directions": Policy(
        GateLevel.OPEN,
        rule="spec§3/clinic_hours_and_directions",
        requires=("KNOWN_LOCATION",),
    ),
    "escalate_to_staff": Policy(
        GateLevel.OPEN,
        rule="spec§3/escalate_to_staff",
        redact=("notes",),
    ),
    # Knowledge extension. Verified because a complaint is health information
    # about the person describing it, and because the routing it produces leads
    # straight into booking, which is itself verified.
    # Clinical review (spec r3 §4.13). OPEN on the *patient* ladder because it
    # touches no patient record — a clinician authenticating has established
    # nobody, and §3.2 is explicit that clinical authentication "does not confer
    # access to an arbitrary patient's record". What keeps a patient session out
    # of it is that the function is not in a patient session's tool schema at
    # all (§2), which is the registry's job; C2 adds the gate's own role check as
    # the second layer.
    "authenticate_clinical_user": Policy(
        GateLevel.OPEN,
        rule="spec§3.2/clinical_authentication",
        redact=("credential_token",),
    ),
    "suggest_appointment_type": Policy(
        GateLevel.VERIFIED,
        rule="spec§4.5/appointment_search",
        # A registered patient reaches this for the same reason they reach
        # booking: they supplied the complaint themselves, so there is nothing
        # to disclose back to them. Denying it sent the model to ask a
        # just-registered patient to verify — against a record created from
        # their own answers, so no combination of identifiers could ever
        # succeed. That is a dead end, and it cost the safety layer too: the
        # red-flag screen lives behind this call, and new patients are exactly
        # the population most likely to describe a raw symptom.
        accepts=(SubjectStatus.REGISTERED,),
        redact=("complaint",),
    ),
}


# -------------------------------------------------------------------- gate ---


class PolicyGate:
    """Evaluates one proposed call against one session."""

    def __init__(self, clinic: ClinicConfig | None = None) -> None:
        self._clinic = clinic or get_clinic_config()

    def evaluate(self, fn_name: str, raw_args: dict[str, Any], session: Session) -> Verdict:
        policy = TOOL_POLICY.get(fn_name)
        model = ARGUMENT_MODELS.get(fn_name)
        if policy is None or model is None:
            return Verdict.deny(
                DenialCode.UNKNOWN_FUNCTION,
                rule="registry",
                remedy=Remedy.FIX_ARGUMENTS,
                detail=f"no policy or argument model for {fn_name!r}",
            )

        # 1 — schema
        try:
            args = model.model_validate(raw_args)
        except ValidationError as exc:
            return Verdict.deny(
                DenialCode.INVALID_ARGUMENTS,
                rule=policy.rule,
                remedy=Remedy.FIX_ARGUMENTS,
                detail=_validation_detail(exc),
            )

        required = policy.required_level(args)
        actual = session.attained_level

        # 2 — authorization
        if not self._authorized(policy, required, session, args):
            return Verdict.deny(
                DenialCode.VERIFICATION_REQUIRED,
                rule=policy.rule,
                remedy=self._authorization_remedy(required, session, args),
                required=required,
                actual=actual,
            )

        # 3 — provenance
        fabricated = provenance.check(args.model_dump(), session)
        if fabricated is not None:
            argument, remedy = fabricated
            return Verdict.deny(
                DenialCode.UNKNOWN_REFERENCE,
                rule="spec§6/do_not_invent_identifiers",
                remedy=remedy,
                required=required,
                actual=actual,
                detail=f"{argument} was not produced by any result in this session",
            )

        # 4 — preconditions
        for name in policy.requires:
            failed = PRECONDITIONS[name](args, session, self._clinic)
            if failed is not None:
                return Verdict.deny(
                    DenialCode.PRECONDITION_FAILED,
                    rule=policy.rule,
                    remedy=failed,
                    required=required,
                    actual=actual,
                    detail=f"precondition {name} not satisfied",
                )

        return Verdict.allow(policy.rule, args, required, actual)

    # ------------------------------------------------------------ helpers ---

    def _authorized(self, policy: Policy, required: GateLevel, session: Session, args: Any) -> bool:
        if required is GateLevel.NUMBER_CONFIRMED:
            # Not on the ladder: the PHONE_CONFIRMED precondition answers it,
            # and a verified session satisfies it too.
            return True

        if session.satisfies(required):
            # Verified for one person, not for the conversation. The provenance
            # ledger says an id is real; it does not say it is *this* patient's,
            # and a verified session must not be able to read a second record
            # just because an earlier lookup put it in the ledger.
            target = getattr(args, "patient_id", None)
            wrong_subject = (
                required in (GateLevel.IDENTIFIED, GateLevel.VERIFIED)
                and target is not None
                and session.patient_id is not None
                and target != session.patient_id
            )
            return not wrong_subject

        # A registered patient may act on the record they just created, and
        # only that one (design §6).
        if session.status in policy.accepts and session.status is SubjectStatus.REGISTERED:
            target = getattr(args, "patient_id", None)
            return target is None or target == session.patient_id

        return False

    @staticmethod
    def _authorization_remedy(required: GateLevel, session: Session, args: Any = None) -> Remedy:
        if session.is_locked:
            return Remedy.ESCALATE_LOCKED
        target = getattr(args, "patient_id", None) if args is not None else None
        if target is not None and session.patient_id is not None and target != session.patient_id:
            return Remedy.WRONG_SUBJECT
        if session.last_lookup_ambiguous:
            return Remedy.DISAMBIGUATE
        if required is GateLevel.IDENTIFIED or session.patient_id is None:
            return Remedy.IDENTIFY_FIRST
        return Remedy.VERIFY_FIRST

    def audit_view(self, fn_name: str, raw_args: dict[str, Any]) -> dict[str, Any]:
        """The log-safe rendering of a call's arguments."""
        return redact_args(raw_args)


def _validation_detail(exc: ValidationError) -> str:
    """Field names and error types only — never the rejected values.

    A validation error on ``identifier_2_value`` must not put the value it
    rejected into the audit log.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['type']}"
        for error in exc.errors()
    )
