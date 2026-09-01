"""Chunking, and the audience tier that makes the whole extension safe (R0).

One record becomes four chunks, and each carries a **tier** decided here — at
build time, by code, never by the model:

* ``patient_safe``    description, causes
* ``routing_only``    symptoms — used to compute a visit type and a red-flag
                      signal, never returned to a patient as text
* ``clinician_only``  treatment, dosage — reaches staff through an escalation
                      ticket and nowhere else

The tier is applied as a **metadata filter on the query**, so restricted vectors
are never candidates rather than being retrieved and then suppressed. That is
the same argument as the policy gate (AD-01): a decision made before the model
is consulted cannot be talked out of.

Chunking per *field* rather than per record is what makes this possible. A
whole-record chunk would carry a dose in the same vector as a description, and
no filter could separate them afterwards.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from app.knowledge.corpus import DEFAULT_SOURCE, NOT_A_DOSE, DiseaseRecord
from app.store.session import Role
from app.tools.schemas import Tier

__all__ = [
    "PATIENT_FACING_TIERS",
    "STAFF_TICKET_TIERS",
    "TIERS_BY_ROLE",
    "Chunk",
    "Tier",
    "TierNotPermitted",
    "TierViolation",
    "chunk_all",
    "chunk_record",
    "narrow_to_role",
    "require_tiers",
    "slug",
    "tiers_for",
]


PATIENT_FACING_TIERS = frozenset({Tier.PATIENT_SAFE})
"""The only tier whose text may be returned to a patient verbatim.

Narrower than what a patient *session* may query — see TIERS_BY_ROLE. Symptom
text is queryable in a patient session because a visit type is computed from it,
and is not returnable because it would read as a diagnosis.
"""

TIERS_BY_ROLE: dict[Role, frozenset[Tier]] = {
    # §1.1 — not a conversational participant. An expired clinical session reads
    # as SYSTEM (Session.effective_role), and it must not be able to read what it
    # could read a minute ago.
    Role.SYSTEM: frozenset(),
    # §1.2 — condition description and causes (patient_safe), plus
    # symptom-to-appointment routing (routing_only). No clinical content.
    Role.PATIENT: frozenset({Tier.PATIENT_SAFE, Tier.ROUTING_ONLY}),
    # §1.2 — the one respect in which the roles differ.
    Role.CLINICAL_ASSISTANT: frozenset({Tier.PATIENT_SAFE, Tier.ROUTING_ONLY, Tier.CLINICIAN_ONLY}),
}
"""Which tiers a session's *role* may query (spec §1.2).

§1.3 is the governing sentence: the tier filter is *"decided when the knowledge
base is built and enforced at query construction, using the role recorded on the
session"*, and no instruction inside a conversation may widen it. This table is
that role-to-tier mapping; the enforcement is in the retrieval tool (C3).
"""

STAFF_TICKET_TIERS = frozenset({Tier.CLINICIAN_ONLY})
"""The §4.12 exemption, named so it cannot be mistaken for an oversight.

§4.12 *requires* clinician-only reference context on a complex_symptoms
escalation ticket, and that ticket is raised from a patient session. So one path
reads CLINICIAN_ONLY under Role.PATIENT, and it is meant to.

It does not widen §7.3, which forbids clinician-only material in a patient-facing
*turn*, *text message*, *appointment record* or *patient-visible artifact*. A
staff ticket is none of those; it is the escalation to a human that §7.3's own
last bullet prescribes. The audit verifier (C6) asserts the boundary the tier
filter cannot: that this material reaches the ticket and never the reply.
"""


def tiers_for(role: Role) -> frozenset[Tier]:
    """Permitted tiers for a role. An unknown role reads nothing.

    Fails closed on purpose: adding a principal to Role without deciding its
    tiers should make retrieval return nothing, not everything.
    """
    return TIERS_BY_ROLE.get(role, frozenset())


def narrow_to_role(requested: frozenset[Tier] | set[Tier], role: Role) -> frozenset[Tier]:
    """Intersect a requested tier set with what the role permits.

    Intersection, never union — §4.14: a requested tier *"is validated against
    the role's permitted set and rejected if it exceeds it; it is never used to
    widen access"*. The rejection belongs to the caller, which needs to tell the
    difference between "nothing matched" and "you asked for something you cannot
    have"; this only computes the permitted set.
    """
    return frozenset(requested) & tiers_for(role)


class TierViolation(RuntimeError):
    """A search named no tier at all.

    Defined here rather than in ``store`` because ``require_tiers`` is now the
    place that raises it and ``store`` imports this module, not the other way
    round. ``app.knowledge.store`` re-exports it for existing callers.
    """


class TierNotPermitted(RuntimeError):
    """A session asked to read a tier its role does not permit (spec §4.14).

    Raised rather than quietly narrowed. §4.14 says a requested tier *"is
    validated against the role's permitted set and rejected if it exceeds it; it
    is never used to widen access"* — and a silent narrowing would hide a probe:
    the caller would get an empty result and no reviewer would ever see that
    somebody asked for the clinician tier from a patient session.
    """

    def __init__(self, requested: frozenset[Tier], permitted: frozenset[Tier], role: Role) -> None:
        self.requested = requested
        self.permitted = permitted
        self.role = role
        self.excess = requested - permitted
        super().__init__(
            f"role {role.value!r} may not read "
            f"{sorted(t.value for t in self.excess)}; permitted: "
            f"{sorted(t.value for t in permitted)}"
        )


def require_tiers(
    requested: Iterable[Tier], role: Role, *, staff_ticket: bool = False
) -> frozenset[Tier]:
    """Resolve the tier filter for one retrieval. **The chokepoint.**

    §1.3 is the governing sentence: the tier filter is *"decided when the
    knowledge base is built and enforced at query construction, using the role
    recorded on the session"*. Every retrieval in the system goes through here,
    so "enforced at query construction" is one function a reviewer can read
    rather than a habit spread across call sites.

    ``staff_ticket`` opens the §4.12 exemption and nothing else: a
    complex_symptoms escalation raised from a *patient* session must carry
    clinician-only reference context onto the ticket. It is a keyword-only
    argument with a default of False so it cannot be passed by accident, and it
    is the only way to reach ``CLINICIAN_ONLY`` from a patient role anywhere in
    the codebase.

    Raises rather than returning an empty set, because "you asked for something
    you cannot have" and "nothing matched" need different answers.
    """
    wanted = frozenset(requested)
    if not wanted:
        raise TierViolation("a search must name at least one tier")

    permitted = tiers_for(role)
    if staff_ticket:
        permitted = permitted | STAFF_TICKET_TIERS

    if wanted - permitted:
        raise TierNotPermitted(wanted, permitted, role)
    return wanted


class Chunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    disease: str
    field: str
    tier: Tier
    text: str
    source_row: int
    source_document: str = DEFAULT_SOURCE.name
    """The file this came from. §4.14 requires every returned chunk to name its
    source document, and §4.16 requires a citation for every dosage value — a
    citation to "the corpus" is not a citation."""

    def metadata(self) -> dict[str, str | int]:
        return {
            "disease": self.disease,
            "field": self.field,
            "tier": self.tier.value,
            "source_row": self.source_row,
            "source_document": self.source_document,
        }


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def chunk_record(record: DiseaseRecord) -> list[Chunk]:
    """Four tiered chunks from one record.

    ``treatment`` and ``dosage`` are combined into a single clinician chunk: a
    dose without the treatment it belongs to is less useful to a nurse and more
    dangerous out of context.
    """
    base = slug(record.name)
    chunks = [
        Chunk(
            chunk_id=f"{base}::description",
            disease=record.name,
            field="description",
            tier=Tier.PATIENT_SAFE,
            # The name is prepended so a query naming the condition matches its
            # own description rather than a neighbour's.
            text=f"{record.name}. {record.description}",
            source_row=record.source_row,
        ),
        Chunk(
            chunk_id=f"{base}::causes",
            disease=record.name,
            field="causes",
            tier=Tier.PATIENT_SAFE,
            text=f"{record.name}. Causes: {record.causes}",
            source_row=record.source_row,
        ),
        Chunk(
            chunk_id=f"{base}::symptoms",
            disease=record.name,
            field="symptoms",
            tier=Tier.ROUTING_ONLY,
            # No condition name here. This chunk is matched against what a
            # patient describes, and prepending the answer would make every
            # query retrieve on the label rather than the presentation.
            text=record.symptoms,
            source_row=record.source_row,
        ),
        Chunk(
            chunk_id=f"{base}::management",
            disease=record.name,
            field="management",
            tier=Tier.CLINICIAN_ONLY,
            text=f"{record.name}. Treatment: {record.treatment} Dosage: {record.dosage}",
            source_row=record.source_row,
        ),
    ]
    return chunks


def chunk_all(records: list[DiseaseRecord]) -> list[Chunk]:
    return [chunk for record in records for chunk in chunk_record(record)]


def usable_dosage(record: DiseaseRecord) -> bool:
    """Whether the dosage field says anything actionable at all.

    Over twenty records read "Children: Not applicable" or "Not recommended for
    pharmacologic treatment". Those are not doses, and a clinician briefing that
    presents them as though they were is worse than one that says nothing.
    """
    return not NOT_A_DOSE.fullmatch(record.dosage.strip())
