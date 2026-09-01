"""C1 — Clinical Assistant authentication (spec r3 §3.2, §4.13).

The load-bearing tests are the refusals. A happy path that works proves the
function is wired; the refusals prove it is the thing §3.2 asks for. Each
directory fixture is wrong in exactly one way, so a test cannot pass because
something else about the record was also unacceptable.

Two claims here matter more than the rest:

* a role claimed in conversation is not a role (§3.2 item 3), and
* authentication is not a way to reach a patient's record (§3.2 last bullet).

No model, no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.policy.decorator import session_scope
from app.policy.gates import PolicyGate
from app.store.session import Role, Session
from app.tools import registry
from app.tools.schemas import ClinicalRole

# The fixture directory, by the one thing wrong with each row.
GOOD = ("STAFF-2001", "fixture-token-alvarez", ClinicalRole.PHYSICIAN)
SHARED = ("STAFF-2900", "fixture-token-triage-desk", ClinicalRole.REGISTERED_NURSE)
NON_CLINICAL = ("STAFF-3001", "fixture-token-blake", ClinicalRole.REGISTERED_NURSE)
EXPIRED_CRED = ("STAFF-2006", "fixture-token-chen", ClinicalRole.PHYSICIAN)

ALL_TOKENS = [
    "fixture-token-alvarez",
    "fixture-token-okafor",
    "fixture-token-reyes",
    "fixture-token-iqbal",
    "fixture-token-novak",
    "fixture-token-triage-desk",
    "fixture-token-blake",
    "fixture-token-chen",
]


class Recorder:
    """An audit sink that keeps what it was told, so §4.13's recording
    requirement can be asserted rather than assumed."""

    def __init__(self) -> None:
        self.notes: list[tuple[str, dict[str, Any]]] = []

    def gate_decision(self, function, verdict, session) -> None:  # noqa: ANN001
        return None

    def tool_result(self, function, result, session) -> None:  # noqa: ANN001
        return None

    def note(self, kind: str, detail: dict[str, Any]) -> None:
        self.notes.append((kind, dict(detail)))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.notes]

    def last(self) -> dict[str, Any]:
        return self.notes[-1][1]


@pytest.fixture
def audit() -> Recorder:
    return Recorder()


@pytest.fixture
def call(sim, clinic, audit):
    """Call a tool inside a clinical session, through the gate."""

    def _call(session: Session, name: str = "authenticate_clinical_user", **kwargs: Any) -> Any:
        with (
            session_scope(session, gate=PolicyGate(clinic), audit=audit),
            registry.backend_scope(sim),
        ):
            return json.loads(registry.load()[name].call(kwargs))

    return _call


def clinical() -> Session:
    return Session(role=Role.CLINICAL_ASSISTANT, channel="clinical")


def creds(who: tuple[str, str, ClinicalRole]) -> dict[str, Any]:
    staff_id, token, role = who
    return {"staff_id": staff_id, "credential_token": token, "asserted_role": role.value}


# ---------------------------------------------------------------- success ---


def test_a_clinician_authenticates(call):
    session = clinical()

    result = call(session, **creds(GOOD))

    assert result["authenticated"] is True
    assert result["role"] == "physician"
    assert session.clinical_authentication_valid
    assert session.effective_role is Role.CLINICAL_ASSISTANT


@pytest.mark.parametrize(
    "who",
    [
        ("STAFF-2001", "fixture-token-alvarez", ClinicalRole.PHYSICIAN),
        ("STAFF-2002", "fixture-token-okafor", ClinicalRole.NURSE_PRACTITIONER),
        ("STAFF-2003", "fixture-token-reyes", ClinicalRole.PHYSICIAN_ASSISTANT),
        ("STAFF-2004", "fixture-token-iqbal", ClinicalRole.REGISTERED_NURSE),
        ("STAFF-2005", "fixture-token-novak", ClinicalRole.CLINICAL_PHARMACIST),
    ],
    ids=lambda w: w[2].value if isinstance(w, tuple) else str(w),
)
def test_every_licensed_role_of_section_4_13_can_authenticate(call, who):
    """All five, so a role enumerated in the spec and unreachable in practice
    fails here rather than in a demo."""
    assert call(clinical(), **creds(who))["authenticated"] is True


def test_the_established_scope_is_reported_once(call):
    """spec §4.13 — *"State the established role and its scope once at session
    start, so the clinician knows which capabilities are live."* The scope has
    to come from the result; the model cannot be trusted to know it."""
    result = call(clinical(), **creds(GOOD))

    assert set(result["scope"]) == {
        "search_clinical_knowledge",
        "summarize_diagnostic_considerations",
        "get_dosage_information",
    }
    assert "once" in result["next_step"]


def test_the_expiry_comes_from_clinic_configuration(call, clinic):
    session = clinical()
    before = datetime.now(UTC)

    call(session, **creds(GOOD))

    assert session.expires_at is not None
    window = session.expires_at - before
    configured = timedelta(minutes=clinic.clinical.session_minutes)
    # The window is measured from before the call, so it exceeds the configured
    # interval by however long the call took.
    assert configured <= window < configured + timedelta(seconds=10)


def test_the_standing_limits_are_stated_at_authentication(call):
    """spec §7.2 — the corpus limits must be stated *"when they bear on the
    answer"*, and they bear on every answer this role can produce."""
    limits = call(clinical(), **creds(GOOD))["limits"]

    assert "not a diagnosis" in limits.lower()
    assert "formulary" in limits.lower()
    assert "treating clinician" in limits.lower()


# --------------------------------------------------------------- refusals ---


def test_an_unknown_staff_id_and_a_wrong_token_are_indistinguishable(call):
    """Telling them apart turns this function into a staff directory oracle —
    the same argument as §3.1 rule 5 in the patient case."""
    unknown = call(
        clinical(),
        staff_id="STAFF-0000",
        credential_token="fixture-token-alvarez",
        asserted_role="physician",
    )
    wrong_token = call(
        clinical(),
        staff_id="STAFF-2001",
        credential_token="definitely-not-the-token",
        asserted_role="physician",
    )

    assert unknown["error"] == wrong_token["error"] == "authentication_failed"
    assert unknown["message"] == wrong_token["message"]


def test_a_shared_account_is_refused(call):
    """spec §3.2 — *"Anonymous or shared clinical accounts must be rejected at
    authentication."* This fixture holds a licensed role and a valid credential,
    so sharedness is the only thing wrong with it."""
    session = clinical()

    result = call(session, **creds(SHARED))

    assert result["error"] == "shared_account_refused"
    assert not session.clinical_authentication_valid
    assert session.effective_role is Role.SYSTEM


def test_an_expired_credential_is_refused(call):
    session = clinical()

    result = call(session, **creds(EXPIRED_CRED))

    assert result["error"] == "credential_expired"
    assert session.effective_role is Role.SYSTEM


def test_a_non_clinical_directory_role_is_refused(call):
    """In the directory, and not a clinician. Authentication succeeded;
    authorization did not, and the two are different answers."""
    result = call(clinical(), **creds(NON_CLINICAL))

    assert result["error"] == "not_a_clinical_role"


def test_a_directory_outage_does_not_read_as_not_a_clinician(call, sim):
    """spec §4.13 — an outage is a *failure*, and must not be mistaken for a
    finding about the person."""
    sim.faults.arm("IdentityProvider", "authenticate", "directory_unavailable")

    result = call(clinical(), **creds(GOOD))

    assert result["error"] != "not_a_clinical_role"
    assert "identity provider" in result["message"].lower()
    assert "outage" in result["message"].lower()
    # §6's clinical bullet — no degrading to a general answer.
    assert "own knowledge" in result["remedy"]
    assert "patient" not in result["remedy"].lower()


@pytest.mark.parametrize(
    "who", [SHARED, NON_CLINICAL, EXPIRED_CRED], ids=["shared", "non_clinical", "expired"]
)
def test_no_refusal_leaves_a_usable_session(call, who):
    """The property that matters across every refusal branch at once."""
    session = clinical()

    call(session, **creds(who))

    assert session.staff_id is None
    assert session.effective_role is Role.SYSTEM
    assert not session.clinical_authentication_valid


# ------------------------------------------- a claimed role is not a role ---


def test_a_claimed_role_the_directory_disagrees_with_is_refused(call):
    """spec §3.2 item 3 — *"A role asserted in conversation text is not a role
    assertion and must be rejected."* RN Iqbal claiming physician."""
    session = clinical()

    result = call(
        session,
        staff_id="STAFF-2004",
        credential_token="fixture-token-iqbal",
        asserted_role="physician",
    )

    assert result["error"] == "role_mismatch"
    assert session.effective_role is Role.SYSTEM


def test_a_mismatch_downward_is_refused_too(call):
    """No ordering over clinical roles exists in this system, and inventing one
    to decide which mismatches are "upward" would be the kind of guess the rest
    of the design exists to avoid. Dr Alvarez claiming registered_nurse."""
    result = call(
        clinical(),
        staff_id="STAFF-2001",
        credential_token="fixture-token-alvarez",
        asserted_role="registered_nurse",
    )

    assert result["error"] == "role_mismatch"


def test_the_session_records_the_directory_role_not_the_claim(call):
    """The positive form of the same rule: what lands on the session is what the
    provider said."""
    session = clinical()

    call(session, **creds(("STAFF-2004", "fixture-token-iqbal", ClinicalRole.REGISTERED_NURSE)))

    assert session.asserted_role is ClinicalRole.REGISTERED_NURSE


def test_a_role_not_in_the_clinic_permitted_list_is_refused(call, sim, clinic, audit):
    """Decision 5 — a licensed role this clinic has not admitted. Distinct from
    not_a_clinical_role: the person *is* a clinician."""
    narrowed = clinic.model_copy(deep=True)
    narrowed.clinical.permitted_roles = [ClinicalRole.PHYSICIAN]
    session = clinical()

    with (
        session_scope(session, gate=PolicyGate(clinic), audit=audit),
        registry.backend_scope(sim),
    ):
        from unittest.mock import patch

        with patch("app.tools.clinical.get_clinic_config", return_value=narrowed):
            result = json.loads(
                registry.load()["authenticate_clinical_user"].call(
                    creds(("STAFF-2004", "fixture-token-iqbal", ClinicalRole.REGISTERED_NURSE))
                )
            )

    assert result["error"] == "role_not_permitted"


# ------------------------------------------------------- session hygiene ---


def test_a_patient_session_cannot_authenticate(call):
    """Defence in depth. §2 keeps the function out of a patient session's tool
    schema entirely, so this is unreachable through the model — and this is the
    one function whose failure mode is a privilege escalation."""
    result = call(Session(), **creds(GOOD))

    assert result["error"] == "not_a_clinical_session"


def test_re_authenticating_into_a_live_session_does_not_refresh_it(call):
    """spec §3.2 — write-once. Extending a session by authenticating into it
    again is exactly what must not work."""
    session = clinical()
    call(session, **creds(GOOD))
    first_expiry = session.expires_at

    result = call(
        session, **creds(("STAFF-2002", "fixture-token-okafor", ClinicalRole.NURSE_PRACTITIONER))
    )

    assert result["already_authenticated"] is True
    assert session.expires_at == first_expiry
    assert session.staff_id == "STAFF-2001"


def test_authentication_confers_no_patient_record_access(call, clinic):
    """spec §3.2 last bullet — clinical authentication *"does not confer access
    to an arbitrary patient's record"*. The two axes, asserted together."""
    session = clinical()
    call(session, **creds(GOOD))

    verdict = PolicyGate(clinic).evaluate(
        "get_patient_demographics", {"patient_id": "PT-4101"}, session
    )

    assert not verdict.allowed


def test_a_disabled_clinic_refuses_everyone(call, sim, clinic, audit):
    off = clinic.model_copy(deep=True)
    off.clinical.enabled = False
    session = clinical()

    with (
        session_scope(session, gate=PolicyGate(clinic), audit=audit),
        registry.backend_scope(sim),
    ):
        from unittest.mock import patch

        with patch("app.tools.clinical.get_clinic_config", return_value=off):
            result = json.loads(registry.load()["authenticate_clinical_user"].call(creds(GOOD)))

    assert result["error"] == "clinical_role_disabled"


# ------------------------------------------------------------- the audit ---


def test_a_successful_authentication_is_recorded(call, audit):
    """spec §4.13 — *"Record authentication outcome, staff identifier, asserted
    role, timestamp, and channel in the audit log."*"""
    call(clinical(), **creds(GOOD))

    assert "clinical_auth" in audit.kinds()
    detail = audit.last()
    assert detail["outcome"] == "authenticated"
    assert detail["staff_id"] == "STAFF-2001"
    assert detail["asserted_role"] == "physician"
    assert detail["channel"] == "clinical"


@pytest.mark.parametrize(
    "who,outcome",
    [
        (SHARED, "shared_account"),
        (NON_CLINICAL, "not_a_clinical_role"),
        (EXPIRED_CRED, "credential_expired"),
    ],
    ids=["shared", "non_clinical", "expired"],
)
def test_every_refusal_is_recorded_with_its_reason(call, audit, who, outcome):
    """A refused authentication is more interesting to an auditor than a
    successful one, so silence on the failure path would be the wrong way round."""
    call(clinical(), **creds(who))

    assert audit.last()["outcome"] == outcome


def test_an_elevation_attempt_is_recorded_with_both_roles(call, audit):
    """The claim and the directory's answer, so a reviewer can see what was
    attempted rather than only that something was refused."""
    call(
        clinical(),
        staff_id="STAFF-2004",
        credential_token="fixture-token-iqbal",
        asserted_role="physician",
    )

    detail = audit.last()
    assert detail["outcome"] == "role_mismatch"
    assert detail["claimed_role"] == "physician"
    assert detail["directory_role"] == "registered_nurse"


@pytest.mark.parametrize("token", ALL_TOKENS)
def test_no_credential_token_ever_reaches_the_audit(call, audit, token):
    """spec §3.2 item 2. Every fixture token, against everything the sink was
    told, on both the success and the failure path."""
    call(clinical(), staff_id="STAFF-2001", credential_token=token, asserted_role="physician")

    assert token not in json.dumps(audit.notes)


def test_the_token_is_redacted_from_the_gate_record():
    """The other place arguments land. The gate logs a redacted view of every
    call, and this one carries a credential."""
    from app.policy.gates import PolicyGate as Gate

    view = Gate().audit_view(
        "authenticate_clinical_user",
        {
            "staff_id": "STAFF-2001",
            "credential_token": "fixture-token-alvarez",
            "asserted_role": "physician",
        },
    )

    assert view["credential_token"] == "<credential>"
    assert "fixture-token-alvarez" not in json.dumps(view)


def test_the_staff_id_survives_redaction():
    """spec §3.2 — every clinical call must be *"auditable to a named
    individual"*. A log that masked this could not do that job, so staff_id is a
    safe reference like patient_id, not a redacted field."""
    from app.policy.gates import PolicyGate as Gate

    view = Gate().audit_view(
        "authenticate_clinical_user",
        {
            "staff_id": "STAFF-2001",
            "credential_token": "x" * 12,
            "asserted_role": "physician",
        },
    )

    assert view["staff_id"] == "STAFF-2001"


# ------------------------------------------------------------ the provider ---


def test_the_assertion_has_no_field_that_could_carry_a_token(sim):
    assertion = sim.identity.authenticate("STAFF-2001", "fixture-token-alvarez")

    assert assertion is not None
    assert "fixture-token-alvarez" not in json.dumps(assertion.model_dump())
    assert not any("token" in field for field in assertion.model_dump())


def test_the_provider_reports_rather_than_decides(sim):
    """A directory has no opinion on whether a shared account may hold a
    clinical session — that is clinic policy, and it lives in the tool."""
    shared = sim.identity.authenticate("STAFF-2900", "fixture-token-triage-desk")

    assert shared is not None
    assert shared.shared_account is True
    assert shared.role is ClinicalRole.REGISTERED_NURSE


def test_the_directory_fixture_covers_every_branch(sim):
    """Guards against a refusal path becoming unreachable because a fixture was
    tidied up."""
    records = [
        sim.identity.authenticate(*c)
        for c in [
            ("STAFF-2900", "fixture-token-triage-desk"),
            ("STAFF-3001", "fixture-token-blake"),
            ("STAFF-2006", "fixture-token-chen"),
        ]
    ]

    assert records[0] is not None and records[0].shared_account
    assert records[1] is not None and records[1].role is None
    assert records[2] is not None and records[2].credential_expired
    assert sim.identity.directory_size() == 8


def test_the_provider_satisfies_the_port(sim):
    from app.ports import IdentityProvider

    assert isinstance(sim.identity, IdentityProvider)
