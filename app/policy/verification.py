"""Identity verification state machine.

Specification §3 sets four conditions: a patient_id from an existence check,
two identifiers, each of a permitted type, and no type used twice. The first is
a gate level, the middle two are argument invariants enforced in the schema, and
the last one lives here — along with attempt counting and lockout.

**One refinement from design §8.** The design says a failed attempt leaves "the
tried identifier type still consumed, so a caller cannot burn three attempts on
the same field". Taken literally that is unworkable: only three identifier types
exist, each attempt consumes two, so a single failure would leave one type and
no possible second attempt — the configured limit of three could never be
reached.

What makes the rule work is consuming the *combination*. Three types yield
exactly three distinct pairs — dob+phone, dob+zip, phone+zip — so a caller gets
three genuinely different attempts and then the session locks, which is
precisely the configured limit. The intent of the design is preserved (no
re-guessing the same field pairing); the arithmetic now works.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import ClinicConfig
from app.policy.redaction import digest
from app.ports import PatientLookupResult, VerificationResult
from app.store.session import Session, SubjectStatus, combination_key
from app.tools.schemas import IdentifierType


@dataclass(frozen=True)
class AttemptOutcome:
    """What one verification attempt did to the session."""

    verified: bool
    locked: bool
    attempts_used: int
    attempts_remaining: int
    repeat_of_earlier_attempt: bool


def record_lookup(session: Session, result: PatientLookupResult) -> None:
    """Apply an existence-check result to the session.

    A single match identifies. Anything else does not — and an ambiguous match
    must not leave a patient_id behind, because the assistant may not choose
    between records (spec §4.1).
    """
    session.existence_checked = True
    session.last_lookup_ambiguous = result.ambiguous

    if result.found and result.patient_id:
        session.mark_identified(result.patient_id)
        return

    if result.ambiguous:
        session.duplicate_suspected = True
        session.reset_subject()
        return

    # No match. Registration is now the path (spec §4.1), and any earlier
    # identification is stale.
    session.duplicate_suspected = False
    session.reset_subject()


def register_attempt(
    session: Session,
    first_type: IdentifierType,
    first_value: str,
    second_type: IdentifierType,
    second_value: str,
    result: VerificationResult,
    clinic: ClinicConfig,
) -> AttemptOutcome:
    """Apply the outcome of one verify_patient_identity call.

    The values are used to compute a salted digest and are then dropped. They
    are never stored, logged or returned.
    """
    limit = clinic.policy.verification_attempt_limit

    fingerprint = digest(
        f"{first_type.value}:{first_value}|{second_type.value}:{second_value}",
        session.salt,
    )
    repeat = fingerprint in session.attempt_digests

    session.attempted_combinations = session.attempted_combinations | {
        combination_key(first_type, second_type)
    }
    session.attempt_digests = session.attempt_digests | {fingerprint}

    if result.verified:
        session.mark_verified([first_type, second_type], at=datetime.now(UTC))
        return AttemptOutcome(
            verified=True,
            locked=False,
            attempts_used=session.failed_attempts,
            attempts_remaining=limit - session.failed_attempts,
            repeat_of_earlier_attempt=repeat,
        )

    session.failed_attempts += 1
    locked = session.failed_attempts >= limit
    if locked:
        # spec §4.2 — after the limit, escalate. Do not keep asking.
        session.mark_locked()
    elif session.status is SubjectStatus.NONE and session.patient_id:
        session.status = SubjectStatus.IDENTIFIED

    return AttemptOutcome(
        verified=False,
        locked=locked,
        attempts_used=session.failed_attempts,
        attempts_remaining=max(0, limit - session.failed_attempts),
        repeat_of_earlier_attempt=repeat,
    )


def available_combinations(session: Session) -> list[str]:
    """Identifier pairings not yet tried, for the assistant to offer next."""
    every = [
        combination_key(first, second)
        for index, first in enumerate(IdentifierType)
        for second in list(IdentifierType)[index + 1 :]
    ]
    return [pair for pair in every if pair not in session.attempted_combinations]


def attempts_remaining(session: Session, clinic: ClinicConfig) -> int:
    return max(0, clinic.policy.verification_attempt_limit - session.failed_attempts)
