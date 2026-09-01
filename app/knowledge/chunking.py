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
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.knowledge.corpus import NOT_A_DOSE, DiseaseRecord


class Tier(StrEnum):
    PATIENT_SAFE = "patient_safe"
    ROUTING_ONLY = "routing_only"
    CLINICIAN_ONLY = "clinician_only"


PATIENT_FACING_TIERS = frozenset({Tier.PATIENT_SAFE})
"""The only tier a patient-facing tool may ever query."""


class Chunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    disease: str
    field: str
    tier: Tier
    text: str
    source_row: int

    def metadata(self) -> dict[str, str | int]:
        return {
            "disease": self.disease,
            "field": self.field,
            "tier": self.tier.value,
            "source_row": self.source_row,
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
