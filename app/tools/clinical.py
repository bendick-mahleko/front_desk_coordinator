"""Clinical Assistant session establishment (spec r3 §4.13).

One function. It does not *grant* the clinical role — §3.2 forbids any function
that does, and the role was bound when the session was established on a
staff-side channel. It authenticates the person holding a session that is
already clinical, and records the outcome §4.13 requires.

The ordering of the refusals below is deliberate. Each one is checked before the
next, and each returns a distinct reason, because "you are not authenticated"
and "your account is shared" and "your credential expired" call for three
different actions from the person on the other end. The one thing never
disclosed is whether a staff id exists, which is why an unknown id and a wrong
token return the same thing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import get_clinic_config
from app.policy.decorator import current_audit, current_session
from app.store.session import Role
from app.tools.registry import backends, tool
from app.tools.schemas import ClinicalRole

SCOPE = (
    "search_clinical_knowledge",
    "summarize_diagnostic_considerations",
    "get_dosage_information",
)
"""What authentication makes available (spec §4.13–§4.16). Reported back so the
assistant can state the scope once at session start, as §4.13 requires."""

STANDING_LIMITS = (
    "Reference material compiled from a fixed set of indexed source documents, "
    "for clinician review. Not a diagnosis, a treatment plan, or a prescription. "
    "Not a current formulary or guideline service. Clinical judgement and "
    "responsibility rest with the treating clinician."
)


def _refuse(reason: str, message: str, remedy: str, **extra: Any) -> dict[str, Any]:
    return {"error": reason, "message": message, "remedy": remedy, **extra}


@tool("authenticate_clinical_user")
def authenticate_clinical_user(
    staff_id: str,
    credential_token: str,
    asserted_role: ClinicalRole,
    department: str | None = None,
) -> Any:
    """Authenticate a member of clinical staff for this session.

    Call this before any clinical-review function. Collect the staff identifier
    and the credential token the clinician's identity provider issued — never a
    password, and never anything you would repeat back to them.

    The role comes from the clinic's directory, not from what the person says.
    If somebody tells you they are a physician, that is not a role: pass what
    they told you as asserted_role so the mismatch is on the record, and let the
    directory decide.

    If this returns an error, say plainly what happened and offer the clinic's
    service desk. Do not try again with different arguments, do not ask the
    person to confirm their own role, and never continue as though the call had
    succeeded.

    On success, tell the clinician once which role was established and what it
    covers, and get on with their question.
    """
    session = current_session()
    clinic = get_clinic_config()
    audit = current_audit()

    def record(outcome: str, **detail: Any) -> None:
        # §4.13 — outcome, staff identifier, asserted role, timestamp, channel.
        # The timestamp is the audit record's own. No credential material: the
        # token is not in scope of this closure by construction.
        audit.note(
            "clinical_auth",
            {
                "staff_id": staff_id,
                "asserted_role": asserted_role.value,
                "channel": session.channel,
                "outcome": outcome,
                **detail,
            },
        )

    # --- is this even a clinical session? --------------------------------
    if session.role is not Role.CLINICAL_ASSISTANT:
        # Unreachable through the model, which is never shown this function in a
        # patient session (§2). Kept because "unreachable" is a claim about
        # today's wiring, and this is the one function whose failure mode is a
        # privilege escalation.
        record("wrong_session_role")
        return _refuse(
            "not_a_clinical_session",
            "This conversation is not a clinical session.",
            "Clinical access is established on the clinical channel at session "
            "start and cannot be requested here. Direct the person to the "
            "clinical surface.",
        )

    if not clinic.clinical.enabled:
        record("role_disabled")
        return _refuse(
            "clinical_role_disabled",
            "This clinic has not enabled clinical review.",
            "Nothing further can be done in this conversation. Direct the person "
            "to the clinic's service desk.",
        )

    if session.clinical_authentication_valid:
        # Not an error worth alarming anyone about, and not a refresh either:
        # §3.2 makes the authentication write-once, so re-authenticating into a
        # live session is what must not be possible.
        record("already_authenticated")
        return {
            "already_authenticated": True,
            "role": session.asserted_role.value if session.asserted_role else None,
            "expires_at": session.expires_at.isoformat() if session.expires_at else None,
            "next_step": (
                "This session is already authenticated. Do not state the role "
                "again — answer the clinician's question."
            ),
        }

    # --- what does the directory say? ------------------------------------
    assertion = backends().identity.authenticate(staff_id, credential_token)

    if assertion is None:
        # Unknown staff id and wrong token are one outcome on purpose. Telling
        # them apart would turn this function into a staff directory oracle.
        record("authentication_failed")
        return _refuse(
            "authentication_failed",
            "Those credentials were not accepted.",
            "Do not retry with a different role or a guessed identifier. Ask the "
            "clinician to obtain a fresh credential from the clinic's identity "
            "provider, or direct them to the service desk.",
        )

    if assertion.shared_account:
        # spec §3.2 last bullet. Checked before the role and before expiry
        # because a shared account is not fixable by re-issuing a credential —
        # it is the wrong kind of account.
        record("shared_account", directory_role=assertion.role.value if assertion.role else None)
        return _refuse(
            "shared_account_refused",
            "That is a shared account. Clinical review requires an individual one.",
            "Every clinical-review call has to be attributable to a named "
            "person. Ask the clinician to sign in with their own staff account.",
        )

    if assertion.credential_expired:
        record("credential_expired")
        return _refuse(
            "credential_expired",
            "That credential has expired.",
            "Ask the clinician to obtain a current credential from the identity "
            "provider and establish a new session. Nothing in this session can "
            "be unlocked by trying again.",
        )

    if assertion.role is None:
        # In the directory, and not in a clinical role. Authentication worked;
        # authorization did not.
        record("not_a_clinical_role")
        return _refuse(
            "not_a_clinical_role",
            "That account is not held in a licensed clinical role.",
            "Clinical review is limited to licensed clinical staff. Direct the "
            "person to the clinic's service desk if they believe this is wrong.",
        )

    if assertion.role is not asserted_role:
        # spec §3.2 item 3 — the directory's answer wins, and a disagreement is
        # worth a record of its own. Both directions are refused: this code has
        # no ordering over clinical roles and inventing one to decide which
        # mismatches are "upward" would be exactly the kind of guess the rest of
        # this system exists to avoid.
        record(
            "role_mismatch",
            directory_role=assertion.role.value,
            claimed_role=asserted_role.value,
        )
        return _refuse(
            "role_mismatch",
            "The role claimed does not match the clinic's directory.",
            "Do not adjust the claim and call again. The directory is "
            "authoritative; ask the clinician to raise the discrepancy with the "
            "service desk.",
        )

    if not clinic.clinical.allows_role(assertion.role):
        # A licensed role the directory holds, that this clinic has not admitted
        # to clinical review. Distinct from not_a_clinical_role: the person is a
        # clinician, and this clinic's configuration does not extend the
        # capability to them.
        record("role_not_permitted", directory_role=assertion.role.value)
        return _refuse(
            "role_not_permitted",
            "This clinic has not enabled clinical review for that role.",
            "This is a configuration decision, not something to work around. "
            "Direct the clinician to the service desk.",
        )

    # --- authenticated ----------------------------------------------------
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=clinic.clinical.session_minutes)
    session.bind_clinical_authentication(
        staff_id=assertion.staff_id,
        asserted_role=assertion.role,
        expires_at=expires_at,
        now=now,
    )
    record("authenticated", directory_role=assertion.role.value, expires_at=expires_at.isoformat())

    return {
        "authenticated": True,
        "role": assertion.role.value,
        "display_name": assertion.display_name,
        "department": assertion.department,
        "expires_at": expires_at.isoformat(),
        "session_minutes": clinic.clinical.session_minutes,
        "scope": list(SCOPE),
        "limits": STANDING_LIMITS,
        "next_step": (
            "State once, in your own words, which role was established and what "
            "it covers — the clinician needs to know which capabilities are live "
            f"and that the session lasts {clinic.clinical.session_minutes} minutes. "
            "Then answer their question. Do not repeat the scope on later turns."
        ),
    }
