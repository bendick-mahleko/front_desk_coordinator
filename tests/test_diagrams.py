"""The inline-SVG diagrams (design items 3 and 4).

Static SVG, so it can be asserted as text — which is the reason to draw it this
way rather than with a component: the picture a reviewer sees is the string these
functions return, and a test can read it.

The load-bearing test is that every denial code the gate can emit maps to a
stage. A diagram that draws the wrong stage is worse than no diagram, because a
reviewer would trust it.
"""

from __future__ import annotations

import re

import pytest

from app.policy.messages import DenialCode
from ui import design, diagrams


def _svg(markup: str) -> str:
    assert markup.startswith("<svg"), markup[:40]
    assert markup.endswith("</svg>")
    return markup


# ------------------------------------------------------- the gate pipeline ---


def test_every_denial_code_maps_to_a_stage():
    """A code with no stage would draw a pipeline that stopped nowhere.

    Read off the gate's own ordering, so a code added to DenialCode without a
    stage fails here rather than rendering a confident lie.
    """
    unmapped = {code.value for code in DenialCode if code.value not in diagrams.STAGE_BY_CODE}

    assert unmapped == set(), f"denial codes with no pipeline stage: {unmapped}"


def test_every_mapped_stage_exists():
    """The other direction — a stage name that is not in the pipeline."""
    for code, stage in diagrams.STAGE_BY_CODE.items():
        assert stage in diagrams.STAGES, f"{code} maps to unknown stage {stage}"


def test_the_stages_are_the_gates_own_order():
    """Not a prettier order. The picture is only useful if it is the real one."""
    assert diagrams.STAGES == (
        "role",
        "auth",
        "schema",
        "authorization",
        "provenance",
        "preconditions",
    )


def test_an_allowed_call_draws_no_failure():
    markup = _svg(diagrams.gate_pipeline(True, None))

    assert "allowed" in markup
    assert design.DENY not in markup
    assert "never ran" not in markup


def test_a_denial_names_the_stage_that_stopped_it():
    markup = _svg(diagrams.gate_pipeline(False, "verification_required"))

    assert "stopped at authorization" in markup
    assert design.DENY in markup


def test_a_denial_says_how_many_checks_never_ran():
    """The distinction the diagram exists for: refused at stage 1 and refused at
    stage 4 read identically as prose."""
    early = _svg(diagrams.gate_pipeline(False, "unknown_function"))
    late = _svg(diagrams.gate_pipeline(False, "precondition_failed"))

    assert "5 later check(s) never ran" in early
    assert "never ran" not in late  # nothing comes after the last stage


def test_a_passed_stage_carries_a_glyph_as_well_as_a_colour():
    """Never colour alone, in the diagram too."""
    markup = _svg(diagrams.gate_pipeline(False, "unknown_reference"))

    assert markup.count("✓") >= 4


def test_an_unknown_code_does_not_invent_a_stage():
    """Fails visibly rather than pointing at a stage it guessed."""
    markup = _svg(diagrams.gate_pipeline(False, "something_new"))

    assert "an unnamed stage" in markup


def test_the_diagram_is_labelled_for_a_screen_reader():
    for markup in (
        diagrams.gate_pipeline(True, None),
        diagrams.gate_pipeline(False, "verification_required"),
    ):
        assert 'role="img"' in markup
        assert 'aria-label="Gate pipeline:' in markup


# ------------------------------------------------------------ tier bands ---


def test_a_patient_session_draws_the_clinician_tier_locked():
    """§1.3 as a picture. The band is dashed and labelled, not merely absent."""
    markup = _svg(diagrams.tier_bands(["routing_only"], ["patient_safe", "routing_only"]))

    assert "locked" in markup
    assert "clinician_only" in markup
    assert "3 3" in markup  # the dashed stroke


def test_a_clinical_session_draws_no_locked_band():
    markup = _svg(
        diagrams.tier_bands(
            ["clinician_only"],
            ["patient_safe", "routing_only", "clinician_only"],
            surface="clinical",
        )
    )

    assert "locked" not in markup
    assert "queried" in markup


def test_all_three_tiers_are_always_drawn():
    """Omitting the locked one would make the restriction invisible, which is the
    opposite of the point."""
    markup = diagrams.tier_bands([], ["patient_safe"])

    for tier in ("patient_safe", "routing_only", "clinician_only"):
        assert tier in markup


# ------------------------------------------------------ provenance ledger ---


def test_an_empty_ledger_says_so_rather_than_drawing_nothing():
    markup = _svg(diagrams.provenance_ledger({}))

    assert markup.count("none yet") == 3


def test_the_ledger_draws_what_was_handed_out():
    markup = _svg(
        diagrams.provenance_ledger(
            {"patient_ids": ["PT-4101"], "appointment_ids": ["AP-77301"], "slot_ids": []}
        )
    )

    assert "PT-4101" in markup
    assert "AP-77301" in markup
    assert "none yet" in markup  # slots


def test_the_ledger_summarises_a_long_list():
    markup = diagrams.provenance_ledger({"slot_ids": [f"SL-{n}" for n in range(9)]})

    assert "+5 more" in markup


def test_the_ledger_only_ever_shows_safe_references():
    """Every key it can draw is one the redactor exempts as a clinic-issued
    reference rather than a fact about a person. Asserted against the redactor's
    own list, so the two cannot drift."""
    from app.policy.redaction import SAFE_REFERENCE_FIELDS

    singular = {
        "patient_ids": "patient_id",
        "appointment_ids": "appointment_id",
        "slot_ids": "slot_id",
    }
    assert set(diagrams.LEDGER_LABELS) == set(singular)
    for key in diagrams.LEDGER_LABELS:
        assert singular[key] in SAFE_REFERENCE_FIELDS, key


def test_markup_is_escaped():
    """The ledger draws values that came from a backend. They are clinic ids
    today; the escaping is not conditional on that staying true."""
    markup = diagrams.provenance_ledger({"patient_ids": ["<script>x</script>"]})

    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup


# --------------------------------------------------- verification ladder ---


@pytest.mark.parametrize(
    "status,expected_filled",
    [("none", 0), ("open", 1), ("identified", 2), ("verified", 3)],
)
def test_the_ladder_fills_to_the_current_rung(status, expected_filled):
    markup = _svg(diagrams.verification_ladder(status))

    filled = len(re.findall(r'<circle[^/]*fill="#[0-9a-fA-F]{6}"', markup))
    assert filled == expected_filled


def test_the_ladder_marks_where_the_conversation_is():
    assert "now here" in diagrams.verification_ladder("identified")


def test_an_unknown_status_fills_nothing_rather_than_guessing():
    """`registered` and `locked` are real session statuses that are not rungs on
    the §3 ladder. Drawing them as a rung would be a claim the gate does not
    make."""
    markup = diagrams.verification_ladder("registered")

    assert "now here" not in markup
    assert re.findall(r'<circle[^/]*fill="none"', markup)


# ----------------------------------------------------------- the support bar ---


def test_the_support_bar_keeps_the_number():
    """A bar for comparison, the number for a threshold check. §4.15 needs the
    ordering legible as strength of support; a clinician checking the floor needs
    the value."""
    markup = design.support_bar(0.408)

    assert "0.41" in markup


@pytest.mark.parametrize("score", [-1.0, 0.0, 0.5, 1.0, 2.0])
def test_the_support_bar_survives_any_score(score):
    """Scores come from cosine similarity, which is bounded in theory and worth
    clamping in practice."""
    assert "width:" in design.support_bar(score)
