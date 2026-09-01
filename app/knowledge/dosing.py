"""Cohort splitting and dose classification (C4, spec §4.16).

The source keeps dosing as one free-text field per record, written as
``Children<qualifier>: … Adults: …``. §4.16 takes a cohort parameter and demands
the figures back *verbatim* — *"Reproduce doses, units, intervals, and maxima
verbatim; do not restate, round, convert, or normalize them."* So everything here
is substring arithmetic on the source text. Nothing is reformatted, no number is
parsed, and no unit is converted.

Three things measured rather than assumed, each of which changed the design.

**All 65 records split, but 12 carry an applicability qualifier in the marker
itself** — ``Children (6-12 years):``, ``Children (10+):``, ``Children (JIA):``,
``Children (Iron deficiency):``. That parenthetical is not decoration: it is the
condition under which the dose applies, and a dose for 6-to-12-year-olds handed
back for a three-year-old is wrong in the most consequential way this module
could be wrong. It is captured and returned, never swallowed by the split.

**Weight-based dosing is not only ``mg/kg``.** The corpus also uses ``mcg/kg``
(Digoxin, a narrow-therapeutic-index cardiac glycoside), ``ml/kg`` (oral
rehydration) and ``mg/m2`` (Methotrexate, body surface area rather than weight).
Detection is therefore on the *per-something* denominator, not on a list of
units.

**The cohort halves must be classified separately.** Four records have a
weight-based adult dose and a fixed or absent paediatric one — Stroke's
paediatric entry is "Not applicable" while its adult entry is
``tPA 0.9mg/kg IV``. Classifying the whole field would attach a paediatric
weight-dosing warning to a record with no paediatric dosing at all, and miss it
on four records that have one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from app.tools.schemas import Cohort

# `Children` optionally followed by a parenthetical qualifier, then a colon.
# The qualifier is captured because it restricts who the dose applies to.
CHILDREN_MARKER = re.compile(r"\bChildren\b([^:]*):\s*", re.IGNORECASE)
ADULTS_MARKER = re.compile(r"\bAdults\b\s*:\s*", re.IGNORECASE)

PER_KILOGRAM = re.compile(r"/\s*kg", re.IGNORECASE)
"""Any per-kilogram denominator. Unit-agnostic on purpose: mg/kg, mcg/kg and
ml/kg all appear in this corpus and all need the same treatment."""

PER_SQUARE_METRE = re.compile(r"/\s*m2", re.IGNORECASE)
"""Body surface area. Not weight, and exactly as unbounded without a maximum."""

NOT_APPLICABLE = re.compile(r"\bnot\s+applicable\b", re.IGNORECASE)

MAXIMUM = re.compile(
    r"""
    \(?                                  # often parenthesised in the source
    (?:
        max(?:imum)?\.?\s*(?:daily\s*)?(?:dose\s*)?[:\s]*[\d][^),.;]*
      | not\s+to\s+exceed\s+[\d][^),.;]*
      | up\s+to\s+a\s+maximum\s+of\s+[\d][^),.;]*
    )
    \)?
    """,
    re.IGNORECASE | re.VERBOSE,
)
"""A recorded ceiling, returned verbatim.

Measured: exactly **one** of the 65 records states a maximum at all — Fever
(Pyrexia), which states one for each cohort. The pattern is deliberately not
tuned tighter than that one example warrants; a false negative here produces a
warning, and a false positive would produce a fabricated ceiling.
"""


class Basis(StrEnum):
    """What the figure is scaled by."""

    WEIGHT_BASED = "weight_based"
    BODY_SURFACE_AREA = "body_surface_area"
    FIXED = "fixed"
    NOT_RECORDED = "not_recorded"
    """The source records no dosing for this cohort. §4.16: render as *"no
    dosing recorded in the source documents for this cohort"* — never as an
    absence of contraindication, and never by substituting the other cohort."""


class DosageUnsplittable(ValueError):
    """A record's dosage text does not carry both cohort markers.

    Raised at corpus load so a source file that changes shape fails the build,
    rather than at query time so a clinician gets half an answer.
    """


@dataclass(frozen=True)
class CohortDosing:
    """One cohort's dosing, as the source records it."""

    cohort: Cohort
    text: str
    """Verbatim. The substring between this cohort's marker and the next."""

    qualifier: str = ""
    """The marker's own parenthetical — "(6-12 years)", "(JIA)" — verbatim and
    without the brackets. Empty for the 53 records that have none."""

    @property
    def recorded(self) -> bool:
        return not NOT_APPLICABLE.search(self.text) and bool(self.text.strip())

    @property
    def basis(self) -> Basis:
        if not self.recorded:
            return Basis.NOT_RECORDED
        # Ordered, most specific first. Rheumatoid Arthritis' paediatric entry
        # carries both mg/m2 and mg/kg; either label triggers the same maximum
        # requirement, so which one is reported does not change the guard.
        if PER_KILOGRAM.search(self.text):
            return Basis.WEIGHT_BASED
        if PER_SQUARE_METRE.search(self.text):
            return Basis.BODY_SURFACE_AREA
        return Basis.FIXED

    @property
    def requires_maximum(self) -> bool:
        """§4.16 — a scaled figure is unbounded until something bounds it.

        Body surface area is included even though §4.16 says "weight-based":
        ``10-15mg/m2`` is exactly as open-ended as ``mg/kg``, and reading the
        clause narrowly enough to exclude it would honour the words while
        missing the point.
        """
        return self.basis in (Basis.WEIGHT_BASED, Basis.BODY_SURFACE_AREA)

    @property
    def maximum(self) -> str | None:
        """The recorded ceiling, verbatim, or None."""
        if not self.recorded:
            return None
        found = MAXIMUM.search(self.text)
        return found.group(0).strip("()") if found else None


def split_cohorts(dosage: str) -> dict[Cohort, CohortDosing]:
    """Split one record's dosage field into its two cohorts.

    Substring arithmetic only. The text between the markers is returned exactly
    as the source has it, so §4.16's "verbatim" is a property of the code rather
    than a promise about it.
    """
    children = CHILDREN_MARKER.search(dosage)
    adults = ADULTS_MARKER.search(dosage)

    if children is None or adults is None:
        missing = [
            name for name, found in (("Children", children), ("Adults", adults)) if found is None
        ]
        raise DosageUnsplittable(f"dosage text carries no {' or '.join(missing)} marker")
    if children.start() > adults.start():
        # Every record in the corpus puts children first. A record that does not
        # would make the substring boundaries wrong, and wrong boundaries here
        # mean one cohort's figure served as the other's.
        raise DosageUnsplittable("Adults marker precedes Children marker")

    return {
        Cohort.PAEDIATRIC: CohortDosing(
            cohort=Cohort.PAEDIATRIC,
            text=dosage[children.end() : adults.start()].strip(),
            qualifier=children.group(1).strip().strip("()"),
        ),
        Cohort.ADULT: CohortDosing(
            cohort=Cohort.ADULT,
            text=dosage[adults.end() :].strip(),
        ),
    }


def cohorts_requested(cohort: Cohort) -> tuple[Cohort, ...]:
    """Which result keys a request asks for."""
    if cohort is Cohort.BOTH:
        return (Cohort.ADULT, Cohort.PAEDIATRIC)
    return (cohort,)


def find_maximum(text: str) -> str | None:
    """Module-level form, for callers holding raw text."""
    found = MAXIMUM.search(text)
    return found.group(0).strip("()") if found else None


def dosing_basis(text: str) -> Basis:
    """Module-level form, for callers holding raw text."""
    return CohortDosing(cohort=Cohort.ADULT, text=text).basis
