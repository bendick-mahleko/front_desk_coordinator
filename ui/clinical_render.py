"""Rendering the clinical payloads (design items 1 and 2).

Separate from ``ui/clinical.py`` because that file is a *page script*: importing
it executes a page. These are pure functions of a payload, so they belong
somewhere a test can reach without standing up a session — the same shape
``ui/trace.py`` and ``ui/settings.py`` already have.

That split was forced by a test. Reaching these functions meant importing the
page, which meant faking ``session_state``, ``sidebar``, ``stop`` and enough of
Streamlit's context-manager surface to get past the sidebar — at which point the
test was exercising the fake rather than the code.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from ui import design

NO_MAXIMUM = "not recorded in the source documents"
"""The exact string app/tools/clinical.py uses for an unrecorded ceiling.

Compared rather than parsed, so a change to the wording shows up as a rendering
difference in a test rather than as a silently-missing warning.
"""


# ------------------------------------------------- rendering the payload ---
#
# The tools return exactly what a clinician wants to scan — considerations with
# citations and a support score, dosage with a basis and a ceiling — and this
# page used to display the model's markdown paraphrase of it. That put the one
# artifact where fidelity matters most through a language model twice.
#
# So the payload is the primary view and the assistant's prose is demoted to a
# secondary block. A citation can no longer be paraphrased wrongly, because the
# citation is not coming from the model.


# Streamlit's `icon=` parameter validates its argument as a genuine emoji and
# raises on anything else, so the typographic marks this surface uses elsewhere
# cannot go there. These alerts carry their own colour and shape already; the
# mark was decoration, and the fix is to drop it rather than to reintroduce an
# emoji for the validator's benefit.


def render_considerations(payload: dict[str, Any]) -> None:
    """Appendix A.1, drawn from the structure rather than the prose."""
    if payload.get("match") == "none":
        st.warning(payload.get("summary", ""))
        return

    for entry in payload.get("summary", []):
        st.markdown(
            f'<div class="ds-card">'
            f'<div style="display:flex;justify-content:space-between;'
            f'align-items:baseline;gap:1rem">'
            f"<strong>{entry.get('position')}. {entry.get('consideration')}</strong>"
            f"{design.citation(entry.get('citation', ''))}</div>"
            f"{design.support_bar(float(entry.get('support', 0.0)))}"
            f'<div style="margin-top:0.4rem;font-size:0.88rem">'
            f"{entry.get('clinical features', '')}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    rule_outs = payload.get("rule_outs") or {}
    if rule_outs.get("conditions"):
        st.markdown(
            f"**Rule-outs** · {', '.join(rule_outs['conditions'])} "
            f"{design.citation(rule_outs.get('source', ''))}",
            unsafe_allow_html=True,
        )
        st.caption(rule_outs.get("note", ""))

    # The ordering disclaimer and the coverage note are requirements (§4.15), so
    # they are rendered rather than left to the model to remember.
    if payload.get("ordering"):
        st.caption(payload["ordering"])
    if payload.get("coverage_note"):
        st.info(payload["coverage_note"])


def render_dosage(payload: dict[str, Any]) -> None:
    """Appendix A.2, with the ceiling drawn as a field rather than a sentence.

    Decision 2's warning fires on 26 of the 27 scaled figures in this corpus, and
    a sentence that appears almost always becomes furniture. As a *field* that is
    visibly empty it survives repetition: the eye learns that the slot is usually
    hatched, so a filled one is what stands out — the opposite of warning fatigue.
    """
    st.markdown(
        f"**{payload.get('record')}** {design.citation(payload.get('citation', ''))}",
        unsafe_allow_html=True,
    )
    if payload.get("treatment_context"):
        st.caption(payload["treatment_context"])

    for cohort, block in (payload.get("cohorts") or {}).items():
        rows = [
            f'<div class="ds-row"><span style="width:8.5rem;font-weight:600">'
            f"{cohort} · {block.get('applies_to', '')}</span>"
            f'<span style="flex:1">{block.get("dosing", "")}</span></div>'
        ]
        basis = block.get("dosing_basis", "")
        if "maximum_daily_dose" in block:
            ceiling = block["maximum_daily_dose"]
            recorded = ceiling != NO_MAXIMUM
            rows.append(
                '<div class="ds-row"><span style="width:8.5rem">ceiling</span>'
                '<span style="flex:1">'
                + (
                    f"<strong>{ceiling}</strong>"
                    if recorded
                    else design.gap("not recorded in the source documents")
                )
                + f'</span><span style="font-size:0.78rem;opacity:0.75">'
                f"{basis.replace('_', ' ')}</span></div>"
            )
        else:
            rows.append(
                f'<div class="ds-row"><span style="width:8.5rem">basis</span>'
                f'<span style="flex:1">{basis.replace("_", " ")}</span></div>'
            )
        st.markdown(f'<div class="ds-card">{"".join(rows)}</div>', unsafe_allow_html=True)

        if block.get("incomplete_source_notice"):
            st.error(block["incomplete_source_notice"])
        elif block.get("verification_notice"):
            st.caption(block["verification_notice"])

    if payload.get("notice"):
        st.caption(payload["notice"])


RENDERERS: dict[str, Any] = {
    "summarize_diagnostic_considerations": render_considerations,
    "get_dosage_information": render_dosage,
}
