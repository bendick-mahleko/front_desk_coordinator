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
from app.knowledge.chunking import TierNotPermitted, require_tiers, slug
from app.knowledge.corpus import DEFAULT_SOURCE, canonical_name, records_by_name
from app.knowledge.dosing import CohortDosing, cohorts_requested, split_cohorts
from app.knowledge.red_flags import RED_FLAGS, severity_for
from app.knowledge.store import DEFAULT_MIN_SCORE
from app.policy.decorator import current_audit, current_session
from app.store.session import Role
from app.tools.registry import backends, knowledge_base, tool
from app.tools.schemas import ClinicalRole, Cohort, Tier

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


# --------------------------------------------------- knowledge retrieval ---


@tool("search_clinical_knowledge")
def search_clinical_knowledge(
    query: str,
    tier: Tier,
    k: int,
    min_score: float | None = None,
) -> Any:
    """Retrieve source material from the indexed clinical documents.

    Returns the source text itself, with a citation for every chunk. It does not
    summarise, interpret, or draw a conclusion — that is deliberate, and you
    must not present its output as though it had.

    Choose the tier by what you need: `patient_safe` for a condition
    description, `routing_only` for symptom text, `clinician_only` for treatment
    and dosage. Asking for a tier this session may not read is refused, not
    quietly narrowed, so ask for what you actually need.

    An empty result is a real answer and means no indexed document matched
    confidently. Say so and stop. Do not lower the threshold to find something,
    do not retry the same question in different words hoping for a hit, and do
    not fill the gap from your own knowledge — the corpus is a fixed set of
    documents and its silence is information.

    Every statement you build on this must cite the chunk it came from. Anything
    you cannot cite to a returned chunk does not go in your answer.
    """
    session = current_session()
    audit = current_audit()
    store = knowledge_base()
    floor = DEFAULT_MIN_SCORE if min_score is None else min_score

    def record(outcome: str, **detail: Any) -> None:
        # spec §4.14 — session and staff identifier, the query, the requested
        # and effective tiers, the chunk ids and their scores. The query goes in
        # unredacted: a reviewer cannot judge whether a retrieval was
        # appropriate against a mask.
        audit.note(
            "clinical_retrieval",
            {
                "tool": "search_clinical_knowledge",
                "staff_id": session.staff_id or "",
                "query": query,
                "requested_tier": tier.value,
                "k": k,
                "min_score": floor,
                "outcome": outcome,
                **detail,
            },
        )

    # §1.3 / §4.14 — the filter is built from the role on the session, before
    # the query is issued. effective_role rather than role: an expired session
    # reads as SYSTEM and SYSTEM reads nothing.
    try:
        effective = require_tiers([tier], session.effective_role)
    except TierNotPermitted as exc:
        record(
            "tier_refused",
            effective_tier=None,
            permitted=sorted(t.value for t in exc.permitted),
        )
        return _refuse(
            "tier_not_permitted",
            f"This session may not read the {tier.value} tier.",
            "Ask for a tier this session holds, or tell the clinician the "
            "material is outside what this session can retrieve. Do not "
            "describe what it would have contained.",
            permitted_tiers=sorted(t.value for t in exc.permitted),
        )

    hits = store.search(query, tiers=effective, k=k, min_score=floor)

    record(
        "no_match" if not hits else "ok",
        effective_tier=[t.value for t in sorted(effective, key=lambda t: t.value)],
        chunks=[{"chunk_id": h.chunk_id, "score": round(h.score, 3)} for h in hits],
    )

    if not hits:
        # spec §6 — *"Treat a below-threshold retrieval as a valid, negative
        # result, not an error."*
        return {
            "chunks": [],
            "match": "none",
            "min_score": floor,
            "next_step": (
                "No indexed document matched this query above the similarity "
                "floor. Tell the clinician there is no confident match in the "
                "source documents and stop. Do not answer from your own "
                "knowledge and do not retry with a lower threshold."
            ),
        }

    return {
        "chunks": [
            {
                "chunk_id": hit.chunk_id,
                "text": hit.text,
                "record": hit.disease,
                "field": hit.field,
                "tier": hit.tier.value,
                "source_document": hit.source_document,
                "source_row": hit.source_row,
                "citation": hit.citation,
                "score": round(hit.score, 3),
            }
            for hit in hits
        ],
        "match": "found",
        "next_step": (
            "This is source material, not an answer. Quote or paraphrase only "
            "what is here, cite each point to its chunk, and say plainly where "
            "the documents do not cover something rather than supplying it."
        ),
    }


# --------------------------------------------------- dosage reference (§4.16) ---

NO_DOSING_RECORDED = "no dosing recorded in the source documents for this cohort"
"""spec §4.16's exact wording for a cohort the source does not cover.

*"Never present it as an absence of contraindication, and never substitute the
other cohort's figure."* Both halves matter: this string says the documents are
silent, not that the drug is safe here.
"""

NO_MAXIMUM_RECORDED = "not recorded in the source documents"

VERIFICATION_NOTICE = (
    "This figure is scaled to the patient, so it is a reference range and not a "
    "dose. Verify it against the clinic's formulary for this patient before use."
)
"""spec §4.16 — the routine check on a scaled regimen.

§4.16 requires it for paediatric weight-based dosing. It is applied to every
scaled figure, adult included: alteplase at 0.9mg/kg needs a formulary check as
much as a child's paracetamol does, and narrowing the rule to the cohort the
clause happens to name would honour the words and miss the point.
"""

INCOMPLETE_SOURCE_NOTICE = (
    "The source documents record no maximum daily dose for this regimen, so the "
    "figure above is unbounded as recorded. Obtain the ceiling from the clinic's "
    "formulary before this is used. This is a gap in the source set, not a "
    "statement that no ceiling applies."
)
"""Decision 2 — the escalated notice, and a *separate field* from the routine one.

Measured on this corpus: 27 cohort entries carry a scaled figure and exactly one
records a maximum, so this fires 26 times out of 27. A notice that appears on
almost every response is furniture, and the routine formulary check appears on
all 27 — if they were one string, the reader would learn to skip the sentence
that actually varies. Two fields, differently worded, separately assertable, so a
UI can block on this one and footnote the other.
"""

STANDING_NOTICE = (
    "Reference summary compiled from indexed source documents for clinician "
    "review. Not a diagnosis, a treatment plan, or a prescription. The source set "
    "is fixed at index build time and is not a current formulary or guideline "
    "service. Clinical judgment and responsibility rest with the treating "
    "clinician."
)
"""Appendix A.4, verbatim. Appended to every response §4.16 produces."""


def _cohort_block(dosing: CohortDosing) -> dict[str, Any]:
    """One cohort's Appendix A.2 block, assembled here rather than by the model.

    The appendix's preamble is explicit that its structures are *"enforced by the
    function result rather than left to the model's discretion"*, so every field
    below is filled from the source text or from a fixed string — never composed.
    """
    who = "children" if dosing.cohort is Cohort.PAEDIATRIC else "adults"
    block: dict[str, Any] = {
        # The marker's qualifier is the condition under which the dose applies.
        # "Children (6-12 years)" handed back as "children" would widen a dose to
        # a three-year-old, which is the worst thing this function could do.
        "applies_to": f"{who} ({dosing.qualifier})" if dosing.qualifier else who,
        "recorded": dosing.recorded,
        "dosing": dosing.text if dosing.recorded else NO_DOSING_RECORDED,
        "dosing_basis": dosing.basis.value,
    }

    if not dosing.requires_maximum:
        # A fixed figure needs no ceiling to be interpretable, and an absent
        # cohort has no figure at all.
        return block

    block["maximum_daily_dose"] = dosing.maximum or NO_MAXIMUM_RECORDED
    block["verification_notice"] = VERIFICATION_NOTICE
    if dosing.maximum is None:
        block["incomplete_source_notice"] = INCOMPLETE_SOURCE_NOTICE
    return block


@tool("get_dosage_information")
def get_dosage_information(
    cohort: Cohort,
    condition_name: str | None = None,
    medication_name: str | None = None,
    include_treatment_context: bool = True,
) -> Any:
    """Look up treatment and dosage reference for a condition or a medication.

    Name exactly one of condition_name or medication_name. A condition name must
    match an indexed record; a near miss returns nothing rather than the closest
    record, because the closest record's dose is a different drug.

    Returns reference ranges as the source documents record them, word for word.
    Reproduce them the same way: do not round, do not convert units, do not
    restate a range as a single figure, and do not compute a dose for a patient's
    weight, age or renal function. That calculation is the clinician's, and this
    function has no patient in front of it.

    Where a figure is scaled per kilogram or per square metre and the source
    records no maximum, say so — the result carries the sentence to use. A scaled
    figure without a ceiling is not a dose anyone can act on, and presenting it as
    one would be worse than the gap it hides.

    Where a cohort reads that no dosing is recorded, that means the documents are
    silent. It does not mean the drug is safe for that cohort, and you must never
    offer the other cohort's figure in its place.

    Never format the result as a prescription or a medication order, and never
    send any of it in a text message.
    """
    session = current_session()
    audit = current_audit()

    def record_call(outcome: str, **detail: Any) -> None:
        # spec §4.16 — staff identifier, condition or medication requested,
        # cohort, chunk identifiers returned.
        audit.note(
            "clinical_retrieval",
            {
                "tool": "get_dosage_information",
                "staff_id": session.staff_id or "",
                "condition_name": condition_name,
                "medication_name": medication_name,
                "cohort": cohort.value,
                "outcome": outcome,
                **detail,
            },
        )

    # Authorization first, through the same chokepoint as every retrieval. This
    # function reads the corpus rather than the vector store — an exact-name
    # lookup has no similarity search to filter — so the tier check is explicit
    # here rather than implied by a query.
    try:
        require_tiers([Tier.CLINICIAN_ONLY], session.effective_role)
    except TierNotPermitted:
        record_call("tier_refused")
        return _refuse(
            "tier_not_permitted",
            "This session may not read treatment or dosage material.",
            "Tell the clinician this session cannot retrieve dosage reference, "
            "and do not describe what it would have contained.",
        )

    # --- resolve the subject ---------------------------------------------
    if condition_name is not None:
        name = canonical_name(condition_name)
        if name is None:
            record_call("not_in_corpus")
            return _refuse(
                "not_in_corpus",
                f"{condition_name!r} is not one of the indexed records.",
                "Do not answer from your own knowledge and do not offer the "
                "nearest record — a neighbouring condition's dose is a different "
                "drug. Say the condition is not in the source set. You may call "
                "search_clinical_knowledge to find out what is.",
            )
    else:
        # A medication is not a record name, so this one is a search. No hit
        # means no result (§4.16).
        if medication_name is None:  # pragma: no cover - the schema forbids it
            return _refuse(
                "invalid_arguments",
                "Name a condition or a medication.",
                "Call again with exactly one of condition_name or medication_name.",
            )
        tiers = require_tiers([Tier.CLINICIAN_ONLY], session.effective_role)
        hits = knowledge_base().search(medication_name, tiers=tiers, k=1)
        if not hits:
            record_call("not_in_corpus")
            return _refuse(
                "not_in_corpus",
                f"No indexed document records dosing for {medication_name!r}.",
                "Say there is no confident match in the source documents and "
                "stop. Do not answer from your own knowledge.",
            )
        name = hits[0].disease

    record = records_by_name()[name]
    cohorts = split_cohorts(record.dosage)
    wanted = cohorts_requested(cohort)

    blocks = {c.value: _cohort_block(cohorts[c]) for c in wanted}
    record_call(
        "ok",
        record=name,
        chunks=[f"{slug(name)}::management"],
        bases={c.value: cohorts[c].basis.value for c in wanted},
        maximum_recorded={c.value: cohorts[c].maximum is not None for c in wanted},
    )

    payload: dict[str, Any] = {
        "record": name,
        "source_document": DEFAULT_SOURCE.name,
        "source_row": record.source_row,
        "citation": f"{DEFAULT_SOURCE.name}, row {record.source_row}, {name}",
        "cohorts": blocks,
        "notice": STANDING_NOTICE,
    }
    if include_treatment_context:
        payload["treatment_context"] = record.treatment

    unbounded = [c for c in wanted if cohorts[c].requires_maximum and not cohorts[c].maximum]
    payload["next_step"] = (
        "Read the figures back exactly as they appear — same numbers, same units, "
        "same intervals — and cite the record. State the standing notice once. "
        "Do not calculate anything for a particular patient, and do not lay the "
        "answer out as a prescription."
        + (
            " One or more cohorts here have a scaled figure with no recorded "
            "maximum: say that plainly, in your own words, before the figure "
            "rather than after it."
            if unbounded
            else ""
        )
    )
    return payload


# ------------------------------ diagnostic consideration summaries (§4.15) ---
#
# Extractive. There is no model call in this function and no generated prose.
#
# Once Decision 1 removed A.1's two unsupported elements, every remaining element
# is a corpus field, a computed coverage note, or a fixed string. So §7.2's
# "uncited clinical content is a defect" holds *by construction* rather than by
# validating a generator's output afterwards — there is nothing here that could
# be uncited, because there is nothing here that was composed.
#
# The generative design (a second internal call with Appendix A.0's framing, then
# a validator resolving every claim to a retrieved chunk_id, failing closed to
# A.3) is recorded in docs/clinical-assistant-plan.md as the migration path for a
# corpus that grows the missing columns. It is more code, more cost and strictly
# more risk for no gain on this one.

A1_ELEMENTS: tuple[str, ...] = (
    "clinical features",
    "key differentiating factors",
    "confirmatory tests",
)
"""What Appendix A.1 asks of every consideration."""

CORPUS_SUPPLIES: tuple[str, ...] = ("clinical features",)
"""What this corpus can actually answer with — the ``symptoms`` field.

The coverage note is the *difference* between these two tuples rather than a
hardcoded sentence. A corpus that later grows a differentiators column starts
rendering that bullet and shrinks the note with no code change; a corpus that
silently loses a column starts disclosing it instead of emitting an empty bullet.
"""

NO_CONFIDENT_MATCH = (
    "No consideration in the indexed source documents matches this presentation "
    "with sufficient confidence. No summary is offered. The retrieval query and "
    "the scores considered are recorded in the audit log."
)
"""Appendix A.3, verbatim."""

CONSIDERATION_RELATIVE_FLOOR = 0.8
"""Keep a candidate only if it scored at least this fraction of the best match.

The absolute floor (``DEFAULT_MIN_SCORE``) answers "is this a match at all". It
cannot answer "is this a *comparable* consideration", and on this corpus the two
questions have different answers: measured on the hashing embedder, "productive
cough, fever and rigors" returns Pneumonia at 0.408 and Bronchitis at 0.340 — a
real differential — while "sudden weakness on one side of the face" returns
Stroke at 0.488 and *Acne Vulgaris* at 0.309. Both second hits clear 0.25; only
one of them belongs in a clinician's summary.

No absolute threshold separates those, because the noise in one query outscores
the signal in another. A relative floor can, and it is embedder-relative by
construction, which is the point: it adapts to whatever geometry the live
embedder has rather than encoding this one's.

Measured at 0.8 on five presentations: it drops every noise candidate in four of
them and keeps both legitimate differentials. It cannot help when the *top* hit
is wrong — see ``docs/gaps.md`` on the swollen-calf case.
"""

RULE_OUT_SOURCE = "clinic red-flag register"
"""Where rule-outs come from, and it is deliberately not "the source documents".

§4.15 asks for *"serious conditions the source documents indicate should be ruled
out"*. The corpus contains no such indication anywhere — no column, no
convention. What exists is ``app/knowledge/red_flags.py``: 14 conditions curated
by the clinic. Citing that as a source document would misstate provenance in a
clinician-facing artifact, which is a worse failure than the gap it papers over.
So rule-outs are attributed to the register by name, visibly distinct from the
source citations beside them.
"""


def _coverage_note() -> str:
    """What the source documents do not cover, stated once (Decision 1).

    §4.15 requires the absence to be stated. Decision 1 removed the per-item
    bullets — a field that is always empty is not a field, and boilerplate under
    every consideration teaches a clinician to skim the one line that is real —
    so it is said here, once, as a fact about the corpus.
    """
    missing = [element for element in A1_ELEMENTS if element not in CORPUS_SUPPLIES]
    if not missing:
        return "The source documents cover every element of this summary."
    return (
        "The source documents carry condition descriptions, causes, symptoms, "
        "treatment and dosage. They do not carry "
        + " or ".join(missing)
        + ", so no consideration below reports either. This is a limit of the "
        "indexed source set, not a finding about this presentation."
    )


@tool("summarize_diagnostic_considerations")
def summarize_diagnostic_considerations(
    presentation: str,
    max_considerations: int = 5,
    patient_id: str | None = None,
) -> Any:
    """Summarise what the indexed source documents say about a presentation.

    Give the clinician's own description of the presentation, verbatim. The
    result groups the source documents' symptom text by the records it resembles,
    each cited, ordered by how strongly the retrieval supported it.

    That ordering is not a ranking by likelihood, and you must not present it as
    one. The corpus does not encode likelihood; the order says which records the
    text resembled most, which is a different claim.

    Everything you say must be traceable to a returned citation. This corpus does
    not record differentiating factors or confirmatory tests, and the result says
    so — pass that on rather than filling the gap from your own knowledge, which
    §6 forbids and which a clinician cannot check.

    Rule-outs come from the clinic's own red-flag register, not from the source
    documents. Attribute them that way.

    Do not say how quickly anyone should be seen, do not assign urgency, and do
    not phrase any of this as a diagnosis, a recommendation or a plan. It is
    reference material for a clinician who retains the judgement.

    Where there is no confident match, say exactly that and stop.
    """
    session = current_session()
    audit = current_audit()
    store = knowledge_base()

    def record_call(outcome: str, **detail: Any) -> None:
        # spec §4.15 — staff identifier, presentation text, retrieved chunk
        # identifiers, considerations returned, and whether the no-match path was
        # taken.
        audit.note(
            "clinical_retrieval",
            {
                "tool": "summarize_diagnostic_considerations",
                "staff_id": session.staff_id or "",
                "presentation": presentation,
                # §4.15 — "recorded for audit linkage only; it does not cause
                # patient-record data to be retrieved or included". Nothing in
                # this function reads the patient backend.
                "patient_id": patient_id,
                "outcome": outcome,
                "no_match": outcome == "no_match",
                **detail,
            },
        )

    try:
        symptom_tiers = require_tiers([Tier.ROUTING_ONLY], session.effective_role)
        context_tiers = require_tiers([Tier.CLINICIAN_ONLY], session.effective_role)
    except TierNotPermitted:
        record_call("tier_refused")
        return _refuse(
            "tier_not_permitted",
            "This session may not read clinical review material.",
            "Tell the clinician this session cannot produce a review summary, and "
            "do not describe what it would have contained.",
        )

    # Two steps, not one blended query. r2 measured why: matching a symptom
    # description against *treatment* text retrieved "Common Cold" ahead of
    # Pneumonia, because the vocabularies do not overlap. So candidates come from
    # symptom-to-symptom similarity, and each candidate's clinician-tier context
    # is then fetched by id — a lookup, not a second guess.
    found = store.search(presentation, tiers=symptom_tiers, k=max(max_considerations, 1) + 2)

    # Two floors, answering two different questions. The store's absolute floor
    # has already asked "is this a match at all"; this asks "is it a comparable
    # consideration", which no absolute number can answer on this corpus.
    candidates = (
        [hit for hit in found if hit.score >= found[0].score * CONSIDERATION_RELATIVE_FLOOR]
        if found
        else []
    )

    if not candidates:
        # spec §4.15 — "Where retrieval returns no confident match, return the
        # no-match response of Appendix A.3. Do not produce a summary from a weak
        # match." §7.2: abstain rather than approximate.
        record_call("no_match", chunks=[], considered=[h.chunk_id for h in found])
        return {
            "presentation": presentation,
            "match": "none",
            "summary": NO_CONFIDENT_MATCH,
            "notice": STANDING_NOTICE,
            "next_step": (
                "Tell the clinician there is no confident match in the source "
                "documents and stop. Do not offer a summary from a weak match, do "
                "not lower the threshold, and do not answer from your own "
                "knowledge."
            ),
        }

    # Rule-outs are computed over *every* candidate, before truncation, so a
    # condition the clinic marks serious cannot be dropped by max_considerations.
    flagged = [hit for hit in candidates if severity_for(hit.disease) is not None]
    considerations = candidates[:max_considerations]

    context: list[dict[str, Any]] = []
    rendered: list[dict[str, Any]] = []
    for position, hit in enumerate(considerations, start=1):
        context.append(
            {
                "chunk_id": hit.chunk_id,
                "tier": hit.tier.value,
                "citation": hit.citation,
                "text": hit.text,
            }
        )
        # Field-driven, not template-driven (Decision 1): one entry per element
        # the record actually supplies. There is no differentiators key and no
        # confirmatory-tests key, rather than keys holding a placeholder.
        entry: dict[str, Any] = {
            "position": position,
            "consideration": hit.disease,
            "citation": hit.citation,
            "support": round(hit.score, 3),
            "clinical features": hit.text,
        }
        management = store.get(f"{slug(hit.disease)}::management", tiers=context_tiers)
        if management is not None:
            context.append(
                {
                    "chunk_id": management.chunk_id,
                    "tier": management.tier.value,
                    "citation": management.citation,
                    "text": management.text,
                }
            )
            entry["recorded management"] = management.text
            entry["management citation"] = management.citation
        rendered.append(entry)

    record_call(
        "ok",
        chunks=[c["chunk_id"] for c in context],
        considerations=[e["consideration"] for e in rendered],
        rule_outs=[hit.disease for hit in flagged],
    )

    return {
        "presentation": presentation,
        "match": "found",
        "context": context,
        "summary": rendered,
        "ordering": (
            "By strength of support in the retrieved context. This is not a "
            "ranking by clinical likelihood, which the source documents do not "
            "encode."
        ),
        "rule_outs": {
            "source": RULE_OUT_SOURCE,
            "conditions": [hit.disease for hit in flagged],
            "note": (
                "Drawn from the clinic's red-flag register, not from the source "
                "documents, which carry no rule-out guidance. The register covers "
                f"{len(RED_FLAGS)} conditions and this list is limited to those "
                "the retrieval surfaced, so it is not a complete differential. No "
                "urgency is assigned: that is a clinical judgement."
            ),
        },
        "coverage_note": _coverage_note(),
        "notice": STANDING_NOTICE,
        "next_step": (
            "Present this as a summary of source documents for review, not as a "
            "diagnosis, a recommendation or a plan. Cite each point to the "
            "citation beside it. Say that the ordering reflects strength of "
            "support in the documents rather than likelihood. Pass on the coverage "
            "note rather than filling the gap. Attribute the rule-outs to the "
            "clinic's register. Do not say how soon the patient should be seen."
        ),
    }
