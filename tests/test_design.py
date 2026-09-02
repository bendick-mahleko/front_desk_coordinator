"""The design system: the theme, the stylesheet's scope, and contrast.

This file exists because the same mistake was made twice, in opposite
directions, and neither time did a test catch it.

First the clinical page painted a near-black ground and declared no text colour,
so Streamlit went on painting its own dark text onto it. Then the fix painted a
light ground and *forced* dark ink, so a viewer on Streamlit's dark theme got
dark button labels on Streamlit's own dark buttons — the "Start a new
conversation" button was invisible until hover repainted it.

Both are one error: a stylesheet setting *some* of a widget's colours while the
framework's theme sets the rest, where the half that loses is whichever is
applied later. The correction is architectural — the theme owns every surface the
framework draws, and the stylesheet is limited to components this project emits
itself — and the tests below are about holding that line rather than about
colours.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from ui import design

ROOT = Path(__file__).resolve().parent.parent
SURFACES = ["patient", "clinical"]


def css_rules(text: str) -> list[tuple[str, str]]:
    """(selector, body) for each rule in a stylesheet.

    Comments are stripped first. Without that, everything between one rule's
    closing brace and the next rule's opening brace counts as the selector — so
    a documented rule reads as a selector beginning with "/*", and the
    framework-internals test failed on the comments explaining why it exists.
    """
    block = re.search(r"<style>(.*?)</style>", text, re.DOTALL)
    assert block, "no stylesheet found"
    body = re.sub(r"/\*.*?\*/", "", block.group(1), flags=re.DOTALL)
    return [
        (match.group(1).strip(), match.group(2))
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", body)
    ]


def contrast(one: str, two: str) -> float:
    """WCAG relative-contrast ratio."""

    def luminance(hex_colour: str) -> float:
        red, green, blue = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
        channels = [
            value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
            for value in (red, green, blue)
        ]
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    first, second = sorted((luminance(one), luminance(two)), reverse=True)
    return (first + 0.05) / (second + 0.05)


# ----------------------------------------------------------------- the theme ---


def test_the_theme_is_pinned_rather_than_inherited():
    """The fix for "most of the text is not visible".

    An un-pinned theme means the page's colours come from the viewer's browser or
    OS, and a stylesheet written against one of those two possibilities is wrong
    half the time. Pinning it also makes the failure reproducible, which the
    invisible button was not.
    """
    theme = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))[
        "theme"
    ]

    for key in ("base", "backgroundColor", "secondaryBackgroundColor", "textColor"):
        assert key in theme, f"the theme should pin {key}"
    assert theme["base"] == "light", "the component palette assumes a light ground"


def test_the_theme_file_and_the_design_module_agree():
    """Two sources for the same colours, kept honest by a test.

    Streamlit reads the theme at startup, so these cannot be generated from
    Python. They are duplicated deliberately, and this is what stops them
    drifting — which matters, because every component in design.py is drawn *on*
    the ground that file paints.
    """
    theme = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))[
        "theme"
    ]

    assert theme["backgroundColor"] == design.THEME_GROUND
    assert theme["secondaryBackgroundColor"] == design.THEME_SECONDARY
    assert theme["textColor"] == design.THEME_INK
    assert theme["borderColor"] == design.THEME_BORDER


# ------------------------------------------------------ the stylesheet's scope ---


@pytest.mark.parametrize("surface", SURFACES)
def test_the_stylesheet_touches_nothing_the_framework_owns(surface):
    """The correction, as an invariant.

    A rule over a framework internal — `.stApp`, `[data-testid="stSidebar"]`, a
    descendant selector reaching into a widget — competes with the theme for
    colours the theme derives dozens of others from. That produced a near-black
    page with dark text, and then dark labels on dark buttons.
    """
    offenders = [
        selector
        for selector, _ in css_rules(design.stylesheet(surface))
        if not all(part.strip().startswith(".ds-") for part in selector.split(","))
    ]

    assert offenders == [], f"{surface}: rules over framework internals: {offenders}"


@pytest.mark.parametrize("surface", SURFACES)
def test_no_rule_sets_a_ground_without_an_ink(surface):
    """The first failure, still guarded — now only over this project's own
    components, which are the only grounds it paints."""
    offenders = [
        selector
        for selector, body in css_rules(design.stylesheet(surface))
        if "background" in body and "color:" not in body.replace("background-color:", "")
    ]

    assert offenders == [], f"{surface}: a background with no text colour: {offenders}"


def test_neither_page_keeps_its_own_stylesheet():
    """One design language. A page-local <style> block is how the two surfaces
    diverged in the first place."""
    for name in ("app.py", "clinical.py"):
        text = (ROOT / "ui" / name).read_text(encoding="utf-8")
        assert "<style>" not in text, f"{name} should use design.stylesheet()"
        assert "design.stylesheet(" in text, f"{name} should inject the shared stylesheet"


def test_every_styled_class_is_actually_used():
    """The first clinical stylesheet carried .clin-banner and .clin-scope,
    neither of which was ever applied. Dead CSS is where a palette drifts from
    what is on screen."""
    usage = " ".join(
        (ROOT / "ui" / name).read_text(encoding="utf-8")
        for name in ("app.py", "clinical.py", "clinical_render.py", "trace.py", "design.py")
    )
    styled = {
        part.strip().lstrip(".").split()[0]
        for surface in SURFACES
        for selector, _ in css_rules(design.stylesheet(surface))
        for part in selector.split(",")
        if part.strip().startswith(".ds-")
    }

    assert styled
    for classname in sorted(styled):
        assert classname in usage, f"{classname} is styled but never applied"


# --------------------------------------------------------------- legibility ---


@pytest.mark.parametrize("surface", SURFACES)
def test_every_component_is_legible_on_the_themed_ground(surface):
    """Computed rather than eyeballed, because "looks fine on my monitor" is how
    the first palette shipped."""
    p = design.PALETTES[surface]

    assert contrast(design.THEME_INK, design.THEME_GROUND) > 7.0, "body text"
    assert contrast(design.THEME_INK, design.THEME_SECONDARY) > 7.0, "text on a card"
    assert contrast(p.identity_ink, p.identity) > 4.5, "the masthead"
    assert contrast(design.MUTED, design.THEME_GROUND) > 3.0, "diagram labels"

    for semantic in (design.ALLOW, design.DENY, design.EMERGENCY, design.NOTICE):
        assert contrast(semantic, design.THEME_GROUND) > 4.5, f"{semantic} on the ground"
        assert contrast(semantic, design.THEME_SECONDARY) > 4.5, f"{semantic} on a card"


def test_semantic_colour_is_the_same_on_both_surfaces():
    """One language, learned once. Only the identity differs — which is the whole
    point of expressing identity in a component rather than a repainted page."""
    assert design.PATIENT.identity != design.CLINICAL.identity
    assert set(design.Palette.__dataclass_fields__) == {"identity", "identity_ink"}


def test_a_verdict_is_never_colour_alone():
    """Green-versus-red on its own is the commonest accessibility failure in a
    dashboard, and the easiest to avoid."""
    allowed = design.verdict(True)
    denied = design.verdict(False)

    assert design.ALLOW_GLYPH in allowed and "ALLOWED" in allowed
    assert design.DENY_GLYPH in denied and "DENIED" in denied
    assert contrast(design.ALLOW, design.DENY) > 1.5, "and they separate in grayscale"


def test_the_glyphs_are_typographic_not_emoji():
    """An emoji renders in its own colours at its own weight and reads as
    decoration. These inherit the text colour."""
    for glyph in (design.ALLOW_GLYPH, design.DENY_GLYPH):
        assert len(glyph) == 1
        assert not (0x1F300 <= ord(glyph) <= 0x1FAFF), f"{glyph!r} is an emoji"
