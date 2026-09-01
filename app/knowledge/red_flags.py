"""Red-flag conditions (R2).

A curated severity table over the corpus. Retrieval decides *which* records a
description resembles; this decides what to do about it — and the answer is
always a routing decision, never content shown to the patient.

Curated rather than inferred, because "is this presentation dangerous" is a
clinical judgement and must not be left to cosine similarity. The list is short
enough to review in one sitting, which is the point.
"""

from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    EMERGENCY = "emergency"
    """Contact emergency services now. Short-circuits the turn."""

    URGENT = "urgent"
    """Needs a clinician today. Escalates, and never books a routine slot."""


RED_FLAGS: dict[str, Severity] = {
    # Time-critical: outcome depends on hours.
    "Stroke": Severity.EMERGENCY,
    "Appendicitis": Severity.EMERGENCY,
    "Deep Vein Thrombosis (DVT)": Severity.EMERGENCY,
    # Symptom text includes "thoughts of death or suicide".
    "Depression": Severity.EMERGENCY,
    # Serious and can deteriorate quickly, but presentation varies.
    "Atrial Fibrillation": Severity.URGENT,
    "Heart Failure": Severity.URGENT,
    "Pneumonia": Severity.URGENT,
    "Cholecystitis (Gallstones)": Severity.URGENT,
    "Diverticulitis": Severity.URGENT,
    "Epilepsy": Severity.URGENT,
    "Type 1 Diabetes": Severity.URGENT,
    "Hepatitis B": Severity.URGENT,
    "Schizophrenia": Severity.URGENT,
    "Bipolar Disorder": Severity.URGENT,
}

RED_FLAG_MIN_SCORE = 0.30
"""Retrieval floor for a red flag to fire. Measured, not guessed.

Against the built index, ordinary front-desk traffic ("book a follow-up", "text
me directions", "itchy rash between my toes") tops out at 0.14 similarity to any
red-flag record, while emergencies described in plain words score 0.38-0.63.
0.30 sits in that gap: it caught four of five probe emergencies with no false
alarms in eight ordinary requests, where 0.42 caught only three.

The fifth is the one worth knowing about. Veiled self-harm language — "I don't
see the point in being here any more" — scores 0.19 against Depression, too
close to the false-positive ceiling to act on. **Retrieval is the wrong
instrument for that**, and lowering the floor to reach it would fire on ordinary
traffic. It is caught by the classifier layer instead, which reads intent rather
than vocabulary, and that is verified in the tests.

Each layer covers what the others miss: keywords for explicit language,
retrieval for paraphrased physical emergencies, the classifier for veiled
psychological ones.
"""

BRIEFING_MIN_SCORE = 0.30
"""Floor for the clinician briefing (R4).

Lower than a patient-facing floor would be, because the harm profile is
different: a slightly-off reference reaching a nurse costs them a moment's
reading, and they are qualified to discard it. The same margin of error in front
of a patient is not recoverable.
"""


def severity_for(disease: str) -> Severity | None:
    return RED_FLAGS.get(disease)


def is_emergency(disease: str) -> bool:
    return RED_FLAGS.get(disease) is Severity.EMERGENCY
