"""C4 — get_dosage_information (spec §4.16, Appendix A.2, Decision 2).

The highest-risk function in the release, so the tests are arranged around what
it must never do rather than what it does.

Two are exhaustive over the whole corpus rather than sampled, because §8 asks for
a *demonstration* and a demonstration on three records is an anecdote:

* no payload ever carries a scaled figure without a basis, a maximum field, and
  — where that maximum is absent — the escalated notice; and
* no cohort is ever served the other cohort's figure.

No model, no network.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.knowledge.chunking import chunk_all
from app.knowledge.corpus import canonical_name, load, records_by_name
from app.knowledge.dosing import (
    Basis,
    Cohort,
    CohortDosing,
    DosageUnsplittable,
    dosing_basis,
    find_maximum,
    split_cohorts,
)
from app.knowledge.embedding import HashingEmbedder
from app.knowledge.store import InMemoryKnowledgeBase
from app.policy.decorator import session_scope
from app.policy.gates import PolicyGate
from app.store.session import Role, Session
from app.tools import registry
from app.tools.clinical import (
    INCOMPLETE_SOURCE_NOTICE,
    NO_DOSING_RECORDED,
    NO_MAXIMUM_RECORDED,
    STANDING_NOTICE,
    VERIFICATION_NOTICE,
)
from app.tools.schemas import ClinicalRole

SCALED = re.compile(r"/\s*(?:kg|m2)", re.IGNORECASE)
"""Any figure scaled to the patient. Unit-agnostic: the corpus uses mg/kg,
mcg/kg, ml/kg and mg/m2, and all four are unbounded without a ceiling."""


@pytest.fixture(scope="module")
def records():
    return load().records


@pytest.fixture(scope="module")
def kb():
    store = InMemoryKnowledgeBase(HashingEmbedder())
    store.index(chunk_all(load().records))
    return store


class Recorder:
    def __init__(self) -> None:
        self.notes: list[tuple[str, dict[str, Any]]] = []

    def gate_decision(self, function, verdict, session) -> None:  # noqa: ANN001
        return None

    def tool_result(self, function, result, session) -> None:  # noqa: ANN001
        return None

    def note(self, kind: str, detail: dict[str, Any]) -> None:
        self.notes.append((kind, dict(detail)))

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [d for k, d in self.notes if k == kind]


@pytest.fixture
def audit() -> Recorder:
    return Recorder()


def clinical(*, expired: bool = False) -> Session:
    session = Session(role=Role.CLINICAL_ASSISTANT, channel="clinical")
    when = (
        datetime.now(UTC) - timedelta(seconds=1)
        if expired
        else datetime.now(UTC) + timedelta(minutes=30)
    )
    session.bind_clinical_authentication("STAFF-2001", ClinicalRole.PHYSICIAN, when)
    return session


@pytest.fixture
def call(sim, clinic, kb, audit):
    def _call(session: Session, **kwargs: Any) -> Any:
        with (
            session_scope(session, gate=PolicyGate(clinic), audit=audit),
            registry.backend_scope(sim),
            registry.knowledge_scope(kb),
        ):
            return json.loads(registry.load()["get_dosage_information"].call(kwargs))

    return _call


# ------------------------------------------------------------ the parser ---


def test_every_record_splits_into_two_cohorts(records):
    for record in records:
        cohorts = split_cohorts(record.dosage)
        assert set(cohorts) == {Cohort.ADULT, Cohort.PAEDIATRIC}, record.name


def test_the_split_is_verbatim(records):
    """§4.16 — *"Reproduce doses, units, intervals, and maxima verbatim; do not
    restate, round, convert, or normalize them."* Substring arithmetic, so
    verbatim is a property of the code rather than a promise about it."""
    for record in records:
        for dosing in split_cohorts(record.dosage).values():
            assert dosing.text in record.dosage, record.name


def test_a_record_with_no_markers_is_unsplittable():
    with pytest.raises(DosageUnsplittable, match="Children"):
        split_cohorts("Paracetamol 500mg four times daily.")


def test_a_record_with_the_cohorts_reversed_is_unsplittable():
    """Wrong boundaries here mean one cohort's figure served as the other's, so
    an unexpected order must fail rather than be guessed at."""
    with pytest.raises(DosageUnsplittable, match="precedes"):
        split_cohorts("Adults: 500mg daily. Children: 10mg/kg daily.")


def test_the_corpus_loader_rejects_an_unsplittable_record(tmp_path):
    """At load, so a changed source file fails the build rather than the answer."""
    source = tmp_path / "bad.csv"
    source.write_text(
        "name of disease|brief description|causes|symptoms|treatment|dosage\n"
        "Testitis|A test|Testing|Aches|Rest|Take 500mg four times daily.\n",
        encoding="utf-8",
    )

    report = load(source)

    assert report.records == []
    assert "will not split by cohort" in report.rejected[0][2]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Digoxin (10 mcg/kg/day) for rate control", Basis.WEIGHT_BASED),
        ("ORS 50-100ml/kg over 4 hours", Basis.WEIGHT_BASED),
        ("Methotrexate (10-15mg/m2) once weekly", Basis.BODY_SURFACE_AREA),
        ("Paracetamol 500-1000mg every 4-6 hours", Basis.FIXED),
        ("Not applicable.", Basis.NOT_RECORDED),
    ],
)
def test_the_basis_is_read_from_the_denominator_not_a_unit_list(text, expected):
    """mg/kg is not the only scaled form. The corpus also has mcg/kg (Digoxin,
    the narrowest therapeutic index in the file), ml/kg and mg/m2."""
    assert dosing_basis(text) is expected


def test_body_surface_area_needs_a_maximum_too():
    """§4.16 says "weight-based". 10-15mg/m2 is exactly as open-ended, and
    reading the clause narrowly enough to exclude it would honour the words and
    miss the point."""
    dosing = CohortDosing(cohort=Cohort.PAEDIATRIC, text="Methotrexate (10-15mg/m2) weekly")

    assert dosing.requires_maximum


def test_a_maximum_is_returned_verbatim():
    assert find_maximum("Paracetamol 15mg/kg every 4-6 hours (max 75mg/kg/day).") == (
        "max 75mg/kg/day"
    )
    assert find_maximum("Paracetamol 500-1000mg every 4-6 hours (max 4g/day).") == "max 4g/day"


def test_no_maximum_is_invented_where_none_is_recorded():
    """A false negative here produces a warning; a false positive would produce
    a fabricated ceiling, which is the worse failure by a wide margin."""
    assert find_maximum("Enoxaparin (1mg/kg) injected subcutaneously twice daily") is None


def test_the_applicability_qualifier_is_preserved(records):
    """12 of 65 paediatric entries carry one — "(6-12 years)", "(10+)", "(JIA)",
    "(Iron deficiency)". It is the condition under which the dose applies, and
    dropping it would widen a dose to a cohort it was never written for."""
    qualified = {
        record.name: split_cohorts(record.dosage)[Cohort.PAEDIATRIC].qualifier
        for record in records
        if split_cohorts(record.dosage)[Cohort.PAEDIATRIC].qualifier
    }

    assert len(qualified) == 12
    assert qualified["Common Cold (Viral Rhinitis)"] == "6-12 years"
    assert qualified["Rheumatoid Arthritis"] == "JIA"
    assert qualified["Anemia"] == "Iron deficiency"


def test_the_measured_shape_of_the_corpus(records):
    """The numbers Decision 2 rests on, pinned so a corpus change is visible.

    27 cohort entries carry a scaled figure and exactly one records a maximum,
    so the escalated notice fires 26 times out of 27.
    """
    scaled = [
        dosing
        for record in records
        for dosing in split_cohorts(record.dosage).values()
        if dosing.requires_maximum
    ]

    assert len(scaled) == 27
    assert sum(1 for d in scaled if d.maximum) == 1


# ------------------------------------------------------- the two exhaustives ---


@pytest.mark.parametrize("cohort", ["adult", "paediatric", "both"])
def test_no_scaled_figure_is_ever_returned_without_its_guard(call, records, cohort):
    """§8's demonstration, restated per Decision 2 and run over all 65 records.

    Never returned without a dosing basis, without an explicit maximum-daily-dose
    field, and — where that field reads "not recorded" — without the escalated
    notice.
    """
    for record in records:
        result = call(clinical(), condition_name=record.name, cohort=cohort)

        for name, block in result["cohorts"].items():
            if not SCALED.search(block["dosing"]):
                continue
            where = f"{record.name}/{name}"
            assert block["dosing_basis"] in {"weight_based", "body_surface_area"}, where
            assert "maximum_daily_dose" in block, where
            assert block["verification_notice"] == VERIFICATION_NOTICE, where
            if block["maximum_daily_dose"] == NO_MAXIMUM_RECORDED:
                assert block["incomplete_source_notice"] == INCOMPLETE_SOURCE_NOTICE, where


def test_no_cohort_is_ever_served_the_other_cohorts_figure(call, records):
    """§4.16 — *"never substitute the other cohort's figure"*. Over every record,
    because this is the failure that would be invisible in a demo."""
    for record in records:
        cohorts = split_cohorts(record.dosage)
        result = call(clinical(), condition_name=record.name, cohort="both")

        for key, other in ((Cohort.ADULT, Cohort.PAEDIATRIC), (Cohort.PAEDIATRIC, Cohort.ADULT)):
            returned = result["cohorts"][key.value]["dosing"]
            if cohorts[key].recorded:
                assert returned == cohorts[key].text, record.name
            else:
                assert returned == NO_DOSING_RECORDED, record.name
                assert returned != cohorts[other].text, record.name


# --------------------------------------------------------- Appendix A.2 ---


def test_the_response_follows_appendix_a2(call):
    result = call(clinical(), condition_name="Cystitis", cohort="both")

    assert result["record"] == "Cystitis"
    assert "disease_list.csv" in result["citation"]
    assert result["treatment_context"] == records_by_name()["Cystitis"].treatment
    assert set(result["cohorts"]) == {"adult", "paediatric"}
    assert result["notice"] == STANDING_NOTICE


def test_the_standing_notice_is_appendix_a4_verbatim():
    """§4.16 — appended to every response. A.4's own words, so a reader can
    check it against the specification."""
    for phrase in (
        "for clinician review",
        "Not a diagnosis, a treatment plan, or a prescription",
        "not a current formulary or guideline service",
        "responsibility rest with the treating clinician",
    ):
        assert phrase in STANDING_NOTICE


def test_treatment_context_can_be_switched_off(call):
    result = call(
        clinical(), condition_name="Cystitis", cohort="adult", include_treatment_context=False
    )

    assert "treatment_context" not in result


def test_an_absent_cohort_uses_the_wording_section_4_16_prescribes(call):
    """*"render it as no dosing recorded in the source documents for this
    cohort"* — and never as an absence of contraindication."""
    result = call(clinical(), condition_name="Stroke", cohort="paediatric")
    block = result["cohorts"]["paediatric"]

    assert block["dosing"] == NO_DOSING_RECORDED
    assert block["recorded"] is False
    assert block["dosing_basis"] == "not_recorded"
    # Nothing to bound, so no ceiling field and no warning about one.
    assert "maximum_daily_dose" not in block
    assert "safe" not in json.dumps(result).lower()


def test_a_recorded_maximum_suppresses_the_escalated_notice_only(call):
    """The discrimination Decision 2 is for. Fever is the one record that states
    a maximum: the routine formulary check still applies, the incomplete-source
    warning does not. If they were one string this test could not exist."""
    result = call(clinical(), condition_name="Fever (Pyrexia)", cohort="paediatric")
    block = result["cohorts"]["paediatric"]

    assert block["maximum_daily_dose"] == "max 75mg/kg/day"
    assert block["verification_notice"] == VERIFICATION_NOTICE
    assert "incomplete_source_notice" not in block


def test_the_two_notices_are_different_sentences():
    """Decision 2's mitigation. The escalated one fires on 26 of 27 scaled
    entries; if it read like the routine one, the reader would learn to skip the
    sentence that actually varies."""
    assert VERIFICATION_NOTICE != INCOMPLETE_SOURCE_NOTICE
    assert "no maximum daily dose" in INCOMPLETE_SOURCE_NOTICE
    assert "unbounded" in INCOMPLETE_SOURCE_NOTICE
    assert "not a statement that no ceiling applies" in INCOMPLETE_SOURCE_NOTICE.lower()


def test_a_fixed_dose_carries_no_ceiling_field(call):
    """A fixed figure is interpretable without a maximum, so demanding one would
    be noise — and noise is what Decision 2's risk actually is."""
    result = call(clinical(), condition_name="Fever (Pyrexia)", cohort="adult")

    assert result["cohorts"]["adult"]["dosing_basis"] == "fixed"
    assert "maximum_daily_dose" not in result["cohorts"]["adult"]
    assert "verification_notice" not in result["cohorts"]["adult"]


def test_the_qualifier_reaches_the_payload(call):
    result = call(clinical(), condition_name="Common Cold (Viral Rhinitis)", cohort="paediatric")

    assert result["cohorts"]["paediatric"]["applies_to"] == "children (6-12 years)"


# ---------------------------------------------------------- near misses ---


def test_a_condition_not_in_the_corpus_returns_nothing(call):
    """§4.16 — *"Return no result rather than a near match"*. A neighbouring
    record's dose is a different drug."""
    result = call(clinical(), condition_name="Cystits", cohort="adult")

    assert result["error"] == "not_in_corpus"
    assert "nearest record" in result["remedy"]


def test_an_alias_the_source_encodes_in_the_name_resolves(call):
    """Found live: asked for the paediatric dose for "Pyrexia", the assistant
    was told the corpus does not cover fever — false, and it reads as an absence
    of data rather than a lookup that did not match.

    Thirteen of the 65 names carry a parenthesised alias, and a clinician will
    type "DVT" or "IBS". That is not a near match: the source is what says the
    two names are the same record. §4.16's prohibition is on returning a
    *different* record, and this returns the same one.
    """
    assert canonical_name("Pyrexia") == "Fever (Pyrexia)"
    assert canonical_name("Fever") == "Fever (Pyrexia)"
    assert canonical_name("DVT") == "Deep Vein Thrombosis (DVT)"
    assert canonical_name("pink eye") == "Conjunctivitis (Pink Eye)"

    assert call(clinical(), condition_name="Pyrexia", cohort="paediatric")["record"] == (
        "Fever (Pyrexia)"
    )


def test_a_typo_is_still_not_a_match(call):
    """The line the alias half must not cross. "Cystits" is not Cystitis, and a
    neighbouring condition's dose is a different drug."""
    assert canonical_name("Cystits") is None
    assert canonical_name("pneumonia complicated") is None
    assert canonical_name("") is None


def test_an_ambiguous_alias_resolves_to_nothing(monkeypatch):
    """Fails closed. If a future corpus made one alias name two records,
    guessing between them is exactly the near match §4.16 rules out."""
    from app.knowledge import corpus

    corpus._by_alias.cache_clear()
    monkeypatch.setattr(
        corpus,
        "records_by_name",
        lambda: {"Alpha (X)": object(), "Beta (X)": object()},
    )

    assert corpus.canonical_name("X") is None
    assert corpus.canonical_name("Alpha") == "Alpha (X)"
    corpus._by_alias.cache_clear()


def test_case_and_spacing_are_typing_not_a_different_condition(call):
    assert canonical_name("  cystitis ") == "Cystitis"
    assert call(clinical(), condition_name="CYSTITIS", cohort="adult")["record"] == "Cystitis"


def test_a_medication_with_no_match_returns_nothing(call):
    result = call(clinical(), medication_name="zzzznotadrug", cohort="adult")

    assert result["error"] == "not_in_corpus"
    assert "own knowledge" in result["remedy"]


def test_naming_both_a_condition_and_a_medication_is_refused(call):
    result = call(
        clinical(), condition_name="Cystitis", medication_name="amoxicillin", cohort="adult"
    )

    assert result["error"] == "invalid_arguments"


def test_naming_neither_is_refused(call):
    assert call(clinical(), cohort="adult")["error"] == "invalid_arguments"


# -------------------------------------------------------------- refusals ---


def test_a_patient_session_cannot_name_this_function(call):
    result = call(Session(), condition_name="Fever (Pyrexia)", cohort="paediatric")

    assert result["error"] == "unknown_function"
    assert not SCALED.search(json.dumps(result))


def test_an_expired_session_is_refused_without_a_figure(call):
    result = call(clinical(expired=True), condition_name="Fever (Pyrexia)", cohort="paediatric")

    assert result["error"] == "session_expired"
    assert not SCALED.search(json.dumps(result))


@pytest.mark.parametrize("cohort", ["adult", "paediatric", "both"])
def test_no_dose_reaches_a_patient_session_for_any_cohort(call, cohort):
    """The §7.3 claim from the argument side: nothing a patient session can pass
    produces a figure."""
    result = call(Session(), condition_name="Fever (Pyrexia)", cohort=cohort)

    assert "cohorts" not in result
    assert not SCALED.search(json.dumps(result))


# ------------------------------------------------------------ the audit ---


def test_the_call_is_audited_with_what_section_4_16_asks_for(call, audit):
    """*"staff identifier, condition or medication requested, cohort, chunk
    identifiers returned"*."""
    call(clinical(), condition_name="Cystitis", cohort="paediatric")

    detail = audit.of_kind("clinical_retrieval")[0]
    assert detail["staff_id"] == "STAFF-2001"
    assert detail["condition_name"] == "Cystitis"
    assert detail["cohort"] == "paediatric"
    assert detail["chunks"] == ["cystitis::management"]


def test_the_audit_records_whether_a_maximum_was_found(call, audit):
    """Decision 2's residual risk is that the warning stops being read. A
    reviewer counting how often the no-maximum path fired is the only way to
    measure that, so the record has to carry it."""
    call(clinical(), condition_name="Cystitis", cohort="paediatric")
    call(clinical(), condition_name="Fever (Pyrexia)", cohort="paediatric")

    notes = audit.of_kind("clinical_retrieval")
    assert notes[0]["maximum_recorded"] == {"paediatric": False}
    assert notes[1]["maximum_recorded"] == {"paediatric": True}


def test_a_near_miss_is_audited_too(call, audit):
    call(clinical(), condition_name="Cystits", cohort="adult")

    assert audit.of_kind("clinical_retrieval")[0]["outcome"] == "not_in_corpus"


# ------------------------------------------------ what it must never produce ---


def test_the_result_is_never_shaped_like_a_prescription(call, records):
    """§4.16 — *"Do not generate, transmit, or format a prescription, a
    medication order, or anything that reads as one."* The function cannot stop
    the model formatting one, but it must not hand over a template."""
    forbidden = ("sig:", "rx", "dispense", "refill", "quantity:", "prescriber", "signature")

    for record in records[:12]:
        blob = json.dumps(call(clinical(), condition_name=record.name, cohort="both")).lower()
        for token in forbidden:
            assert token not in blob, f"{record.name}: {token}"


def test_nothing_in_the_result_computes_a_patient_dose(call):
    """§4.16 — *"Do not calculate a dose for a specific patient, weight, age, or
    renal function."* Ranges come back as ranges."""
    result = call(clinical(), condition_name="Cystitis", cohort="paediatric")
    dosing = result["cohorts"]["paediatric"]["dosing"]

    assert "20-40mg/kg/day" in dosing
    assert "next_step" in result
    assert "do not calculate" in result["next_step"].lower()


def test_the_next_step_flags_an_unbounded_figure_before_the_figure(call):
    result = call(clinical(), condition_name="Cystitis", cohort="paediatric")

    assert "no recorded" in result["next_step"]
    assert "before the figure" in result["next_step"]
