"""P2-T11 — the policy matrix, exhaustively.

Phase 2's exit test. Every cell of the specification §3 table is exercised in
both directions: the level that must be allowed, and every level below it that
must be denied. No model is involved anywhere in this file.
"""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from app.policy.gates import PRECONDITIONS, TOOL_POLICY, PolicyGate, Verdict
from app.policy.messages import DenialCode, Remedy
from app.store.session import GateLevel, Session, SubjectStatus, slot_time_key
from app.tools.schemas import ARGUMENT_MODELS

PINNED = date(2026, 9, 7)


@pytest.fixture
def gate(clinic) -> PolicyGate:
    return PolicyGate(clinic)


# ------------------------------------------------------------- inventory ---


def test_every_function_has_a_policy_and_vice_versa():
    """A function without a policy entry must fail the build, not default open."""
    assert set(TOOL_POLICY) == set(ARGUMENT_MODELS)


def test_the_knowledge_extension_is_gated_at_verified():
    """A complaint is health information about the person describing it, and the
    routing it produces leads straight into booking, which is itself verified."""
    assert TOOL_POLICY["suggest_appointment_type"].level is GateLevel.VERIFIED


def test_every_named_precondition_exists():
    named = {name for policy in TOOL_POLICY.values() for name in policy.requires}
    assert named <= set(PRECONDITIONS), f"undefined preconditions: {named - set(PRECONDITIONS)}"


def test_every_policy_cites_the_specification():
    for name, policy in TOOL_POLICY.items():
        assert policy.rule.startswith("spec§"), f"{name} has no specification citation"


# --------------------------------------------------- the §3 table, in full ---

OPEN_FUNCTIONS = {
    "check_patient_exists",
    "create_new_patient_record",
    "search_available_appointments",
    "get_clinic_hours",
    "check_business_hours",
    "get_clinic_directions",
    "escalate_to_staff",
}
IDENTIFIED_FUNCTIONS = {"verify_patient_identity"}
VERIFIED_FUNCTIONS = {
    "suggest_appointment_type",
    "get_patient_demographics",
    "get_patient_appointments",
    "check_insurance_eligibility",
    "book_appointment",
    "cancel_appointment",
    "reschedule_appointment",
}
CONDITIONAL_FUNCTIONS = {"send_secure_text"}

CLINICAL_FUNCTIONS = {"authenticate_clinical_user"}
"""spec r3 §3.2, not §3.1.

These sit at OPEN on the *patient* ladder because they touch no patient record —
§3.2's last bullet keeps patient-record access behind the ordinary path no matter
who is asking. They are listed apart from OPEN_FUNCTIONS because "open to a
patient session" and "open to whoever holds this session" are different claims,
and the parametrized §3.1 tests below make the first one.
"""


def test_the_policy_table_matches_specification_section_3():
    """The table is the specification. If it drifts, this is where you find out."""
    by_level = {
        "open": {n for n, p in TOOL_POLICY.items() if p.level is GateLevel.OPEN},
        "identified": {n for n, p in TOOL_POLICY.items() if p.level is GateLevel.IDENTIFIED},
        "verified": {
            n
            for n, p in TOOL_POLICY.items()
            if p.level is GateLevel.VERIFIED and p.conditional is None
        },
        "conditional": {n for n, p in TOOL_POLICY.items() if p.conditional is not None},
    }
    assert by_level["open"] == OPEN_FUNCTIONS | CLINICAL_FUNCTIONS
    assert by_level["identified"] == IDENTIFIED_FUNCTIONS
    assert by_level["verified"] == VERIFIED_FUNCTIONS
    assert by_level["conditional"] == CONDITIONAL_FUNCTIONS


# ------------------------------------------------------------- fixtures ---


def anonymous() -> Session:
    return Session()


def identified() -> Session:
    session = Session()
    session.existence_checked = True
    session.mark_identified("PT-4101")
    return session


def verified() -> Session:
    session = identified()
    session.mark_verified([])
    return session


def registered() -> Session:
    session = Session()
    session.existence_checked = True
    session.mark_registered("PT-4900")
    return session


def locked() -> Session:
    session = identified()
    session.failed_attempts = 3
    session.mark_locked()
    return session


VALID_ARGS: dict[str, dict] = {
    "check_patient_exists": {
        "first_name": "Amara",
        "last_name": "Osei",
        "date_of_birth": "1978-03-04",
    },
    "verify_patient_identity": {
        "patient_id": "PT-4101",
        "identifier_1_type": "dob",
        "identifier_1_value": "1978-03-04",
        "identifier_2_type": "address_zip",
        "identifier_2_value": "98101",
    },
    "get_patient_demographics": {"patient_id": "PT-4101"},
    "get_patient_appointments": {"patient_id": "PT-4101"},
    "create_new_patient_record": {
        "first_name": "Ada",
        "last_name": "Nwosu",
        "date_of_birth": "1990-01-01",
        "phone_number": "+12065550999",
    },
    "search_available_appointments": {
        "appointment_type": "follow_up",
        "date_range_start": "2026-09-07",
        "date_range_end": "2026-09-14",
        "modality": "any",
    },
    "book_appointment": {
        "appointment_date": "2026-09-08",
        "appointment_time": "09:30",
        "reason_for_visit": "Blood pressure review",
        "patient_id": "PT-4101",
    },
    "cancel_appointment": {
        "patient_id": "PT-4101",
        "appointment_id": "AP-77301",
        "cancellation_reason": "Schedule conflict",
    },
    "reschedule_appointment": {
        "patient_id": "PT-4101",
        "current_appointment_id": "AP-77301",
        "new_appointment_slot_id": "SL-2026-09-14-0-1",
        "reschedule_reason": "Work conflict",
    },
    "check_insurance_eligibility": {"patient_id": "PT-4101", "service_date": "2026-09-13"},
    "send_secure_text": {"phone_number": "+12065550142", "message_type": "intake_forms"},
    "get_clinic_hours": {"date": "2026-09-12"},
    "check_business_hours": {},
    "get_clinic_directions": {"location": "main_clinic"},
    "escalate_to_staff": {"reason": "other", "priority": "routine", "notes": "Wants a person."},
    "suggest_appointment_type": {"complaint": "itchy rash between my toes"},
    "authenticate_clinical_user": {
        "staff_id": "STAFF-2001",
        "credential_token": "fixture-token-alvarez",
        "asserted_role": "physician",
    },
}


def satisfy_preconditions(session: Session, fn_name: str) -> Session:
    """Give the session whatever the function's preconditions require, so the
    authorization check is what the test is actually measuring."""
    session.seen_appointment_ids = {"AP-77301"}
    session.seen_slot_ids = {"SL-2026-09-14-0-1"}
    session.offered_times = {slot_time_key(date(2026, 9, 8), time(9, 30))}
    session.booked_service_dates = {date(2026, 9, 13)}
    session.confirmed_phone = "+12065550142"
    session.existence_checked = True
    return session


def test_every_policy_entry_has_a_valid_argument_fixture():
    assert set(VALID_ARGS) == set(TOOL_POLICY)


# ------------------------------------------------- allowed at the right level ---


@pytest.mark.parametrize("fn_name", sorted(OPEN_FUNCTIONS))
def test_open_functions_are_callable_with_no_identity(gate, fn_name):
    session = satisfy_preconditions(anonymous(), fn_name)
    verdict = gate.evaluate(fn_name, VALID_ARGS[fn_name], session)

    assert verdict.allowed, f"{fn_name}: {verdict.code} — {verdict.detail}"


@pytest.mark.parametrize("fn_name", sorted(VERIFIED_FUNCTIONS))
def test_verified_functions_are_callable_once_verified(gate, fn_name):
    session = satisfy_preconditions(verified(), fn_name)
    verdict = gate.evaluate(fn_name, VALID_ARGS[fn_name], session)

    assert verdict.allowed, f"{fn_name}: {verdict.code} — {verdict.detail}"


def test_verification_is_callable_once_identified(gate):
    session = satisfy_preconditions(identified(), "verify_patient_identity")
    assert gate.evaluate(
        "verify_patient_identity", VALID_ARGS["verify_patient_identity"], session
    ).allowed


# ----------------------------------------------- denied below the right level ---


@pytest.mark.parametrize("fn_name", sorted(VERIFIED_FUNCTIONS))
@pytest.mark.parametrize("builder", [anonymous, identified], ids=["anonymous", "identified"])
def test_verified_functions_are_denied_below_verified(gate, fn_name, builder):
    """The other direction of every §3 cell — the one that matters."""
    session = satisfy_preconditions(builder(), fn_name)
    verdict = gate.evaluate(fn_name, VALID_ARGS[fn_name], session)

    assert not verdict.allowed
    assert verdict.code is DenialCode.VERIFICATION_REQUIRED
    assert verdict.required is GateLevel.VERIFIED


def test_verification_is_denied_before_identification(gate):
    session = satisfy_preconditions(anonymous(), "verify_patient_identity")
    session.seen_patient_ids = {"PT-4101"}  # isolate the level check
    verdict = gate.evaluate(
        "verify_patient_identity", VALID_ARGS["verify_patient_identity"], session
    )

    assert not verdict.allowed
    assert verdict.required is GateLevel.IDENTIFIED


@pytest.mark.parametrize("fn_name", sorted(VERIFIED_FUNCTIONS))
def test_a_locked_session_reaches_nothing_protected(gate, fn_name):
    session = satisfy_preconditions(locked(), fn_name)
    verdict = gate.evaluate(fn_name, VALID_ARGS[fn_name], session)

    assert not verdict.allowed
    assert verdict.remedy_key is Remedy.ESCALATE_LOCKED


def test_escalation_is_always_available_even_when_locked(gate):
    """spec §4.12 — always honour a request to speak with a person."""
    session = locked()
    assert gate.evaluate("escalate_to_staff", VALID_ARGS["escalate_to_staff"], session).allowed


# ------------------------------------------------------------- registered ---


def test_a_registered_patient_may_book(gate):
    """spec §3 — new-patient registration permitted before booking."""
    session = satisfy_preconditions(registered(), "book_appointment")
    args = {**VALID_ARGS["book_appointment"], "patient_id": "PT-4900"}

    assert gate.evaluate("book_appointment", args, session).allowed


def test_a_registered_patient_may_not_read_demographics(gate):
    """REGISTERED confers booking rights and nothing else (design §6)."""
    session = satisfy_preconditions(registered(), "get_patient_demographics")
    verdict = gate.evaluate("get_patient_demographics", {"patient_id": "PT-4900"}, session)

    assert not verdict.allowed
    assert verdict.code is DenialCode.VERIFICATION_REQUIRED


def test_a_registered_patient_may_not_book_for_someone_else(gate):
    """Booking rights extend to the record they created, and no further."""
    session = satisfy_preconditions(registered(), "book_appointment")
    session.seen_patient_ids = session.seen_patient_ids | {"PT-4101"}
    args = {**VALID_ARGS["book_appointment"], "patient_id": "PT-4101"}

    verdict = gate.evaluate("book_appointment", args, session)
    assert not verdict.allowed


# ------------------------------------------------- send_secure_text branching ---


def test_directions_need_only_a_confirmed_number(gate):
    """spec §4.10 — directions carry no health information."""
    session = anonymous()
    session.confirm_phone("+12065550142")

    verdict = gate.evaluate(
        "send_secure_text",
        {"phone_number": "+12065550142", "message_type": "directions"},
        session,
    )
    assert verdict.allowed
    assert verdict.required is GateLevel.NUMBER_CONFIRMED


def test_directions_still_need_the_number_confirmed(gate):
    verdict = gate.evaluate(
        "send_secure_text",
        {"phone_number": "+12065550142", "message_type": "directions"},
        anonymous(),
    )
    assert not verdict.allowed
    assert verdict.remedy_key is Remedy.CONFIRM_PHONE_NUMBER


@pytest.mark.parametrize(
    "message_type",
    ["intake_forms", "appointment_confirmation", "telehealth_link", "portal_access"],
)
def test_every_other_message_type_requires_verification(gate, message_type):
    session = anonymous()
    session.confirm_phone("+12065550142")

    verdict = gate.evaluate(
        "send_secure_text",
        {"phone_number": "+12065550142", "message_type": message_type},
        session,
    )
    assert not verdict.allowed
    assert verdict.required is GateLevel.VERIFIED


def test_a_verified_patient_may_receive_any_message_type(gate):
    session = verified()
    session.confirm_phone("+12065550142")

    assert gate.evaluate(
        "send_secure_text",
        {"phone_number": "+12065550142", "message_type": "telehealth_link"},
        session,
    ).allowed


def test_texts_only_go_to_the_confirmed_number(gate):
    session = verified()
    session.confirm_phone("+12065550142")

    verdict = gate.evaluate(
        "send_secure_text",
        {"phone_number": "+12065550188", "message_type": "intake_forms"},
        session,
    )
    assert not verdict.allowed
    assert verdict.remedy_key is Remedy.CONFIRM_PHONE_NUMBER


# ------------------------------------------------------------ check order ---


def test_schema_is_checked_before_authorization(gate):
    """A malformed call is rejected without revealing anything about identity."""
    verdict = gate.evaluate("get_patient_demographics", {"patient_id": ""}, anonymous())

    assert verdict.code is DenialCode.INVALID_ARGUMENTS
    assert verdict.required is None


def test_authorization_is_checked_before_provenance(gate):
    """An unverified caller learns "verify first", not "that id is unknown"."""
    verdict = gate.evaluate("get_patient_appointments", {"patient_id": "PT-9999"}, anonymous())

    assert verdict.code is DenialCode.VERIFICATION_REQUIRED


def test_provenance_is_checked_before_preconditions(gate):
    session = satisfy_preconditions(verified(), "cancel_appointment")
    session.seen_appointment_ids = set()

    verdict = gate.evaluate(
        "cancel_appointment",
        {**VALID_ARGS["cancel_appointment"], "appointment_id": "AP-00000"},
        session,
    )
    assert verdict.code is DenialCode.UNKNOWN_REFERENCE


# ----------------------------------------------------------- preconditions ---


def test_registration_requires_an_existence_check_first(gate):
    """spec §4.4."""
    session = anonymous()
    verdict = gate.evaluate(
        "create_new_patient_record", VALID_ARGS["create_new_patient_record"], session
    )

    assert not verdict.allowed
    assert verdict.remedy_key is Remedy.CHECK_EXISTENCE_FIRST


def test_a_suspected_duplicate_blocks_registration(gate):
    """spec §4.4 — escalate instead of creating a second record."""
    session = anonymous()
    session.existence_checked = True
    session.duplicate_suspected = True

    verdict = gate.evaluate(
        "create_new_patient_record", VALID_ARGS["create_new_patient_record"], session
    )
    assert not verdict.allowed
    assert verdict.remedy_key is Remedy.ESCALATE_DUPLICATE


def test_booking_a_time_no_search_offered_is_refused(gate):
    """spec §4.6 — the model may not invent availability."""
    session = satisfy_preconditions(verified(), "book_appointment")
    session.offered_times = set()

    verdict = gate.evaluate("book_appointment", VALID_ARGS["book_appointment"], session)
    assert not verdict.allowed
    assert verdict.remedy_key is Remedy.SEARCH_SLOTS_FIRST


def test_eligibility_needs_a_confirmed_service_date(gate):
    """spec §4.9."""
    session = satisfy_preconditions(verified(), "check_insurance_eligibility")
    session.booked_service_dates = set()

    verdict = gate.evaluate(
        "check_insurance_eligibility", VALID_ARGS["check_insurance_eligibility"], session
    )
    assert not verdict.allowed
    assert verdict.remedy_key is Remedy.CONFIRM_SERVICE_DATE


def test_an_unconfigured_location_is_refused(gate, clinic):
    session = anonymous()
    stripped = clinic.model_copy(
        update={"locations": {"main_clinic": clinic.locations["main_clinic"]}}
    )
    verdict = PolicyGate(stripped).evaluate(
        "get_clinic_directions", {"location": "satellite_office"}, session
    )

    assert not verdict.allowed
    assert verdict.remedy_key is Remedy.UNKNOWN_LOCATION


def test_a_repeated_identifier_combination_is_refused(gate):
    """Three types make three pairs; retrying one wastes an attempt."""
    session = satisfy_preconditions(identified(), "verify_patient_identity")
    session.attempted_combinations = {"address_zip+dob"}

    verdict = gate.evaluate(
        "verify_patient_identity", VALID_ARGS["verify_patient_identity"], session
    )
    assert not verdict.allowed
    assert verdict.remedy_key is Remedy.TRY_DIFFERENT_IDENTIFIERS


# ------------------------------------------------------- one subject only ---


def test_a_verified_session_may_only_act_on_the_record_it_verified(gate):
    """A session is verified for a person, not for the conversation.

    The provenance ledger says an id is real. It does not say it is *this*
    patient's, so a verified session must not be able to read a second record
    merely because an earlier lookup put it in the ledger.
    """
    session = satisfy_preconditions(verified(), "get_patient_appointments")
    session.seen_patient_ids = {"PT-4101", "PT-4103"}

    assert gate.evaluate("get_patient_appointments", {"patient_id": "PT-4101"}, session).allowed

    other = gate.evaluate("get_patient_appointments", {"patient_id": "PT-4103"}, session)
    assert not other.allowed
    assert other.code is DenialCode.VERIFICATION_REQUIRED
    assert other.remedy_key is Remedy.WRONG_SUBJECT


def test_looking_up_a_different_patient_drops_verification(gate):
    """Otherwise the second patient's record becomes readable to the first."""
    from app.policy.verification import record_lookup
    from app.ports import PatientLookupResult

    session = Session()
    record_lookup(session, PatientLookupResult(match_count=1, patient_id="PT-4101"))
    session.mark_verified([])
    assert session.status is SubjectStatus.VERIFIED

    record_lookup(session, PatientLookupResult(match_count=1, patient_id="PT-4103"))

    assert session.patient_id == "PT-4103"
    assert session.status is SubjectStatus.IDENTIFIED, "verification must not carry over"
    assert session.verified_at is None


def test_re_looking_up_the_same_patient_keeps_verification(gate):
    """The model often re-checks after confirming spelling; that must be free."""
    from app.policy.verification import record_lookup
    from app.ports import PatientLookupResult

    session = Session()
    record_lookup(session, PatientLookupResult(match_count=1, patient_id="PT-4101"))
    session.mark_verified([])
    record_lookup(session, PatientLookupResult(match_count=1, patient_id="PT-4101"))

    assert session.status is SubjectStatus.VERIFIED


def test_escalation_is_unaffected_by_the_subject_check(gate):
    """spec §4.12 — escalation is OPEN level and must always be available."""
    session = satisfy_preconditions(verified(), "escalate_to_staff")
    session.seen_patient_ids = {"PT-4101", "PT-4103"}

    assert gate.evaluate(
        "escalate_to_staff",
        {"reason": "other", "priority": "routine", "notes": "x", "patient_id": "PT-4103"},
        session,
    ).allowed


# ------------------------------------------------------------- disclosure ---


def test_no_denial_reveals_a_record_value(gate):
    """spec §3 rule 5 — a refusal must not disclose."""
    session = satisfy_preconditions(anonymous(), "get_patient_demographics")
    verdict = gate.evaluate("get_patient_demographics", {"patient_id": "PT-4101"}, session)

    combined = f"{verdict.message} {verdict.remedy}"
    for leak in ["Amara", "Osei", "98101", "1978-03-04", "2065550142"]:
        assert leak not in combined


def test_validation_detail_never_carries_the_rejected_value(gate):
    """An invalid ZIP must not put the ZIP into the audit detail."""
    verdict = gate.evaluate(
        "verify_patient_identity",
        {
            "patient_id": "PT-4101",
            "identifier_1_type": "dob",
            "identifier_1_value": "1978-03-04",
            "identifier_2_type": "dob",
            "identifier_2_value": "1978-03-04",
        },
        identified(),
    )

    assert verdict.code is DenialCode.INVALID_ARGUMENTS
    assert "1978-03-04" not in verdict.detail


def test_unknown_functions_are_refused(gate):
    verdict = gate.evaluate("delete_patient_record", {}, verified())

    assert not verdict.allowed
    assert verdict.code is DenialCode.UNKNOWN_FUNCTION


def test_an_allowed_verdict_carries_coerced_arguments(gate):
    session = satisfy_preconditions(verified(), "check_insurance_eligibility")
    verdict = gate.evaluate(
        "check_insurance_eligibility", VALID_ARGS["check_insurance_eligibility"], session
    )

    assert isinstance(verdict, Verdict)
    assert verdict.args is not None
    assert verdict.args.service_date == date(2026, 9, 13)


def test_audit_view_redacts_arguments(gate):
    view = gate.audit_view("create_new_patient_record", VALID_ARGS["create_new_patient_record"])

    assert view["date_of_birth"] == "<dob>"
    assert view["phone_number"] == "<phone>"
    assert view["first_name"] == "<name>", "a name identifies as surely as a date of birth"


# ------------------------------------------------------ schema-enforced rules ---


def test_schema_enforced_rules_are_documented_not_duplicated():
    """Three §3 rules are argument invariants, enforced in the models.

    They are listed here so a reviewer reading the policy table still sees the
    complete picture, but they are not re-implemented as preconditions.
    """
    documented = {rule for policy in TOOL_POLICY.values() for rule in policy.enforced_by_schema}
    assert any("identifier types must differ" in rule for rule in documented)
    assert any("date_range_end" in rule for rule in documented)
    assert any("first and last name" in rule for rule in documented)

    named = {name for policy in TOOL_POLICY.values() for name in policy.requires}
    assert "DISTINCT_ID_TYPES" not in named
    assert "VALID_DATE_RANGE" not in named


@pytest.mark.parametrize("fn_name", sorted(TOOL_POLICY))
def test_every_function_denies_a_call_with_a_bad_enum_or_missing_field(gate, fn_name):
    """Check 1 fires for every function, not just the ones with fixtures."""
    session = satisfy_preconditions(verified(), fn_name)
    verdict = gate.evaluate(fn_name, {"definitely_not_a_field": True}, session)

    assert not verdict.allowed
    assert verdict.code is DenialCode.INVALID_ARGUMENTS


def test_search_range_ordering_is_still_enforced_somewhere(gate):
    """Dropped from TOOL_POLICY, so prove the model still catches it."""
    verdict = gate.evaluate(
        "search_available_appointments",
        {
            "appointment_type": "follow_up",
            "date_range_start": "2026-09-14",
            "date_range_end": "2026-09-07",
            "modality": "any",
        },
        anonymous(),
    )
    assert not verdict.allowed
    assert verdict.code is DenialCode.INVALID_ARGUMENTS


def test_future_service_dates_outside_the_ledger_are_refused(gate):
    session = satisfy_preconditions(verified(), "check_insurance_eligibility")
    far_future = (PINNED + timedelta(days=400)).isoformat()

    verdict = gate.evaluate(
        "check_insurance_eligibility",
        {"patient_id": "PT-4101", "service_date": far_future},
        session,
    )
    assert not verdict.allowed
    assert verdict.remedy_key is Remedy.CONFIRM_SERVICE_DATE


# ------------------------------------------------- a new patient's first visit ---
#
# Reported from a live session: registered as a new patient, asked for an
# appointment, got one labelled "Follow-up Visit". Nothing in the system had an
# opinion about visit types, so whatever the model picked stood.


def test_a_newly_registered_patient_cannot_search_for_a_follow_up(gate):
    """The reported defect. There is no earlier visit to follow up on."""
    verdict = gate.evaluate(
        "search_available_appointments",
        VALID_ARGS["search_available_appointments"],  # appointment_type: follow_up
        satisfy_preconditions(registered(), "search_available_appointments"),
    )

    assert not verdict.allowed
    assert verdict.code is DenialCode.PRECONDITION_FAILED
    assert verdict.remedy_key is Remedy.NEW_PATIENT_FIRST_VISIT


def test_a_newly_registered_patient_cannot_book_a_follow_up(gate):
    """Blocked at booking too — the search is not the only way in."""
    session = satisfy_preconditions(registered(), "book_appointment")
    args = {
        **VALID_ARGS["book_appointment"],
        "patient_id": "PT-4900",
        "appointment_type": "follow_up",
    }

    verdict = gate.evaluate("book_appointment", args, session)

    assert not verdict.allowed
    assert verdict.remedy_key is Remedy.NEW_PATIENT_FIRST_VISIT


@pytest.mark.parametrize("visit_type", ["new_patient", "sick_visit", "wellness", "telehealth"])
def test_every_other_visit_type_is_still_open_to_a_new_patient(gate, visit_type):
    """The guard removes one impossible option, not the patient's choices."""
    session = satisfy_preconditions(registered(), "search_available_appointments")
    args = {**VALID_ARGS["search_available_appointments"], "appointment_type": visit_type}

    assert gate.evaluate("search_available_appointments", args, session).allowed


def test_an_established_patient_may_still_book_a_follow_up(gate):
    """The whole point of the visit type. A verified patient has history."""
    session = satisfy_preconditions(verified(), "search_available_appointments")

    assert gate.evaluate(
        "search_available_appointments", VALID_ARGS["search_available_appointments"], session
    ).allowed


def test_an_anonymous_caller_may_still_search_for_a_follow_up(gate):
    """Someone who has not identified themselves may well be an established
    patient. Blocking their search would be a worse error than a wrong label."""
    session = satisfy_preconditions(anonymous(), "search_available_appointments")

    assert gate.evaluate(
        "search_available_appointments", VALID_ARGS["search_available_appointments"], session
    ).allowed


def test_the_remedy_tells_the_model_what_to_do_instead(gate):
    """A denial the model cannot act on costs a turn and confuses the patient."""
    from app.policy.messages import remedy_text

    text = remedy_text(Remedy.NEW_PATIENT_FIRST_VISIT)

    assert "new_patient" in text
    assert "follow up" in text


def test_a_registered_patient_may_be_routed(gate):
    """They can already book, and the complaint is their own words coming back
    to them. Denying it sent the model to ask a just-registered patient to
    verify against a record built from their own answers — unreachable."""
    verdict = gate.evaluate(
        "suggest_appointment_type",
        VALID_ARGS["suggest_appointment_type"],
        satisfy_preconditions(registered(), "suggest_appointment_type"),
    )

    assert verdict.allowed, verdict.remedy
