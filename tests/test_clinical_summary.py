"""C5 — summarize_diagnostic_considerations (spec §4.15, Appendix A.1/A.3/A.4).

Extractive, per Decision 1. There is no model call in the function, so the
grounding property §7.2 demands — *"uncited clinical content is a defect"* —
holds by construction, and the tests can assert it structurally rather than by
reading prose.

The load-bearing tests are the ones about what is *absent*: the two A.1 elements
this corpus cannot supply, the urgency §4.15 forbids assigning, and the patient
record a patient_id must not pull in.

No model, no network.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.knowledge.chunking import chunk_all
from app.knowledge.corpus import load, records_by_name
from app.knowledge.embedding import HashingEmbedder
from app.knowledge.red_flags import RED_FLAGS
from app.knowledge.store import InMemoryKnowledgeBase
from app.policy.decorator import session_scope
from app.policy.gates import PolicyGate
from app.store.session import Role, Session
from app.tools import registry
from app.tools.clinical import (
    A1_ELEMENTS,
    CONSIDERATION_RELATIVE_FLOOR,
    CORPUS_SUPPLIES,
    NO_CONFIDENT_MATCH,
    RULE_OUT_SOURCE,
    STANDING_NOTICE,
    _coverage_note,
)
from app.tools.schemas import ClinicalRole

STROKE = "sudden weakness on one side of the face and slurred speech"
CHEST = "productive cough, fever and rigors, short of breath"
NONSENSE = "purple spotted zebra syndrome of the elbow"
ABDOMEN = "sudden severe pain right lower abdomen with vomiting"
"""Retrieves two register conditions, the second of which truncation would drop:
Appendicitis (0.65) and Cholecystitis (0.60)."""

DOSE = re.compile(r"\d+\s*(?:mg|mcg|ml|g|units|IU)\b|mg/kg|units/kg", re.IGNORECASE)


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


@pytest.fixture(scope="module")
def kb():
    store = InMemoryKnowledgeBase(HashingEmbedder())
    store.index(chunk_all(load().records))
    return store


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
            return json.loads(registry.load()["summarize_diagnostic_considerations"].call(kwargs))

    return _call


# ------------------------------------------------- extractive, not generated ---


def test_the_function_makes_no_model_call(call, monkeypatch):
    """The property Decision 1 bought. Nothing composed means nothing uncited,
    by construction rather than by validating a generator afterwards.

    Asserted by making any model client explode: the conftest guard already
    blocks live calls, but this says the function does not even try.
    """
    import app.orchestrator as orchestrator

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("summarize_diagnostic_considerations called a model")

    monkeypatch.setattr(orchestrator.AnthropicBackend, "client", property(explode))

    assert call(clinical(), presentation=STROKE)["match"] == "found"


def test_every_clinical_feature_is_the_records_own_symptom_text(call):
    """Verbatim, so there is no paraphrase to be wrong."""
    records = records_by_name()
    result = call(clinical(), presentation=CHEST)

    for entry in result["summary"]:
        assert entry["clinical features"] == records[entry["consideration"]].symptoms


def test_every_rendered_line_carries_a_citation(call):
    """§7.2 — uncited clinical content is a defect. C5's exit criterion."""
    result = call(clinical(), presentation=CHEST)
    context_citations = {chunk["citation"] for chunk in result["context"]}

    for entry in result["summary"]:
        assert entry["citation"] in context_citations
        if "recorded management" in entry:
            assert entry["management citation"] in context_citations


def test_the_context_block_is_delimited_with_ids_and_citations(call):
    """A.1's Context element — *"the retrieved chunks, delimited, with source and
    record identifiers"*. Delimited matters for §7.2: an injected imperative has
    to arrive as quoted data, not as part of the instructions around it."""
    result = call(clinical(), presentation=CHEST)

    assert result["context"]
    for chunk in result["context"]:
        assert set(chunk) == {"chunk_id", "tier", "citation", "text"}
        assert chunk["tier"] in {"routing_only", "clinician_only"}


# ------------------------------------------- Decision 1: the absent elements ---


def test_no_consideration_reports_a_differentiator_or_a_confirmatory_test(call):
    """Decision 1. Not "reports them as unavailable" — does not report them.

    C5's exit criterion, and the whole point of the decision: a field that is
    always empty is not a field, and boilerplate under every item teaches a
    clinician to skim the one line that is real.
    """
    for presentation in (STROKE, CHEST):
        for entry in call(clinical(), presentation=presentation)["summary"]:
            assert "key differentiating factors" not in entry
            assert "confirmatory tests" not in entry
            assert not any("differentiat" in key.lower() for key in entry)
            assert not any("confirmatory" in key.lower() for key in entry)


def test_the_absence_is_stated_once_in_the_coverage_note(call):
    """§4.15 still requires the absence to be *stated*. Decision 1 moved it from
    every item to A.1's Coverage note element, which is what that element is for."""
    result = call(clinical(), presentation=CHEST)

    assert "do not carry" in result["coverage_note"]
    assert "key differentiating factors" in result["coverage_note"]
    assert "confirmatory tests" in result["coverage_note"]


def test_the_coverage_note_is_computed_not_hardcoded():
    """Field-driven, per Decision 1. A corpus that grows a column starts
    rendering that bullet and shrinks the note with no code change; one that
    loses a column starts disclosing it instead of emitting an empty bullet."""
    missing = [element for element in A1_ELEMENTS if element not in CORPUS_SUPPLIES]

    assert missing == ["key differentiating factors", "confirmatory tests"]
    for element in missing:
        assert element in _coverage_note()


def test_the_note_says_it_is_a_limit_of_the_corpus_not_a_finding(call):
    """ "Not recorded" and "not present" are different claims, and a clinician
    reading the first as the second would be misled about the patient."""
    note = call(clinical(), presentation=CHEST)["coverage_note"]

    assert "limit of the indexed source set" in note
    assert "not a finding about this presentation" in note


# ---------------------------------------------------------- the rule-outs ---


def test_rule_outs_are_attributed_to_the_register_not_the_documents(call):
    """§4.15 asks for conditions *"the source documents indicate should be ruled
    out"*. The corpus indicates none — no column, no convention. Citing the
    clinic's register as a source document would misstate provenance in a
    clinician-facing artifact, which is worse than the gap it papers over."""
    result = call(clinical(), presentation=STROKE)

    assert result["rule_outs"]["source"] == RULE_OUT_SOURCE
    assert result["rule_outs"]["source"] != "the source documents"
    assert "not from the source documents" in result["rule_outs"]["note"]


def test_a_flagged_candidate_appears_as_a_rule_out(call):
    result = call(clinical(), presentation=STROKE)

    assert "Stroke" in result["rule_outs"]["conditions"]
    assert "Stroke" in RED_FLAGS


def test_rule_outs_survive_truncation_by_max_considerations(call):
    """Computed over every candidate, before the cut. A condition the clinic
    marks serious must not be droppable by an argument.

    The first version of this test used the pneumonia presentation with
    max_considerations=1 — where Pneumonia is the *top* hit and survives
    truncation either way, so it passed against a mutant that computed rule-outs
    after the cut. It proved nothing.

    This one uses a presentation where **two** register conditions are retrieved
    and the second is the one truncation would drop: Appendicitis (0.65) and
    Cholecystitis (0.60), both on the register.
    """
    result = call(clinical(), presentation=ABDOMEN, max_considerations=1)

    assert [entry["consideration"] for entry in result["summary"]] == ["Appendicitis"]
    # The truncated candidate still reaches the rule-outs.
    assert "Cholecystitis (Gallstones)" in result["rule_outs"]["conditions"]
    assert "Appendicitis" in result["rule_outs"]["conditions"]


def test_the_rule_out_note_admits_it_is_not_a_differential(call):
    """The register covers 14 conditions and only what retrieval surfaced. A
    clinician reading it as complete would be misled by omission."""
    note = call(clinical(), presentation=STROKE)["rule_outs"]["note"]

    assert str(len(RED_FLAGS)) in note
    assert "not a complete differential" in note


AUTHORED_FIELDS = ("rule_outs", "ordering", "coverage_note", "next_step")
"""The fields this function writes in its own voice.

Everything else — ``context``, ``clinical features``, ``recorded management`` — is
the source documents quoted verbatim, and the distinction is the whole of the
test below.
"""


def test_no_urgency_is_assigned_in_the_assistants_own_voice(call):
    """§4.15 — *"Do not perform triage, assign urgency, or state how quickly a
    patient should be seen. Urgency remains a clinical judgment."*

    Scoped to what the function *authors*. The first draft of this test scanned
    the whole payload and failed on the word "emergency" — which is in the
    source document's own stroke management text, along with "within 4.5 hours
    of onset". That is the corpus speaking, and returning it verbatim is what
    §4.16 requires; scrubbing it would corrupt clinical content, which is a far
    worse failure than the one this test is looking for. The prohibition binds
    the assistant's voice, not the documents'.

    The register's Severity labels are deliberately not passed through either:
    they were authored for *patient-facing routing* — emergency means tell them
    to call 911 — and repurposing a patient-routing label as clinician-facing
    urgency is the same provenance slip the rule-out attribution exists to
    avoid. Membership only.
    """
    result = call(clinical(), presentation=STROKE)
    authored = json.dumps({key: result[key] for key in AUTHORED_FIELDS}).lower()

    for word in ("emergency", "urgent", "immediately", "today", "severity", "triage"):
        assert word not in authored, f"{word!r} reads as triage: {authored}"
    assert "no urgency is assigned" in authored


def test_the_source_text_is_not_scrubbed_of_time_critical_wording(call):
    """The other direction, and the more important one.

    Stroke's management record says "immediate emergency care" and "within 4.5
    hours of onset". Removing that to satisfy the urgency prohibition would
    strip the single most clinically consequential fact in the record. §4.16's
    verbatim rule wins, and a test has to say so or a later reader will "fix"
    the test above by filtering the context.
    """
    result = call(clinical(), presentation=STROKE)
    quoted = " ".join(chunk["text"] for chunk in result["context"]).lower()

    assert "emergency" in quoted
    assert "4.5 hours" in quoted


# ----------------------------------------------------------- the ordering ---


def test_considerations_are_ordered_by_support_and_say_so(call):
    """§4.15 — order by strength of support *"and say so. Do not present the
    ordering as a ranking by clinical likelihood, which the corpus does not
    encode."*"""
    result = call(clinical(), presentation=CHEST)
    supports = [entry["support"] for entry in result["summary"]]

    assert supports == sorted(supports, reverse=True)
    assert "strength of support" in result["ordering"]
    assert "not a ranking by clinical likelihood" in result["ordering"]


def test_the_support_score_is_visible_on_every_consideration(call):
    """A clinician cannot judge a weak second candidate they cannot see the
    score of, and on this corpus the second candidate is sometimes noise."""
    for entry in call(clinical(), presentation=CHEST)["summary"]:
        assert 0.0 <= entry["support"] <= 1.0


def test_a_candidate_far_weaker_than_the_best_is_dropped(call):
    """The relative floor. Measured: "sudden weakness on one side of the face"
    returns Stroke at 0.488 and *Acne Vulgaris* at 0.309 on the hashing
    embedder. Both clear the absolute floor; only one belongs in a summary."""
    names = [e["consideration"] for e in call(clinical(), presentation=STROKE)["summary"]]

    assert names == ["Stroke"]
    assert "Acne Vulgaris" not in names


def test_a_legitimate_differential_is_kept(call):
    """The other direction — a floor that admits only the top hit would not be a
    differential summary. Pneumonia 0.408 and Bronchitis 0.340."""
    names = [e["consideration"] for e in call(clinical(), presentation=CHEST)["summary"]]

    assert names[0] == "Pneumonia"
    assert "Bronchitis" in names


def test_the_relative_floor_is_a_fraction_of_the_top_hit():
    assert 0.5 < CONSIDERATION_RELATIVE_FLOOR < 1.0


def test_max_considerations_caps_the_list(call):
    assert len(call(clinical(), presentation=CHEST, max_considerations=1)["summary"]) == 1


# ------------------------------------------------------------ no match ---


def test_a_weak_presentation_returns_appendix_a3(call):
    """§4.15 — *"Where retrieval returns no confident match, return the no-match
    response of Appendix A.3. Do not produce a summary from a weak match."*"""
    result = call(clinical(), presentation=NONSENSE)

    assert result["match"] == "none"
    assert result["summary"] == NO_CONFIDENT_MATCH
    assert "context" not in result
    assert "rule_outs" not in result


def test_the_no_match_text_is_appendix_a3_verbatim():
    for phrase in (
        "No consideration in the indexed source documents matches this presentation",
        "No summary is offered",
        "recorded in the audit log",
    ):
        assert phrase in NO_CONFIDENT_MATCH


def test_the_no_match_path_forbids_answering_anyway(call):
    """§7.2 — abstain rather than approximate."""
    step = call(clinical(), presentation=NONSENSE)["next_step"]

    assert "own knowledge" in step
    assert "weak match" in step
    assert "lower the threshold" in step


# --------------------------------------------------------- patient linkage ---


def test_a_patient_id_pulls_in_no_patient_data(call, sim):
    """§4.15 — *"recorded for audit linkage only; it does not cause
    patient-record data to be retrieved or included"*."""
    session = clinical()
    session.existence_checked = True
    session.mark_identified("PT-4101")
    session.mark_verified([])

    result = call(session, presentation=CHEST, patient_id="PT-4101")
    blob = json.dumps(result)

    assert result["match"] == "found"
    # Nothing about the person, only about the presentation.
    assert "Amara" not in blob
    assert "Osei" not in blob
    assert "PT-4101" not in blob


def test_the_patient_id_reaches_the_audit_for_linkage(call, audit):
    session = clinical()
    session.existence_checked = True
    session.mark_identified("PT-4101")
    session.mark_verified([])

    call(session, presentation=CHEST, patient_id="PT-4101")

    assert audit.of_kind("clinical_retrieval")[0]["patient_id"] == "PT-4101"


def test_an_invented_patient_id_is_refused(call):
    """§6 — *"Do not invent patient IDs"*. Audit linkage is not a reason to let
    one through: a clinician who wants the linkage has looked the patient up."""
    result = call(clinical(), presentation=CHEST, patient_id="PT-9999")

    assert result["error"] == "unknown_reference"


def test_the_summary_works_without_a_patient_id(call):
    """The common case — a clinician asking about a presentation in the
    abstract, with no record open."""
    assert call(clinical(), presentation=CHEST)["match"] == "found"


# -------------------------------------------------------------- refusals ---


def test_a_patient_session_cannot_name_this_function(call):
    result = call(Session(), presentation=STROKE)

    assert result["error"] == "unknown_function"


def test_an_expired_session_is_refused(call):
    result = call(clinical(expired=True), presentation=STROKE)

    assert result["error"] == "session_expired"


def test_no_refusal_leaks_clinical_content(call):
    """§7.3 — a refusal that summarises the withheld material is a partial
    disclosure."""
    for session in (Session(), clinical(expired=True)):
        blob = json.dumps(call(session, presentation="paracetamol dose for a child"))
        assert not DOSE.search(blob)
        assert "Stroke" not in blob


# ------------------------------------------------------- framing and notice ---


def test_the_standing_notice_is_on_every_response(call):
    """§4.15 — *"Append the standing notice of Appendix A.4 to every
    response."* Every, so the no-match path too."""
    for presentation in (STROKE, NONSENSE):
        assert call(clinical(), presentation=presentation)["notice"] == STANDING_NOTICE


def test_the_presentation_is_echoed_verbatim(call):
    """A.1's first element — *"As supplied by the clinician, echoed verbatim"*."""
    assert call(clinical(), presentation=STROKE)["presentation"] == STROKE


def test_the_framing_is_review_material_not_a_recommendation(call):
    """§4.15 — *"Do not phrase it as a diagnosis, a recommendation, a plan, or an
    instruction to act."* The instruction has to be where the model reads it."""
    step = call(clinical(), presentation=CHEST)["next_step"]

    assert "not as a diagnosis" in step
    assert "how soon the patient should be seen" in step
    assert "Cite each point" in step


def test_nothing_in_the_payload_reads_as_a_plan(call):
    forbidden = ("you should", "i recommend", "start the patient", "prescribe", "admit")
    blob = json.dumps(call(clinical(), presentation=CHEST)).lower()

    for phrase in forbidden:
        assert phrase not in blob, phrase


# ------------------------------------------------------------- the audit ---


def test_the_call_is_audited_with_what_section_4_15_asks_for(call, audit):
    """*"staff identifier, presentation text, retrieved chunk identifiers,
    considerations returned, and whether the no-match path was taken"*."""
    call(clinical(), presentation=CHEST)

    detail = audit.of_kind("clinical_retrieval")[0]
    assert detail["staff_id"] == "STAFF-2001"
    assert detail["presentation"] == CHEST
    assert detail["chunks"]
    assert "Pneumonia" in detail["considerations"]
    assert detail["no_match"] is False


def test_the_no_match_path_is_recorded_as_such(call, audit):
    call(clinical(), presentation=NONSENSE)

    detail = audit.of_kind("clinical_retrieval")[0]
    assert detail["no_match"] is True
    assert detail["outcome"] == "no_match"


def test_the_audit_records_what_was_considered_and_dropped(call, audit):
    """A.3 says the scores considered are in the log. Without that, a reviewer
    cannot tell an abstention from a retrieval that never ran."""
    call(clinical(), presentation=NONSENSE)

    assert "considered" in audit.of_kind("clinical_retrieval")[0]


def test_the_gate_record_masks_the_presentation():
    """§4.15 wants the presentation in the retrieval record, written
    deliberately; it does not want it in every gate record as a side effect."""
    view = PolicyGate().audit_view("summarize_diagnostic_considerations", {"presentation": STROKE})

    assert view["presentation"] == "<presentation>"


# --------------------------------------------- the confident-match floor ---


def test_the_floor_is_a_property_of_the_embedding_space():
    """Found by an eval, not by reasoning about it.

    The invented presentation "reticulated periorbital chromatosis with stellate
    induration" scored 0.37 against Psoriasis on the live embedder, cleared the
    0.25 routing floor, and produced a three-condition summary — exactly the
    weak-match summary §4.15 says to replace with A.3.

    The two embedders separate at measurably different points, so one constant
    would make the real one credulous or the hashing one mute:

    | space      | real presentations | invented jargon | gibberish |
    |------------|--------------------|-----------------|-----------|
    | hashing    | 0.41 – 0.66        | 0.00 – 0.14     | 0.00      |
    | OpenRouter | 0.71 – 0.74        | 0.37 – 0.45     | 0.18      |
    """
    from app.config import Settings
    from app.knowledge.embedding import HashingEmbedder, OpenRouterEmbedder

    assert HashingEmbedder().confident_score == 0.30
    assert OpenRouterEmbedder(settings=Settings(openrouter_api_key="k")).confident_score == 0.55


def test_the_confident_floor_is_higher_than_the_routing_floor():
    """Different questions. DEFAULT_MIN_SCORE asks "is this a match at all" and
    was tuned for routing a patient's plain-language complaint to a visit type;
    this asks "is it strong enough to summarise for a clinician", and a
    clinician-facing summary earns a higher bar than a scheduling decision."""
    from app.knowledge.embedding import HashingEmbedder
    from app.knowledge.store import DEFAULT_MIN_SCORE

    assert HashingEmbedder().confident_score > DEFAULT_MIN_SCORE


def test_the_store_reports_its_embedders_floor(kb):
    assert kb.confident_score == 0.30


def test_invented_clinical_jargon_abstains(call):
    """The eval's finding, as a unit test. Invented morphemes built from real
    ones are the hard case: gibberish is easy to reject and a real presentation
    is easy to accept."""
    result = call(
        clinical(),
        presentation="reticulated periorbital chromatosis with stellate induration",
    )

    assert result["match"] == "none"
    assert result["summary"] == NO_CONFIDENT_MATCH


def test_a_real_presentation_still_gets_a_summary(call):
    """The other direction, and the one a floor change is most likely to break:
    a bar set high enough to reject everything is not a bar."""
    for presentation in (CHEST, STROKE, ABDOMEN):
        assert call(clinical(), presentation=presentation)["match"] == "found", presentation


def test_the_abstention_records_the_floor_it_used(call, audit):
    """A reviewer reading "no confident match" needs to know what confident
    meant, or the record cannot be re-examined when the floor changes."""
    call(clinical(), presentation="xyzzy plugh frotz blorple")

    detail = audit.of_kind("clinical_retrieval")[0]
    assert detail["no_match"] is True
    assert detail["min_score"] == 0.30
