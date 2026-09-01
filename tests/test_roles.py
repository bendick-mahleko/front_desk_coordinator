"""C0 — the role axis (spec r3 §1.1, §1.2, §1.3, §3.2, §4.13).

The load-bearing tests here are the immutability ones. Everything else in r3
rests on a single claim: that what a session *is* cannot be changed by anything
that happens inside it. If that claim fails, no tier filter downstream means
anything, because the role the filter is built from could have been moved.

No model, no network, no retrieval.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.channel import CHANNELS, ClinicalChannel, TextChannel, channel_for, is_patient_facing
from app.config import ClinicalConfig
from app.knowledge.chunking import (
    PATIENT_FACING_TIERS,
    STAFF_TICKET_TIERS,
    TIERS_BY_ROLE,
    Tier,
    narrow_to_role,
    tiers_for,
)
from app.store.session import (
    BOUND_AT_ESTABLISHMENT,
    WRITE_ONCE,
    Role,
    RoleImmutable,
    Session,
    SubjectStatus,
)
from app.tools.schemas import ClinicalRole

LATER = datetime.now(UTC) + timedelta(minutes=30)


def clinical() -> Session:
    return Session(role=Role.CLINICAL_ASSISTANT, channel="clinical")


def authenticated(expires: datetime = LATER) -> Session:
    session = clinical()
    session.bind_clinical_authentication("STAFF-2001", ClinicalRole.PHYSICIAN, expires)
    return session


# ------------------------------------------------------- the principal ---


def test_a_session_is_a_patient_session_unless_established_otherwise():
    """The default is the one that can read the least."""
    assert Session().role is Role.PATIENT


def test_the_role_cannot_be_changed_after_establishment():
    """spec §1.1 — *"never inferred from, or changed by, anything said inside
    the conversation"*. The single most important assertion in r3."""
    session = Session()

    with pytest.raises(RoleImmutable):
        session.role = Role.CLINICAL_ASSISTANT

    assert session.role is Role.PATIENT


@pytest.mark.parametrize("field", sorted(BOUND_AT_ESTABLISHMENT))
def test_nothing_bound_at_establishment_can_be_reassigned(field):
    session = Session()
    current = getattr(session, field)

    with pytest.raises(RoleImmutable):
        setattr(session, field, current)

    assert getattr(session, field) == current


def test_there_is_no_method_that_grants_the_clinical_role():
    """spec §3.2 — *"There is no mid-session elevation, and no function that
    grants it."* Asserted against the class surface, so a helpfully-named
    setter added later fails this rather than shipping."""
    granting = [
        name
        for name in dir(Session)
        if callable(getattr(Session, name, None))
        and not name.startswith("_")
        and any(word in name for word in ("elevate", "promote", "become", "set_role", "grant"))
    ]

    assert granting == []


# --------------------------------------------------------- write-once ---


def test_authentication_details_are_written_once(caplog):
    """spec §3.2 item 4 — recorded, then *"read-only for the session's
    lifetime"*."""
    session = authenticated()

    with pytest.raises(RoleImmutable):
        session.bind_clinical_authentication(
            "STAFF-9999", ClinicalRole.REGISTERED_NURSE, LATER + timedelta(hours=8)
        )

    assert session.staff_id == "STAFF-2001"


def test_expiry_cannot_be_extended_in_place():
    """Otherwise "sessions expire and require re-authentication" would be
    satisfiable by moving the deadline, which is not re-authentication."""
    session = authenticated()

    with pytest.raises(RoleImmutable):
        session.expires_at = LATER + timedelta(days=1)


@pytest.mark.parametrize("field", sorted(WRITE_ONCE))
def test_a_write_once_field_can_be_set_from_none(field):
    """The other direction: write-once must still permit the first write, or
    authentication itself could never record anything."""
    session = clinical()
    assert getattr(session, field) is None

    session.bind_clinical_authentication("STAFF-2001", ClinicalRole.PHYSICIAN, LATER)

    assert getattr(session, field) is not None


def test_authentication_cannot_be_bound_to_a_patient_session():
    with pytest.raises(RoleImmutable):
        Session().bind_clinical_authentication("STAFF-2001", ClinicalRole.PHYSICIAN, LATER)


# ------------------------------------------------------ effective role ---


def test_a_clinical_session_is_system_until_it_authenticates():
    """spec §4.13 — the functions are unavailable before authentication, and
    the session must not read as a patient in the meantime."""
    assert clinical().effective_role is Role.SYSTEM


def test_an_authenticated_clinical_session_is_clinical():
    assert authenticated().effective_role is Role.CLINICAL_ASSISTANT


def test_expiry_drops_to_system_and_never_to_patient():
    """spec §4.13 — *"drop to the system role. Do not fall back to the patient
    role"*. Falling back to patient would hand a clinician a stranger's
    scheduling workflows on a timer."""
    expired = authenticated(expires=datetime.now(UTC) - timedelta(seconds=1))

    assert expired.effective_role is Role.SYSTEM
    assert expired.effective_role is not Role.PATIENT
    assert not expired.clinical_authentication_valid


def test_the_established_role_survives_expiry():
    """§3.2 fixes the role for the session's lifetime; only the *capability*
    lapses. Both sentences hold because they are about different fields."""
    expired = authenticated(expires=datetime.now(UTC) - timedelta(seconds=1))

    assert expired.role is Role.CLINICAL_ASSISTANT
    assert expired.effective_role is Role.SYSTEM


def test_a_patient_session_is_never_downgraded_by_the_clock():
    session = Session()
    session.mark_identified("PT-4101")

    assert session.effective_role is Role.PATIENT


def test_validity_is_derived_not_stored():
    """A stored boolean would be one tick away from disagreeing with
    expires_at, and the disagreement would grant rather than deny."""
    assert "clinical_authentication_valid" not in Session.model_fields


# ------------------------------------------------------------ channels ---


def test_the_patient_channel_is_patient_facing():
    assert is_patient_facing(TextChannel.name)


def test_the_clinical_channel_is_not():
    assert not is_patient_facing(ClinicalChannel.name)


def test_a_clinical_session_cannot_be_established_on_a_patient_channel():
    """spec §3.2 — the structural half of the rule, so it holds on every
    construction path including rehydration from the store."""
    with pytest.raises(ValidationError, match="patient-facing"):
        Session(role=Role.CLINICAL_ASSISTANT, channel="text")


def test_a_patient_session_on_the_clinical_channel_is_allowed():
    """The rule is one-directional. Nothing about a staff-side channel makes a
    patient-role session unsafe, and forbidding it would block the §7.3
    workflow where a clinical session terminates and re-establishes."""
    assert Session(role=Role.PATIENT, channel="clinical").role is Role.PATIENT


def test_an_unknown_channel_name_is_an_error_not_a_default():
    """Defaulting would silently make an unrecognised name patient-facing."""
    with pytest.raises(ValueError, match="unknown channel"):
        channel_for("voice")


def test_a_channel_is_patient_facing_unless_it_says_otherwise():
    """The default must be the safe one, so adding a channel and forgetting the
    flag cannot open a clinical session to the public."""
    from app.channel import Capabilities

    assert Capabilities(spoken=False, overhearable=False).patient_facing


def test_every_registered_channel_renders_and_masks():
    for name, channel in CHANNELS.items():
        assert channel.render("call me on 206-555-0142") != "call me on 206-555-0142", name
        assert channel.mask_identifier("phone", "+12065550142")


# ------------------------------------------------------------- config ---


def test_the_role_is_off_unless_the_clinic_turns_it_on():
    assert not ClinicalConfig().enabled


def test_a_patient_facing_channel_cannot_be_made_eligible():
    """spec §3.2 — channel eligibility is configuration, but *this* is not a
    knob. A clinic cannot configure its way to a public clinical session."""
    with pytest.raises(ValidationError, match="patient-facing"):
        ClinicalConfig(enabled=True, channels=["text"], permitted_roles=[ClinicalRole.PHYSICIAN])


def test_an_unknown_channel_cannot_be_made_eligible():
    with pytest.raises(ValidationError, match="unknown channel"):
        ClinicalConfig(
            enabled=True, channels=["carrier_pigeon"], permitted_roles=[ClinicalRole.PHYSICIAN]
        )


def test_enabling_the_role_with_nothing_configured_is_refused():
    """Enabled with no channel would present the capability and refuse every
    call, which reads as a bug rather than as a policy."""
    with pytest.raises(ValidationError, match="no channels"):
        ClinicalConfig(enabled=True, permitted_roles=[ClinicalRole.PHYSICIAN])

    with pytest.raises(ValidationError, match="no roles"):
        ClinicalConfig(enabled=True, channels=["clinical"])


def test_a_disabled_clinic_allows_nothing():
    disabled = ClinicalConfig()

    assert not disabled.allows_channel("clinical")
    assert not disabled.allows_role(ClinicalRole.PHYSICIAN)


def test_the_shipped_clinic_config_enables_the_role_on_a_staff_channel(clinic):
    assert clinic.clinical.enabled
    assert clinic.clinical.allows_channel("clinical")
    assert not clinic.clinical.allows_channel("text")
    assert clinic.clinical.session_minutes == 30


# -------------------------------------------------------------- tiers ---


def test_a_patient_role_cannot_read_the_clinician_tier():
    """spec §1.2 — the one respect in which the roles differ. C0's exit."""
    assert Tier.CLINICIAN_ONLY not in TIERS_BY_ROLE[Role.PATIENT]


def test_a_patient_role_can_read_description_and_symptoms():
    """§1.2 gives a patient condition description and causes (patient_safe) and
    symptom-to-appointment routing (routing_only)."""
    assert TIERS_BY_ROLE[Role.PATIENT] == {Tier.PATIENT_SAFE, Tier.ROUTING_ONLY}


def test_the_clinical_role_can_read_every_tier():
    assert TIERS_BY_ROLE[Role.CLINICAL_ASSISTANT] == set(Tier)


def test_the_system_role_reads_nothing():
    """An expired clinical session reads as SYSTEM, and must not be able to
    retrieve what it could retrieve a minute earlier."""
    assert tiers_for(Role.SYSTEM) == frozenset()


def test_an_unmapped_role_reads_nothing():
    """Fails closed: adding a principal without deciding its tiers must return
    nothing rather than everything."""

    class Later(str):
        pass

    assert tiers_for(Later("auditor")) == frozenset()  # type: ignore[arg-type]


def test_every_role_has_an_entry():
    assert set(TIERS_BY_ROLE) == set(Role)


def test_narrowing_is_an_intersection_never_a_union():
    """spec §4.14 — a requested tier *"is never used to widen access"*."""
    assert narrow_to_role({Tier.CLINICIAN_ONLY}, Role.PATIENT) == frozenset()
    assert narrow_to_role(set(Tier), Role.PATIENT) == {Tier.PATIENT_SAFE, Tier.ROUTING_ONLY}
    assert narrow_to_role({Tier.CLINICIAN_ONLY}, Role.CLINICAL_ASSISTANT) == {Tier.CLINICIAN_ONLY}


def test_narrowing_an_expired_session_yields_nothing():
    """The composition that matters: effective_role, not role, drives the
    filter. Threaded through the retrieval tool in C3."""
    expired = authenticated(expires=datetime.now(UTC) - timedelta(seconds=1))

    assert narrow_to_role({Tier.CLINICIAN_ONLY}, expired.effective_role) == frozenset()


def test_returnable_text_is_narrower_than_queryable_tiers():
    """Two different questions, and conflating them is how symptom text would
    end up quoted back to a patient as a diagnosis."""
    assert TIERS_BY_ROLE[Role.PATIENT] > PATIENT_FACING_TIERS
    assert Tier.ROUTING_ONLY not in PATIENT_FACING_TIERS


def test_the_staff_ticket_exemption_is_named_and_narrow():
    """spec §4.12 requires clinician-only context on a complex_symptoms ticket,
    raised from a patient session. Named so the next reader sees a decision
    rather than a hole, and asserted narrow so it cannot grow."""
    assert {Tier.CLINICIAN_ONLY} == STAFF_TICKET_TIERS
    assert Tier.CLINICIAN_ONLY not in tiers_for(Role.PATIENT)


# ------------------------------------------------------- interoperation ---


def test_role_and_subject_status_are_independent(clinic):
    """spec §3.2 — *"The two are independent and neither substitutes for the
    other."* An authenticated clinician has established no patient."""
    session = authenticated()

    assert session.effective_role is Role.CLINICAL_ASSISTANT
    assert session.status is SubjectStatus.NONE
    assert session.patient_id is None


def test_a_clinical_session_still_has_to_verify_a_patient(clinic):
    """§3.2's last bullet — clinical authentication *"does not confer access to
    an arbitrary patient's record"*. The gate reads SubjectStatus, which
    authentication never touches."""
    from app.policy.gates import PolicyGate

    verdict = PolicyGate(clinic).evaluate(
        "get_patient_demographics", {"patient_id": "PT-4101"}, authenticated()
    )

    assert not verdict.allowed


# ----------------------------------------------------------- round trip ---


def test_a_clinical_session_survives_the_session_store(tmp_path):
    """Rehydration is a construction path, so it must be able to restore fields
    nothing else may write — and must not be able to alter them afterwards."""
    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    from app.store.models import SessionStore

    engine = create_engine(f"sqlite:///{tmp_path / 'roles.db'}")
    SQLModel.metadata.create_all(engine)
    store = SessionStore(engine=engine)
    original = authenticated()
    store.save(original)

    restored = store.load(original.session_id)

    assert restored is not None
    assert restored.role is Role.CLINICAL_ASSISTANT
    assert restored.staff_id == "STAFF-2001"
    assert restored.asserted_role is ClinicalRole.PHYSICIAN
    assert restored.channel == "clinical"
    with pytest.raises(RoleImmutable):
        restored.role = Role.PATIENT
