"""One design language for both surfaces.

Before this module the patient app declared no palette and the clinical app
declared its own, so the two shared nothing — and a reviewer moving between them
had to learn the colour language twice. Worse, they had already diverged in a way
that mattered: the clinical page painted a near-black ground and left the text to
Streamlit, which is how it shipped illegible.

Three rules, and they are the whole module:

**Semantic colour is constant across surfaces.** Allow, deny, emergency and
escalation mean the same thing on the front desk as they do in clinical review,
so the language is learned once. Only the *identity* hue differs, which is what
tells a clinician which side of the §3.2 boundary they are on.

**Never colour alone.** Allow and deny carry a glyph, a word and a position as
well as a hue, so the distinction survives colourblindness, a grayscale print and
a projector with the saturation turned down. Green-versus-red on its own is the
most common accessibility failure in a dashboard and the easiest to avoid.

**Every rule that paints a ground declares its ink.** Enforced by a test, because
this module exists partly because that rule was broken once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Surface = Literal["patient", "clinical"]

# --- mirrored from .streamlit/config.toml --------------------------------
#
# Streamlit reads its theme from that file at startup, so these cannot be
# generated from it at runtime. They are duplicated deliberately and a test
# asserts the two agree — change one, change both, or the test says so.
THEME_GROUND = "#fbfcfd"
THEME_SECONDARY = "#eef3f5"
THEME_INK = "#1a2328"
THEME_BORDER = "#d6e0e4"


@dataclass(frozen=True)
class Palette:
    """What differs between the two surfaces — which is only the identity.

    Ground, ink, borders and every widget colour come from the Streamlit theme
    now, so there is one of each rather than one per surface. That is the whole
    correction: identity belongs in a *component* (the masthead, the card rail,
    the accent) and not in a repainted page, because repainting the page means
    fighting the framework for control of colours it derives dozens of others
    from.
    """

    identity: str
    identity_ink: str


PATIENT = Palette(identity="#2c4a5e", identity_ink="#eef4f7")
CLINICAL = Palette(identity="#0f4c52", identity_ink="#eaf4f4")

PALETTES: dict[Surface, Palette] = {"patient": PATIENT, "clinical": CLINICAL}

# --- semantic colour, identical on both surfaces --------------------------
#
# The pair separates by *lightness* as well as hue, so it survives a grayscale
# print and a colourblind reader before the glyph and the label are even read.
#
# Measured, because the first attempt did not. #0f6a58 against #9e3520 looked
# like a dark rust and a mid teal and had a relative contrast of **1.08** — all
# but identical in luminance, so grayscale would have shown two indistinguishable
# greys under a comment claiming otherwise. These separate at 2.17 while both
# still reach AA on the themed ground and on a card.
ALLOW = "#12796a"
DENY = "#6b1f12"
EMERGENCY = "#8a1c1c"
NOTICE = "#1f4e79"
MUTED = "#6b7f86"

QUIET = "#4a5c63"
"""Secondary text, at a weight that actually reaches AA.

Streamlit renders `st.caption` at `opacity: 0.6`, which takes the theme's ink to
roughly 3.4:1 on the sidebar ground — below AA for body text, and the reason the
sidebar was reported as hard to read. There is no theme token for secondary text,
so the opacity is overridden (see FRAMEWORK_EXCEPTIONS) and this colour is used
at full strength instead.
"""

FRAMEWORK_EXCEPTIONS: dict[str, str] = {
    '[data-testid="stCaptionContainer"]': (
        "Streamlit renders captions at opacity 0.6, which is below WCAG AA on "
        "its own ground. There is no theme token for secondary text, so this is "
        "overridden here. Safe in a way the rules that broke twice were not: it "
        "sets opacity and colour on a text-only element with a transparent "
        "background, so there is no ground for the framework and this file to "
        "disagree about."
    ),
}
"""Framework selectors this stylesheet is permitted to touch, and why.

An allowlist rather than a free hand. Every entry is a deliberate exception to
"style nothing the framework owns", each justified in place, and a test asserts
the stylesheet touches nothing outside `.ds-*` and these keys. The rule exists
because ignoring it produced two unreadable pages; the allowlist exists because
one of the framework's defaults is itself inaccessible.
"""

ALLOW_GLYPH = "✓"
DENY_GLYPH = "✕"
"""Typographic marks rather than emoji.

An emoji renders in its own colours, at its own weight, differently per platform,
and reads as decoration. These inherit the text colour, so they participate in
the palette instead of fighting it — and a clinical tool that speaks in emoji
reads like a toy.
"""


def stylesheet(surface: Surface) -> str:
    """The components this project draws itself. Nothing the framework owns.

    Deliberately narrow, after breaking both surfaces by being broad. Streamlit's
    theme (`.streamlit/config.toml`) paints the page, the sidebar, the buttons,
    the inputs and the chat bubbles, and it derives all of those from a few
    values so they stay consistent. A stylesheet cannot reach them reliably: it
    sets some of a widget's colours while the theme sets the rest, and the half
    that loses is whichever the framework applies later.

    Both failures had that shape. First a near-black ground with no ink declared,
    so a light theme painted dark text on it. Then a light ground with forced
    dark ink, so a *dark* theme painted dark button labels on its own dark
    buttons — invisible until hover repainted them.

    So: no `.stApp` rule, no descendant selectors over framework internals, no
    forcing colour onto anything with a background this file did not paint. Every
    rule below styles a `ds-` class that only this project emits, and every one
    that paints a ground declares its ink.
    """
    p = PALETTES[surface]
    return f"""
<style>
  /* The one framework exception, justified in FRAMEWORK_EXCEPTIONS: Streamlit's
     caption opacity is below AA, and no theme token replaces it. Opacity and
     colour on a text-only element — no ground for the theme and this file to
     disagree about. */
  [data-testid="stCaptionContainer"] {{ opacity: 1; color: {QUIET}; }}

  /* The masthead. What makes a surface recognisable in a screenshot, and the
     only place the surface identity is expressed — identity through a component
     rather than by repainting the page, which is what kept going wrong. */
  .ds-band {{
    background-color: {p.identity}; color: {p.identity_ink};
    padding: 0.7rem 1rem; border-radius: 4px; margin-bottom: 1rem;
    font-size: 0.92rem; line-height: 1.45;
  }}
  .ds-band strong {{ color: #ffffff; }}

  /* A section label in the sidebar. Carries the identity so the eye has
     something to anchor on besides grey. */
  .ds-label {{
    color: {p.identity}; font-size: 0.72rem; font-weight: 700;
    letter-spacing: 0.09em; text-transform: uppercase;
    margin: 0.2rem 0 0.35rem;
  }}

  /* Session state as a chip rather than an emoji and a word. */
  .ds-chip {{
    display: inline-block; background-color: {p.identity}; color: {p.identity_ink};
    border-radius: 11px; padding: 0.12rem 0.6rem;
    font-size: 0.78rem; font-weight: 600;
  }}
  .ds-chip-quiet {{
    display: inline-block; background-color: {THEME_SECONDARY}; color: {QUIET};
    border: 1px solid {THEME_BORDER}; border-radius: 11px;
    padding: 0.12rem 0.6rem; font-size: 0.78rem; font-weight: 600;
  }}

  /* A verdict: glyph, word and hue together, never hue alone. */
  .ds-verdict {{
    display: inline-flex; align-items: baseline; gap: 0.35rem;
    font-weight: 600; font-size: 0.86rem; letter-spacing: 0.02em;
  }}
  .ds-allow {{ color: {ALLOW}; }}
  .ds-deny {{ color: {DENY}; }}

  /* A citation, so a clinician can see at a glance that one exists. */
  .ds-cite {{
    display: inline-block; background-color: {THEME_SECONDARY}; color: {THEME_INK};
    border-radius: 3px; padding: 0.05rem 0.4rem;
    font-size: 0.76rem; font-variant-numeric: tabular-nums;
  }}

  /* A field the source documents leave empty. Hatched rather than blank, so an
     absence reads as a recorded absence instead of a rendering slip. */
  .ds-gap {{
    display: inline-block; background-color: {THEME_SECONDARY}; color: {DENY};
    font-size: 0.8rem; font-weight: 600;
    padding: 0.1rem 0.45rem; border: 1px solid {DENY}; border-radius: 3px;
  }}

  .ds-row {{
    display: flex; align-items: baseline; gap: 0.6rem;
    padding: 0.35rem 0; border-bottom: 1px solid {THEME_BORDER};
  }}
  .ds-card {{
    background-color: {THEME_SECONDARY}; color: {THEME_INK};
    border-left: 3px solid {p.identity};
    border-radius: 4px; padding: 0.6rem 0.8rem; margin-bottom: 0.5rem;
  }}

  /* An empty panel that says what will appear in it. Most of this page is this
     panel before the first message, and a single grey line of prose made the
     screen look broken rather than ready. */
  .ds-empty {{
    background-color: {THEME_SECONDARY}; color: {QUIET};
    border: 1px dashed {THEME_BORDER}; border-radius: 6px;
    padding: 1rem 1.1rem; font-size: 0.88rem; line-height: 1.55;
  }}
  .ds-empty strong {{ color: {THEME_INK}; }}
  .ds-num {{ font-variant-numeric: tabular-nums; }}
</style>
"""


def band(text: str) -> str:
    return f'<div class="ds-band">{text}</div>'


def verdict(allowed: bool) -> str:
    """A verdict rendered four ways at once: glyph, word, hue, and weight."""
    if allowed:
        return f'<span class="ds-verdict ds-allow">{ALLOW_GLYPH} ALLOWED</span>'
    return f'<span class="ds-verdict ds-deny">{DENY_GLYPH} DENIED</span>'


def citation(text: str) -> str:
    return f'<span class="ds-cite">{text}</span>'


def gap(text: str) -> str:
    """A field the source leaves empty, drawn as an absence rather than blank."""
    return f'<span class="ds-gap">{text}</span>'


def support_bar(score: float, surface: Surface = "clinical", width: int = 120) -> str:
    """Retrieval support as a bar, with the number beside it.

    A bar because 0.408 next to 0.340 is a comparison a reader has to compute,
    and because §4.15 requires the *ordering* to be legible as strength of
    support rather than likelihood. The number stays, because a clinician
    checking a threshold needs the value.
    """
    p = PALETTES[surface]
    clamped = max(0.0, min(1.0, score))
    filled = int(clamped * width)
    return (
        f'<span style="display:inline-flex;align-items:center;gap:0.45rem">'
        f'<span style="display:inline-block;width:{width}px;height:7px;'
        f'background:{THEME_BORDER};border-radius:4px;overflow:hidden">'
        f'<span style="display:block;width:{filled}px;height:7px;'
        f'background:{p.identity}"></span></span>'
        f'<span class="ds-num" style="font-size:0.76rem;color:{MUTED}">'
        f"{score:.2f}</span></span>"
    )
