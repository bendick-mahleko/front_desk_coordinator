"""Static inline SVG for the three things the system does that prose hides.

No library, no runtime, no images: hand-authored `<svg>` in a markdown block,
which is the only drawing Streamlit can be trusted with. Static because an
animated version wants a real component, and that is the decision deferred with
item 5.

Each diagram exists because the corresponding property was *invisible until it
refused something*:

* the gate is a six-stage pipeline, and **which stage stopped a call** is the
  interesting fact — "never got past stage 1" and "reached authorization and was
  refused" are different claims that read identically as prose;
* the retrieval tier is a filter built before the query, and a locked band shows
  that better than a sentence can;
* the provenance ledger is the cleverest rule in the system and nobody can see
  it until `unknown_reference` fires.
"""

from __future__ import annotations

from ui.design import (
    ALLOW,
    DENY,
    MUTED,
    PALETTES,
    THEME_BORDER,
    THEME_INK,
    Surface,
)

# The gate's own order (app/policy/gates.py). Two of these were added by r3 and
# run ahead of the original four.
STAGES: tuple[str, ...] = (
    "role",
    "auth",
    "schema",
    "authorization",
    "provenance",
    "preconditions",
)

STAGE_BY_CODE: dict[str, str] = {
    "unknown_function": "role",
    "role_required": "auth",
    "session_expired": "auth",
    "invalid_arguments": "schema",
    "verification_required": "authorization",
    "unknown_reference": "provenance",
    "precondition_failed": "preconditions",
}
"""Which stage a denial code came from.

Read off the gate's ordering rather than guessed: each check returns exactly one
code, so the mapping is total and a code the gate stops emitting shows up here as
a dead key rather than a wrong picture.
"""


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def gate_pipeline(allowed: bool, code: str | None, surface: Surface = "patient") -> str:
    """The six checks, with the one that stopped the call lit.

    Stages after the failure are drawn hollow rather than omitted: a reviewer
    needs to see that they were *never reached*, which is the difference between
    a call refused early and one refused late.
    """
    failed = STAGE_BY_CODE.get(code or "") if not allowed else None
    stop = STAGES.index(failed) if failed in STAGES else len(STAGES)

    box_w, box_h, gap_w, pad = 92, 26, 12, 4
    width = len(STAGES) * box_w + (len(STAGES) - 1) * gap_w + pad * 2
    height = box_h + 26

    label = "allowed" if allowed else f"stopped at {failed or 'an unnamed stage'}"
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Gate pipeline: {_escape(label)}" '
        f'style="max-width:100%;height:auto;font-family:inherit">'
    ]

    for index, stage in enumerate(STAGES):
        x = pad + index * (box_w + gap_w)
        if not allowed and index == stop:
            fill, stroke, ink, weight = DENY, DENY, "#ffffff", "600"
        elif index < stop:
            fill, stroke, ink, weight = "none", ALLOW, ALLOW, "500"
        else:
            fill, stroke, ink, weight = "none", THEME_BORDER, MUTED, "400"

        parts.append(
            f'<rect x="{x}" y="2" width="{box_w}" height="{box_h}" rx="4" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
            f'<text x="{x + box_w / 2}" y="{2 + box_h / 2 + 4}" '
            f'text-anchor="middle" font-size="11" font-weight="{weight}" '
            f'fill="{ink}">{stage}</text>'
        )
        # A passed stage carries the glyph too, so the row is not colour alone.
        if index < stop and not (not allowed and index == stop):
            parts.append(
                f'<text x="{x + box_w - 9}" y="{2 + box_h - 17}" font-size="9" '
                f'fill="{ALLOW}">✓</text>'
            )
        if index < len(STAGES) - 1:
            arrow_x = x + box_w
            colour = ALLOW if index < stop else THEME_BORDER
            parts.append(
                f'<line x1="{arrow_x + 1}" y1="{2 + box_h / 2}" '
                f'x2="{arrow_x + gap_w - 3}" y2="{2 + box_h / 2}" '
                f'stroke="{colour}" stroke-width="1.5"/>'
            )

    caption_colour = ALLOW if allowed else DENY
    parts.append(
        f'<text x="{pad}" y="{height - 5}" font-size="10.5" '
        f'fill="{caption_colour}" font-weight="600">{_escape(label)}</text>'
    )
    if not allowed and stop < len(STAGES) - 1:
        parts.append(
            f'<text x="{width - pad}" y="{height - 5}" text-anchor="end" '
            f'font-size="10" fill="{MUTED}">'
            f"{len(STAGES) - stop - 1} later check(s) never ran</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


TIERS: tuple[str, ...] = ("patient_safe", "routing_only", "clinician_only")


def tier_bands(queried: list[str], permitted: list[str], surface: Surface = "patient") -> str:
    """Three bands: queried, permitted-but-unqueried, and locked.

    §1.3 — the filter is built from the session role *before* the query, so a
    restricted vector is never a candidate. A padlock on a band says that; a
    sentence about metadata filters does not.
    """
    p = PALETTES[surface]
    row_h, gap_h, label_w, bar_w, pad = 22, 6, 104, 150, 4
    height = len(TIERS) * (row_h + gap_h) + pad
    width = label_w + bar_w + 78

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Retrieval tiers: queried {_escape(", ".join(queried) or "none")}" '
        f'style="max-width:100%;height:auto;font-family:inherit">'
    ]
    for index, tier in enumerate(TIERS):
        y = pad + index * (row_h + gap_h)
        if tier in queried:
            fill, stroke, note, note_colour = p.identity, p.identity, "queried", p.identity
        elif tier in permitted:
            fill, stroke, note, note_colour = "none", THEME_BORDER, "available", MUTED
        else:
            fill, stroke, note, note_colour = "none", DENY, "locked ✕", DENY

        parts.append(
            f'<text x="0" y="{y + row_h / 2 + 4}" font-size="11" '
            f'fill="{THEME_INK}">{tier}</text>'
            f'<rect x="{label_w}" y="{y}" width="{bar_w}" height="{row_h}" rx="3" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.5" '
            f'stroke-dasharray="{"3 3" if tier not in permitted else "none"}"/>'
            f'<text x="{label_w + bar_w + 8}" y="{y + row_h / 2 + 4}" font-size="10" '
            f'font-weight="600" fill="{note_colour}">{note}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


LEDGER_LABELS: dict[str, str] = {
    "patient_ids": "patient records",
    "appointment_ids": "appointments",
    "slot_ids": "slots offered",
}


def provenance_ledger(ledger: dict[str, list[str]], surface: Surface = "patient") -> str:
    """What the conversation has been *handed*, and may therefore refer to.

    The rule this draws is the one nobody can see: an identifier may only be
    passed into a function if a previous result produced it. Until something is
    refused, that rule is invisible — so the ledger is shown accumulating, and
    then `unknown_reference` explains itself.

    Every value here is a clinic-issued reference rather than a fact about a
    person, which is why it is safe to draw. It is the same set
    ``SAFE_REFERENCE_FIELDS`` exempts from the redactor.
    """
    p = PALETTES[surface]
    rows = [(LEDGER_LABELS[key], ledger.get(key) or []) for key in LEDGER_LABELS]
    row_h, gap_h, label_w, pad = 20, 5, 116, 4
    height = len(rows) * (row_h + gap_h) + pad + 2
    width = 330

    total = sum(len(values) for _, values in rows)
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Provenance ledger: {total} reference(s) handed out" '
        f'style="max-width:100%;height:auto;font-family:inherit">'
    ]
    for index, (label, values) in enumerate(rows):
        y = pad + index * (row_h + gap_h)
        parts.append(
            f'<text x="0" y="{y + row_h / 2 + 4}" font-size="11" fill="{MUTED}">{label}</text>'
        )
        if not values:
            parts.append(
                f'<text x="{label_w}" y="{y + row_h / 2 + 4}" font-size="10.5" '
                f'fill="{MUTED}">— none yet</text>'
            )
            continue
        # Chips, so the count is visible without reading the values.
        x = label_w
        for value in values[:4]:
            chip_w = 8 + len(value) * 6.2
            parts.append(
                f'<rect x="{x}" y="{y + 2}" width="{chip_w:.0f}" height="{row_h - 4}" '
                f'rx="3" fill="{THEME_BORDER}" stroke="{p.identity}" stroke-width="1"/>'
                f'<text x="{x + chip_w / 2:.0f}" y="{y + row_h / 2 + 3.5}" '
                f'text-anchor="middle" font-size="9.5" fill="{THEME_INK}">'
                f"{_escape(value)}</text>"
            )
            x += chip_w + 5
        if len(values) > 4:
            parts.append(
                f'<text x="{x}" y="{y + row_h / 2 + 4}" font-size="10" '
                f'fill="{MUTED}">+{len(values) - 4} more</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


LADDER: tuple[str, ...] = ("open", "identified", "verified")


def verification_ladder(actual: str, surface: Surface = "patient") -> str:
    """Where this conversation stands on the §3 ladder, always on screen.

    It used to be two `st.metric`s inside whichever expander happened to be open
    — state expressed as an event. A conversation has one position on the ladder
    at a time and it belongs somewhere ambient.
    """
    p = PALETTES[surface]
    reached = LADDER.index(actual) if actual in LADDER else -1
    step_h, pad, dot_x = 24, 4, 8
    height = len(LADDER) * step_h + pad
    width = 200

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Verification ladder: {_escape(actual)}" '
        f'style="max-width:100%;height:auto;font-family:inherit">'
        f'<line x1="{dot_x}" y1="{pad + 8}" x2="{dot_x}" '
        f'y2="{height - step_h + 8}" stroke="{THEME_BORDER}" stroke-width="2"/>'
    ]
    for index, rung in enumerate(LADDER):
        y = pad + index * step_h + 8
        if index <= reached:
            parts.append(
                f'<circle cx="{dot_x}" cy="{y}" r="5" fill="{p.identity}"/>'
                f'<text x="{dot_x + 14}" y="{y + 4}" font-size="11.5" '
                f'font-weight="600" fill="{THEME_INK}">{rung}</text>'
            )
        else:
            parts.append(
                f'<circle cx="{dot_x}" cy="{y}" r="5" fill="none" '
                f'stroke="{THEME_BORDER}" stroke-width="2"/>'
                f'<text x="{dot_x + 14}" y="{y + 4}" font-size="11.5" '
                f'fill="{MUTED}">{rung}</text>'
            )
        if index == reached:
            parts.append(
                f'<text x="{width - 4}" y="{y + 4}" text-anchor="end" '
                f'font-size="10" font-weight="600" fill="{p.identity}">now here</text>'
            )
    parts.append("</svg>")
    return "".join(parts)
