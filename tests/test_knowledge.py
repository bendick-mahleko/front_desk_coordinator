"""The knowledge extension (R0–R4).

The load-bearing tests are the tier-leak ones. Everything else is retrieval
quality, which is measured; the tier boundary is a safety property, which is
asserted.

Deterministic throughout: the hashing embedder means the same corpus always
produces the same index, so a retrieval assertion is a real assertion.
"""

from __future__ import annotations

import re

import pytest

from app.knowledge.chunking import PATIENT_FACING_TIERS, Tier, chunk_all, chunk_record, slug
from app.knowledge.corpus import DEFAULT_SOURCE, load
from app.knowledge.embedding import HashingEmbedder, build_embedder, cosine
from app.knowledge.red_flags import RED_FLAGS, Severity, is_emergency
from app.knowledge.routing import CHRONIC, NEEDS_IN_PERSON, combine, route
from app.knowledge.store import InMemoryKnowledgeBase, TierViolation
from app.tools.schemas import AppointmentType, Modality

# Anything that looks like a dose. The single most important pattern in the file.
DOSE = re.compile(r"\d+\s*(?:mg|mcg|ml|g|units|IU)\b|mg/kg|units/kg", re.IGNORECASE)


@pytest.fixture(scope="module")
def records():
    return load().records


@pytest.fixture(scope="module")
def chunks(records):
    return chunk_all(records)


@pytest.fixture(scope="module")
def kb(chunks):
    store = InMemoryKnowledgeBase(HashingEmbedder())
    store.index(chunks)
    return store


# ------------------------------------------------------------- the corpus ---


def test_the_source_is_vendored_in_the_repo():
    """The build must be reproducible without reaching outside the project."""
    assert DEFAULT_SOURCE.exists()


def test_records_load_and_the_truncated_row_is_rejected():
    report = load()

    assert len(report.records) == 65
    assert len(report.rejected) == 1
    row, name, reason = report.rejected[0]
    assert name == "Hypoglycemia"
    assert "truncated" in reason


def test_citation_artifacts_are_stripped(records):
    for record in records:
        for field in (
            record.description,
            record.causes,
            record.symptoms,
            record.treatment,
            record.dosage,
        ):
            assert "[citation:" not in field


def test_the_mojibake_is_gone(records):
    fever = next(r for r in records if r.name.startswith("Fever"))
    assert "37°C" in fever.symptoms
    assert "Â" not in fever.symptoms


def test_quoted_fields_parse(records):
    """The Fibromyalgia row uses CSV quoting with escaped inner quotes."""
    fibro = next(r for r in records if r.name == "Fibromyalgia")
    assert '"fibro fog"' in fibro.symptoms


def test_paediatric_dosing_is_identifiable(records):
    """21 records carry weight-based paediatric dosing — the highest-harm content."""
    assert sum(1 for r in records if r.has_paediatric_dosing) == 21


# -------------------------------------------------------------- chunking ---


def test_each_record_becomes_four_tiered_chunks(records):
    chunks = chunk_record(records[0])
    assert len(chunks) == 4
    assert {c.tier for c in chunks} == {Tier.PATIENT_SAFE, Tier.ROUTING_ONLY, Tier.CLINICIAN_ONLY}


def test_chunk_ids_are_stable(records):
    """Rebuilding upserts rather than duplicating."""
    first = [c.chunk_id for c in chunk_record(records[0])]
    second = [c.chunk_id for c in chunk_record(records[0])]
    assert first == second
    assert all(c.startswith(slug(records[0].name)) for c in first)


def test_the_corpus_tiers_as_expected(chunks):
    counts = {tier: sum(1 for c in chunks if c.tier is tier) for tier in Tier}
    assert counts[Tier.PATIENT_SAFE] == 130
    assert counts[Tier.ROUTING_ONLY] == 65
    assert counts[Tier.CLINICIAN_ONLY] == 65


def test_no_dose_appears_outside_the_clinician_tier(chunks):
    """The load-bearing assertion of the whole extension.

    A dose in a patient_safe or routing_only chunk would be retrievable by a
    patient-facing tool no matter how the query was filtered.
    """
    for chunk in chunks:
        if chunk.tier is Tier.CLINICIAN_ONLY:
            continue
        assert not DOSE.search(chunk.text), f"{chunk.chunk_id} carries a dose: {chunk.text[:80]}"


def test_the_symptoms_chunk_does_not_name_its_condition(chunks):
    """It is matched against what a patient describes. Prepending the answer
    would make every query retrieve on the label rather than the presentation."""
    symptom_chunks = [c for c in chunks if c.tier is Tier.ROUTING_ONLY]
    named = [c for c in symptom_chunks if c.text.startswith(c.disease)]
    assert not named


# ------------------------------------------------------------ embedding ---


def test_the_hashing_embedder_is_deterministic():
    a = HashingEmbedder().embed(["itchy rash between the toes"])[0]
    b = HashingEmbedder().embed(["itchy rash between the toes"])[0]
    assert a == b


def test_the_hashing_embedder_produces_meaningful_distances():
    """Not a stub returning zeros: related text must score above unrelated."""
    e = HashingEmbedder()
    query, near, far = e.embed(
        [
            "itchy scaly rash between the toes",
            "itchy rash on the feet, scaly skin",
            "irregular heart rhythm and palpitations",
        ]
    )
    assert cosine(query, near) > cosine(query, far)


def test_vectors_are_normalised():
    vector = HashingEmbedder().embed(["fever and chills"])[0]
    assert abs(sum(v * v for v in vector) - 1.0) < 1e-9


def test_the_embedder_falls_back_when_offline(monkeypatch):
    from app.config import Settings

    embedder = build_embedder(Settings(openrouter_api_key=None, embedding_provider="openrouter"))
    assert isinstance(embedder, HashingEmbedder)


# ------------------------------------------------- retrieval and its tiers ---


def test_semantic_retrieval_finds_the_right_record(kb):
    hits = kb.search("itchy scaly rash between my toes", tiers=[Tier.ROUTING_ONLY], k=3)
    assert hits
    assert hits[0].disease == "Athlete's Foot"


def test_a_patient_facing_search_can_never_reach_a_dose(kb):
    """The safety property, asserted on returned metadata rather than on text."""
    for query in [
        "what dose of paracetamol for a 4 year old",
        "how much metformin should I take",
        "treatment for asthma",
        "prescribe me something for my rash",
    ]:
        hits = kb.search(query, tiers=PATIENT_FACING_TIERS, k=5, min_score=0.0)
        assert all(h.tier is Tier.PATIENT_SAFE for h in hits)
        assert all(h.field in {"description", "causes"} for h in hits)
        assert not any(DOSE.search(h.text) for h in hits)


def test_the_clinician_tier_does_hold_the_doses(kb):
    """The content exists; only its reachability is restricted."""
    hits = kb.search("paracetamol dose for fever", tiers=[Tier.CLINICIAN_ONLY], k=1)
    assert hits and DOSE.search(hits[0].text)


def test_a_search_must_name_a_tier(kb):
    with pytest.raises(TierViolation):
        kb.search("anything", tiers=[], k=3)


def test_a_weak_match_returns_nothing(kb):
    """66 records means everything has a nearest neighbour. Returning the
    least-bad row would be worse than admitting there is no match."""
    assert (
        kb.search("purple spotted zebra syndrome", tiers=[Tier.ROUTING_ONLY], k=3, min_score=0.45)
        == []
    )


# ------------------------------------------------------------- red flags ---


def test_red_flag_conditions_are_in_the_corpus(records):
    """A flag on a condition that is not indexed can never fire."""
    names = {r.name for r in records}
    for flagged in RED_FLAGS:
        assert flagged in names, f"{flagged} is flagged but not in the corpus"


def test_self_harm_language_is_flagged_as_an_emergency(records):
    """Depression's symptom text includes thoughts of suicide."""
    assert is_emergency("Depression")
    depression = next(r for r in records if r.name == "Depression")
    assert "suicide" in depression.symptoms.lower()


def test_time_critical_conditions_are_emergencies():
    for name in ["Stroke", "Appendicitis", "Deep Vein Thrombosis (DVT)"]:
        assert RED_FLAGS[name] is Severity.EMERGENCY


def test_an_unflagged_condition_is_not_an_emergency():
    assert not is_emergency("Athlete's Foot")
    assert not is_emergency("Gingivitis")


# --------------------------------------------------------------- routing ---


def test_an_acute_complaint_routes_to_a_sick_visit():
    assert route("Athlete's Foot").appointment_type is AppointmentType.SICK_VISIT


def test_a_chronic_condition_routes_to_a_follow_up():
    routing = route("Hypertension")
    assert routing.appointment_type is AppointmentType.FOLLOW_UP
    assert routing.within_days == 14


def test_conditions_needing_examination_route_in_person():
    assert route("Conjunctivitis (Pink Eye)").modality is Modality.IN_PERSON


def test_an_urgent_condition_overrides_everything():
    routing = route("Pneumonia")
    assert routing.urgent
    assert routing.within_days == 1
    assert routing.modality is Modality.IN_PERSON


def test_combining_candidates_takes_the_most_cautious():
    """Retrieval returns neighbours and the top hit is not reliably right.
    Being early and in person costs a slot; being late does not."""
    routing = combine(["Gingivitis", "Pneumonia"])
    assert routing.urgent
    assert routing.within_days == 1


def test_routing_carries_no_disease_name():
    """The structural guarantee: there is no field that could hold one."""
    payload = combine(["Athlete's Foot"]).as_payload()
    assert "Athlete" not in str(payload)
    assert set(payload) == {"appointment_type", "modality", "suggested_within_days", "urgent"}


def test_the_routing_tables_only_name_real_conditions(records):
    names = {r.name for r in records}
    assert names >= NEEDS_IN_PERSON
    assert names >= CHRONIC
