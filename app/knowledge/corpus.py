"""The disease corpus — loading, cleaning and validating the source data (R0).

Everything downstream depends on this being trustworthy, so defects are raised
rather than absorbed. A row that will not parse cleanly is dropped with a named
reason and counted, not indexed with a truncated field and forgotten.
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.knowledge.dosing import (
    Cohort,
    CohortDosing,
    DosageUnsplittable,
    split_cohorts,
)

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_SOURCE = DATA_DIR / "disease_list.csv"

# The source carries provenance markers from whatever generated it. They are
# noise to a reader and noise to an embedding.
CITATION = re.compile(r"\s*\[citation:\d+\]")

# "Children: Not applicable", "Not recommended for pharmacologic treatment" and
# friends are not doses. They must not be indexed as though they were, or a
# retrieval for a paediatric question returns a confident non-answer.
NOT_A_DOSE = re.compile(
    r"not applicable|not recommended|not usually needed|same as children",
    re.IGNORECASE,
)


class DiseaseRecord(BaseModel):
    """One row of the source, cleaned."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    causes: str
    symptoms: str
    treatment: str
    dosage: str
    source_row: int

    @property
    def cohorts(self) -> dict[Cohort, CohortDosing]:
        """The dosage field split by cohort, verbatim (spec §4.16)."""
        return split_cohorts(self.dosage)

    @property
    def has_paediatric_dosing(self) -> bool:
        """Weight- or BSA-scaled *paediatric* dosing — the highest-harm content
        in the file.

        This was ``"mg/kg" in self.dosage or "units/kg" in self.dosage``, which
        was wrong in both directions and measured so:

        * It flagged **Stroke**, whose paediatric entry reads "Not applicable",
          because its *adult* entry is ``tPA 0.9mg/kg IV``. Also Acne Vulgaris,
          for adult isotretinoin.
        * It missed four records whose paediatric entry is scaled in units the
          list did not enumerate — ``mcg/kg`` (Digoxin, a narrow-therapeutic-index
          cardiac glycoside), ``ml/kg`` (oral rehydration, twice), and
          Levothyroxine.

        Either error matters for §4.16: a false positive attaches a
        paediatric-dosing warning to a record with no paediatric dosing, and a
        false negative lets a scaled paediatric figure past the guard §8 asks to
        be demonstrated.
        """
        return self.cohorts[Cohort.PAEDIATRIC].requires_maximum


@dataclass
class LoadReport:
    """What happened during a load. Printed by the build CLI."""

    records: list[DiseaseRecord] = field(default_factory=list)
    rejected: list[tuple[int, str, str]] = field(default_factory=list)
    source_sha256: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.records)

    def render(self) -> str:
        lines = [
            f"{len(self.records)} record(s) loaded from a source hashing {self.source_sha256[:12]}"
        ]
        for row, name, reason in self.rejected:
            lines.append(f"  rejected row {row} ({name or 'unnamed'}): {reason}")
        return "\n".join(lines)


def clean(value: str | None) -> str:
    if not value:
        return ""
    return CITATION.sub("", value).strip()


REQUIRED_COLUMNS = (
    "name of disease",
    "brief description",
    "causes",
    "symptoms",
    "treatment",
    "dosage",
)


def load(path: Path | None = None) -> LoadReport:
    """Read the pipe-delimited source into validated records."""
    path = path or DEFAULT_SOURCE
    raw = path.read_text(encoding="utf-8")
    report = LoadReport(source_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest())

    reader = csv.DictReader(raw.splitlines(), delimiter="|")
    missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"{path} is missing column(s): {', '.join(missing)}")

    for number, row in enumerate(reader, start=2):  # row 1 is the header
        name = clean(row.get("name of disease"))
        fields = {key: clean(row.get(key)) for key in REQUIRED_COLUMNS}

        empty = [key for key, value in fields.items() if not value]
        if empty:
            report.rejected.append((number, name, f"empty field(s): {', '.join(empty)}"))
            continue

        # The final row of the supplied file is truncated mid-dosage
        # ("… 4 oz juice). Adults"). Indexing that would put a dangling "Adults"
        # into the clinician tier as if it were guidance.
        if _looks_truncated(fields["dosage"]):
            report.rejected.append((number, name, "dosage field is truncated"))
            continue

        # spec §4.16 needs the cohorts separated, and the separation is
        # substring arithmetic on the markers. A record that does not carry both
        # markers has no safe reading — the boundaries would be wrong, and wrong
        # boundaries mean one cohort's figure served as the other's. Rejected at
        # load so a changed source file fails the build rather than the answer.
        try:
            split_cohorts(fields["dosage"])
        except DosageUnsplittable as exc:
            report.rejected.append((number, name, f"dosage will not split by cohort: {exc}"))
            continue

        report.records.append(
            DiseaseRecord(
                name=name,
                description=fields["brief description"],
                causes=fields["causes"],
                symptoms=fields["symptoms"],
                treatment=fields["treatment"],
                dosage=fields["dosage"],
                source_row=number,
            )
        )

    return report


def _looks_truncated(dosage: str) -> bool:
    """A dosage that names an audience and then stops.

    "Adults" or "Children" as the last token means the dose that was supposed to
    follow is missing.
    """
    tail = dosage.rstrip(" .:;").split()[-1:] if dosage.strip() else []
    return bool(tail) and tail[0].lower().rstrip(":") in {"adults", "children", "adult", "child"}


def names(records: list[DiseaseRecord]) -> list[str]:
    """The indexed condition names, for the enum a patient-facing tool accepts."""
    return sorted(record.name for record in records)


@lru_cache(maxsize=1)
def records_by_name() -> dict[str, DiseaseRecord]:
    """The corpus keyed by record name, loaded once.

    §4.16 constrains ``condition_name`` to the indexed record names and wants the
    treatment and dosage fields back verbatim. The vector store holds those two
    fields combined into one chunk, so reconstructing them from a chunk would
    mean parsing text this module already has structured. Reading the corpus is
    the honest path — and the *authorization* to read the clinician tier is a
    separate question, answered by ``chunking.require_tiers`` at the call site.
    """
    return {record.name: record for record in load().records}


def canonical_name(name: str) -> str | None:
    """Resolve a condition name, case- and whitespace-insensitively.

    Exact on the name itself. §4.16: *"Return no result rather than a near match
    when the requested condition or medication is not in the indexed corpus."*
    Case and stray spacing are typing, not a different condition; anything else
    is a near match and returns None.
    """
    wanted = " ".join(name.split()).casefold()
    for candidate in records_by_name():
        if " ".join(candidate.split()).casefold() == wanted:
            return candidate
    return None
