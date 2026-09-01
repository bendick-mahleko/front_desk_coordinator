"""What counts as clinician-only material, in one place (spec r3 §7.3).

§7.3: *"Clinician-only material must never appear in a patient-facing turn, in a
secure text message, in an appointment record, or in any patient-visible
artifact. This is enforced at retrieval, asserted in the audit verifier, and
tested adversarially."*

Three mechanisms, and this module serves the middle one. Retrieval enforcement is
``app.knowledge.chunking.require_tiers``; the adversarial tests are C8. The
verifier needs a definition of the thing it is looking for, and that definition
used to exist as a ``DOSE`` regex copied into three test files — which is one
definition too few for something three layers depend on.

The asymmetry this exists to express: **a dose in a clinical session's log is the
feature working, and the same dose in a patient session's log is a leak.** Only
the session's role tells them apart, which is why the audit record carries one.
"""

from __future__ import annotations

import re

DOSE = re.compile(
    r"""
      \d+\s*(?:mg|mcg|ug|g|ml|units|IU)\b   # an absolute figure with a unit
    | \d*\s*(?:mg|mcg|ug|g|ml|units|IU)\s*/\s*(?:kg|m2)   # a scaled figure
    """,
    re.IGNORECASE | re.VERBOSE,
)
"""Anything that reads as a dose.

Deliberately broad. A false positive here is a verifier complaint a developer
resolves in a minute; a false negative is a dose in a patient-facing artifact
that nobody notices. The scan is only ever applied to records that should carry
no clinical content at all, so there is nothing legitimate for it to trip on.
"""

CLINICIAN_MARKERS: tuple[str, ...] = (
    "clinician_only",
    "::management",
)
"""Structural traces of the clinician tier, rather than its content.

A chunk id ending ``::management`` names the tier it came from even when the text
beside it is innocuous, and a record naming ``clinician_only`` as an effective
tier has read that tier. Both are evidence the boundary was crossed, and both are
cheaper to spot than the content itself.
"""


def clinical_content(text: str) -> str | None:
    """Why this text looks like clinician-only material, or None.

    Returns a reason rather than a bool so a verifier problem can say what it
    found without quoting the material it found — which would put the leak in
    the report about the leak.
    """
    if DOSE.search(text):
        return "a dose figure"
    lowered = text.lower()
    for marker in CLINICIAN_MARKERS:
        if marker in lowered:
            return f"the clinician-tier marker {marker!r}"
    return None
