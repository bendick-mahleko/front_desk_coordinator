"""P2-T12 — the verification state machine."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.policy.verification import (
    AttemptOutcome,
    available_combinations,
    record_lookup,
    register_attempt,
)
from app.ports import PatientLookupResult, VerificationResult
from app.store.session import GateLevel, Session, SubjectStatus
from app.tools.schemas import IdentifierType

DOB = IdentifierType.DOB
PHONE = IdentifierType.PHONE
ZIP = IdentifierType.ADDRESS_ZIP


def outcome(session: Session, clinic, first, second, verified: bool) -> AttemptOutcome:
    result = VerificationResult(
        verified=verified,
        patient_id=session.patient_id or "PT-4101",
        methods=(first, second),
        checked_at=datetime.now(UTC),
    )
    return register_attempt(session, first, "value-1", second, "value-2", result, clinic)


# ---------------------------------------------------------------- lookup ---


def test_a_single_match_identifies():
    session = Session()
    record_lookup(session, PatientLookupResult(match_count=1, patient_id="PT-4101"))

    assert session.status is SubjectStatus.IDENTIFIED
    assert session.patient_id == "PT-4101"
    assert session.existence_checked
    assert "PT-4101" in session.seen_patient_ids


def test_an_ambiguous_match_identifies_nobody():
    """spec §4.1 — the assistant may not choose between records."""
    session = Session()
    record_lookup(session, PatientLookupResult(match_count=2))

    assert session.status is SubjectStatus.NONE
    assert session.patient_id is None
    assert session.duplicate_suspected
    assert session.last_lookup_ambiguous


def test_no_match_leaves_the_session_anonymous_and_registerable():
    session = Session()
    record_lookup(session, PatientLookupResult(match_count=0))

    assert session.status is SubjectStatus.NONE
    assert session.existence_checked
    assert not session.duplicate_suspected


def test_a_later_ambiguous_lookup_clears_an_earlier_identification():
    session = Session()
    record_lookup(session, PatientLookupResult(match_count=1, patient_id="PT-4101"))
    record_lookup(session, PatientLookupResult(match_count=2))

    assert session.patient_id is None
    assert session.status is SubjectStatus.NONE


# ----------------------------------------------------------- verification ---


def test_a_correct_pair_verifies(clinic):
    session = Session()
    record_lookup(session, PatientLookupResult(match_count=1, patient_id="PT-4101"))

    result = outcome(session, clinic, DOB, ZIP, verified=True)

    assert result.verified
    assert session.status is SubjectStatus.VERIFIED
    assert session.attained_level is GateLevel.VERIFIED
    assert session.verified_at is not None
    assert session.verify_methods == [DOB, ZIP]


def test_a_failure_returns_to_identified_and_counts(clinic):
    session = Session()
    record_lookup(session, PatientLookupResult(match_count=1, patient_id="PT-4101"))

    result = outcome(session, clinic, DOB, ZIP, verified=False)

    assert not result.verified
    assert not result.locked
    assert session.status is SubjectStatus.IDENTIFIED
    assert session.failed_attempts == 1
    assert result.attempts_remaining == 2


def test_three_failures_lock_the_session(clinic):
    """The configured limit. After it, escalation is the only exit (spec §4.2)."""
    session = Session()
    record_lookup(session, PatientLookupResult(match_count=1, patient_id="PT-4101"))

    outcome(session, clinic, DOB, ZIP, verified=False)
    outcome(session, clinic, DOB, PHONE, verified=False)
    final = outcome(session, clinic, PHONE, ZIP, verified=False)

    assert final.locked
    assert session.status is SubjectStatus.LOCKED
    assert session.attained_level is GateLevel.OPEN
    assert final.attempts_remaining == 0


def test_three_identifier_types_yield_exactly_three_attempts(clinic):
    """The refinement of design §8 that makes the limit reachable.

    Consuming individual types would leave one type after the first failure and
    no possible second pair. Consuming the *combination* gives three genuinely
    different attempts, which is exactly the configured limit.
    """
    session = Session()
    record_lookup(session, PatientLookupResult(match_count=1, patient_id="PT-4101"))

    assert len(available_combinations(session)) == 3

    outcome(session, clinic, DOB, ZIP, verified=False)
    assert available_combinations(session) == ["dob+phone", "address_zip+phone"]

    outcome(session, clinic, DOB, PHONE, verified=False)
    assert available_combinations(session) == ["address_zip+phone"]

    outcome(session, clinic, PHONE, ZIP, verified=False)
    assert available_combinations(session) == []
    assert session.status is SubjectStatus.LOCKED
    assert session.failed_attempts == clinic.policy.verification_attempt_limit


def test_combination_order_does_not_matter(clinic):
    session = Session()
    record_lookup(session, PatientLookupResult(match_count=1, patient_id="PT-4101"))

    outcome(session, clinic, DOB, ZIP, verified=False)

    assert session.has_tried(ZIP, DOB)
    assert session.has_tried(DOB, ZIP)


def test_a_success_after_a_failure_still_verifies(clinic):
    session = Session()
    record_lookup(session, PatientLookupResult(match_count=1, patient_id="PT-4101"))

    outcome(session, clinic, DOB, ZIP, verified=False)
    result = outcome(session, clinic, DOB, PHONE, verified=True)

    assert result.verified
    assert session.status is SubjectStatus.VERIFIED
    assert session.failed_attempts == 1, "a success must not erase the audit trail"


def test_the_attempt_limit_comes_from_configuration(clinic):
    """Not a constant in the source — the privacy officer's decision (design §20)."""
    relaxed = clinic.model_copy(
        update={"policy": clinic.policy.model_copy(update={"verification_attempt_limit": 2})}
    )
    session = Session()
    record_lookup(session, PatientLookupResult(match_count=1, patient_id="PT-4101"))

    outcome(session, relaxed, DOB, ZIP, verified=False)
    second = outcome(session, relaxed, DOB, PHONE, verified=False)

    assert second.locked


# ------------------------------------------------------------- disclosure ---


def test_no_identifier_value_is_ever_stored(clinic):
    """spec §4.2 — the result, the timestamp and the method. Nothing else."""
    session = Session()
    record_lookup(session, PatientLookupResult(match_count=1, patient_id="PT-4101"))
    register_attempt(
        session,
        DOB,
        "1978-03-04",
        ZIP,
        "98101",
        VerificationResult(
            verified=False, patient_id="PT-4101", methods=(DOB, ZIP), checked_at=datetime.now(UTC)
        ),
        clinic,
    )

    serialised = session.model_dump_json()
    assert "1978-03-04" not in serialised
    assert "98101" not in serialised
    assert session.attempt_digests, "the attempt must still be recognisable"


def test_a_repeated_attempt_is_recognised_without_storing_the_values(clinic):
    session = Session()
    record_lookup(session, PatientLookupResult(match_count=1, patient_id="PT-4101"))

    def attempt(first, second):
        return register_attempt(
            session,
            first,
            "1978-03-04",
            second,
            "98101",
            VerificationResult(
                verified=False,
                patient_id="PT-4101",
                methods=(first, second),
                checked_at=datetime.now(UTC),
            ),
            clinic,
        )

    first = attempt(DOB, ZIP)
    second = attempt(DOB, ZIP)

    assert not first.repeat_of_earlier_attempt
    assert second.repeat_of_earlier_attempt


def test_digests_do_not_correlate_across_sessions(clinic):
    """A per-session salt means the same value hashes differently elsewhere."""
    sessions = []
    for _ in range(2):
        session = Session()
        record_lookup(session, PatientLookupResult(match_count=1, patient_id="PT-4101"))
        register_attempt(
            session,
            DOB,
            "1978-03-04",
            ZIP,
            "98101",
            VerificationResult(
                verified=False,
                patient_id="PT-4101",
                methods=(DOB, ZIP),
                checked_at=datetime.now(UTC),
            ),
            clinic,
        )
        sessions.append(session)

    assert sessions[0].attempt_digests != sessions[1].attempt_digests


@pytest.mark.parametrize("status", [SubjectStatus.LOCKED])
def test_a_locked_session_forfeits_its_level(status):
    session = Session()
    session.mark_identified("PT-4101")
    session.mark_locked()

    assert session.attained_level is GateLevel.OPEN
    assert session.patient_id == "PT-4101", "the id is kept for the escalation note"
    assert session.is_locked
