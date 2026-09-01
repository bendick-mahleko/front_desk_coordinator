"""Appointment routing (R3).

Turns a described complaint into an *administrative* decision: which kind of
visit, in person or remote, and how soon. That is the front desk's actual job,
and it is what the specification's §4.5 already expects the assistant to get
right — "confirm the intended appointment type and modality if unclear".

**Nothing here names a condition to the patient.** Retrieval identifies which
records a description resembles; this module maps that to a visit type and then
discards the disease name. The patient hears "that sounds like something to be
seen in person, I'd suggest a sick visit this week", which is a receptionist
doing their job and contains no diagnosis.

The mapping is curated for the same reason the red-flag list is: which visit
type suits a presentation is a clinic policy question, not a similarity score.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.knowledge.red_flags import Severity, severity_for
from app.tools.schemas import AppointmentType, Modality

# Conditions needing hands-on examination or a procedure. Everything not listed
# is treatable remotely in principle, so the default is "either works".
NEEDS_IN_PERSON = frozenset(
    {
        "Acne Vulgaris",
        "Athlete's Foot",
        "Cataract",
        "Cholecystitis (Gallstones)",
        "Conjunctivitis (Pink Eye)",
        "Cystitis",
        "Deep Vein Thrombosis (DVT)",
        "Diverticulitis",
        "Earwax Blockage (Cerumen Impaction)",
        "Eczema",
        "Endometriosis",
        "Gingivitis",
        "Glaucoma",
        "Gout",
        "Hearing Loss",
        "Hemorrhoids",
        "Otitis Media (Middle Ear Infection)",
        "Pneumonia",
        "Psoriasis",
        "Shingles (Herpes Zoster)",
        "Sinusitis (Sinus Infection)",
        "Appendicitis",
        "Low Back Pain",
        "Osteoarthritis (OA)",
    }
)

# Long-term conditions a patient already carries: a review, not a new problem.
CHRONIC = frozenset(
    {
        "Alzheimer's Disease",
        "Anxiety Disorder",
        "Asthma",
        "Atrial Fibrillation",
        "Bipolar Disorder",
        "COPD",
        "Cystic Fibrosis",
        "Dementia",
        "Depression",
        "Endometriosis",
        "Epilepsy",
        "Fibromyalgia",
        "Glaucoma",
        "Heart Failure",
        "Hepatitis B",
        "Hepatitis C",
        "High Cholesterol",
        "Hypertension",
        "Hyperthyroidism",
        "Hypothyroidism",
        "Irritable Bowel Syndrome (IBS)",
        "Menopause",
        "Osteoarthritis (OA)",
        "Osteoporosis",
        "Psoriasis",
        "Rheumatoid Arthritis",
        "Schizophrenia",
        "Type 1 Diabetes",
        "Type 2 Diabetes",
    }
)


@dataclass(frozen=True)
class Routing:
    """An administrative recommendation. Carries no disease name by design."""

    appointment_type: AppointmentType
    modality: Modality
    within_days: int
    urgent: bool = False

    def as_payload(self) -> dict[str, str | int | bool]:
        return {
            "appointment_type": self.appointment_type.value,
            "modality": self.modality.value,
            "suggested_within_days": self.within_days,
            "urgent": self.urgent,
        }


DEFAULT = Routing(AppointmentType.SICK_VISIT, Modality.ANY, within_days=7)


def route(disease: str) -> Routing:
    """Map one matched record to a visit type. The name goes no further."""
    severity = severity_for(disease)
    modality = Modality.IN_PERSON if disease in NEEDS_IN_PERSON else Modality.ANY

    if severity is Severity.URGENT:
        # Urgent presentations are seen in person and soon, whatever the
        # condition would otherwise suggest.
        return Routing(AppointmentType.SICK_VISIT, Modality.IN_PERSON, 1, urgent=True)

    if disease in CHRONIC:
        return Routing(AppointmentType.FOLLOW_UP, modality, within_days=14)

    return Routing(AppointmentType.SICK_VISIT, modality, within_days=7)


def combine(diseases: list[str]) -> Routing:
    """Route on several candidate matches at once.

    Retrieval returns neighbours, and the top hit is not reliably the right one
    — so the *most cautious* routing among the candidates wins. Being early and
    in person costs a wasted appointment slot; being late does not.
    """
    if not diseases:
        return DEFAULT

    routings = [route(disease) for disease in diseases]
    urgent = any(r.urgent for r in routings)
    in_person = any(r.modality is Modality.IN_PERSON for r in routings)
    soonest = min(r.within_days for r in routings)

    # A sick visit outranks a follow-up: if any candidate looks acute, treat the
    # presentation as acute.
    acute = any(r.appointment_type is AppointmentType.SICK_VISIT for r in routings)

    return Routing(
        appointment_type=AppointmentType.SICK_VISIT if acute else AppointmentType.FOLLOW_UP,
        modality=Modality.IN_PERSON if in_person else Modality.ANY,
        within_days=soonest,
        urgent=urgent,
    )
