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


@dataclass(frozen=True)
class Palette:
    """Ground, ink and identity for one surface."""

    ground: str
    ink: str
    quiet: str
    """Captions and secondary text. Still AA against the ground."""
    panel: str
    """Cards and chat bubbles, a step off the ground."""
    edge: str
    identity: str
    """The accent that says which surface this is."""
    identity_ink: str


PATIENT = Palette(
    ground="#fbfcfd",
    ink="#1a2328",
    quiet="#54666e",
    panel="#ffffff",
    edge="#dbe3e7",
    identity="#2c4a5e",
    identity_ink="#eef4f7",
)

CLINICAL = Palette(
    ground="#f1f6f7",
    ink="#14282e",
    quiet="#41626a",
    panel="#ffffff",
    edge="#cfe0e3",
    identity="#0f4c52",
    identity_ink="#eaf4f4",
)

PALETTES: dict[Surface, Palette] = {"patient": PATIENT, "clinical": CLINICAL}

# --- semantic colour, identical on both surfaces --------------------------
#
# Chosen so the pair also separates by *lightness*: the deny rust is markedly
# darker than the allow teal, so the two remain distinguishable in grayscale
# even before the glyph and the label are read.
ALLOW = "#0f6a58"
DENY = "#9e3520"
EMERGENCY = "#8a1c1c"
NOTICE = "#1f4e79"
MUTED = "#6b7f86"

ALLOW_GLYPH = "✓"
DENY_GLYPH = "✕"
"""Typographic marks rather than emoji.

An emoji renders in its own colours, at its own weight, differently per platform,
and reads as decoration. These inherit the text colour, so they participate in
the palette instead of fighting it — and a clinical tool that speaks in emoji
reads like a toy.
"""


def stylesheet(surface: Surface) -> str:
    """The one stylesheet a surface injects.

    Every rule that sets a background sets a colour. The container rules set
    their own ink rather than delegating to a descendant selector, because a
    descendant selector misses whatever the framework renders directly into the
    container — which is the specific way this broke before.
    """
    p = PALETTES[surface]
    return f"""
<style>
  .stApp {{ background-color: {p.ground}; color: {p.ink}; }}

  /* Streamlit colours several of these itself, so inheritance is not enough. */
  .stApp, .stApp p, .stApp li, .stApp label, .stApp span,
  .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5,
  .stApp [data-testid="stMarkdownContainer"] {{ color: {p.ink}; }}

  .stApp [data-testid="stCaptionContainer"],
  .stApp [data-testid="stCaptionContainer"] * {{ color: {p.quiet}; }}

  [data-testid="stSidebar"] {{ background-color: {p.edge}; color: {p.ink}; }}
  [data-testid="stSidebar"] * {{ color: {p.ink}; }}
  [data-testid="stHeader"] {{ background-color: {p.ground}; color: {p.ink}; }}

  .stApp code, .stApp pre {{
    background-color: {p.edge}; color: {p.ink};
  }}
  [data-testid="stChatMessage"] {{
    background-color: {p.panel}; color: {p.ink}; border: 1px solid {p.edge};
  }}
  .stApp input, .stApp textarea {{
    background-color: {p.panel}; color: {p.ink};
  }}

  /* The masthead. What makes a surface recognisable in a screenshot. */
  .ds-band {{
    background-color: {p.identity}; color: {p.identity_ink};
    padding: 0.7rem 1rem; border-radius: 4px; margin-bottom: 1rem;
    font-size: 0.92rem; line-height: 1.45;
  }}
  .ds-band strong {{ color: #ffffff; }}

  /* A verdict: glyph, word and hue together, never hue alone. */
  .ds-verdict {{
    display: inline-flex; align-items: baseline; gap: 0.35rem;
    font-weight: 600; font-size: 0.86rem; letter-spacing: 0.02em;
  }}
  .ds-allow {{ color: {ALLOW}; }}
  .ds-deny {{ color: {DENY}; }}

  /* A citation, so a clinician can see at a glance that one exists. */
  .ds-cite {{
    display: inline-block; background-color: {p.edge}; color: {p.ink};
    border-radius: 3px; padding: 0.05rem 0.4rem;
    font-size: 0.76rem; font-variant-numeric: tabular-nums;
  }}

  /* A field the source documents leave empty. Hatched rather than blank, so an
     absence reads as a recorded absence instead of a rendering slip. */
  .ds-gap {{
    display: inline-block; color: {DENY}; font-size: 0.8rem; font-weight: 600;
    background-image: repeating-linear-gradient(
      135deg, {p.edge} 0 4px, transparent 4px 8px
    );
    padding: 0.1rem 0.45rem; border: 1px solid {DENY}; border-radius: 3px;
  }}

  .ds-row {{
    display: flex; align-items: center; gap: 0.6rem;
    padding: 0.35rem 0; border-bottom: 1px solid {p.edge};
  }}
  .ds-card {{
    background-color: {p.panel}; color: {p.ink};
    border: 1px solid {p.edge}; border-left: 3px solid {p.identity};
    border-radius: 4px; padding: 0.6rem 0.8rem; margin-bottom: 0.5rem;
  }}
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
        f'background:{p.edge};border-radius:4px;overflow:hidden">'
        f'<span style="display:block;width:{filled}px;height:7px;'
        f'background:{p.identity}"></span></span>'
        f'<span class="ds-num" style="font-size:0.76rem;color:{p.quiet}">'
        f"{score:.2f}</span></span>"
    )
