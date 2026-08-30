"""The channel a conversation happens over (AD-08).

Only text is built. The abstraction exists because several specification rules
are *conditional on the channel*, and dropping the condition would quietly drop
the rule:

* §4.2 — "mask sensitive values when repeating them verbally"
* §4.3 — "confirm privacy before reading appointment details if a third party
  may be present"

Neither sentence means anything without knowing whether output is spoken and
whether someone else can hear it. Modelling that now keeps both rules alive and
makes adding voice a new implementation rather than a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.policy.redaction import mask, mask_contact_details


@dataclass(frozen=True)
class Capabilities:
    spoken: bool
    """Output is heard rather than read."""

    overhearable: bool
    """A third party may perceive the output without the patient's knowledge."""

    supports_masking: bool = True


@runtime_checkable
class Channel(Protocol):
    name: str
    capabilities: Capabilities

    def render(self, text: str) -> str: ...

    def mask_identifier(self, kind: str, value: str) -> str: ...

    def privacy_check_required(self) -> bool: ...


class TextChannel:
    """Browser chat. The only channel in the prototype."""

    name = "text"
    capabilities = Capabilities(spoken=False, overhearable=False, supports_masking=True)

    def render(self, text: str) -> str:
        """Everything the patient sees passes through here.

        The model is instructed to mask identifiers it repeats back, and it
        does. This is the layer that holds when it forgets — the same reason the
        gate exists rather than trusting the prompt (AD-01).
        """
        return mask_contact_details(text)

    def mask_identifier(self, kind: str, value: str) -> str:
        """Shorten an identifier the assistant repeats back (spec §4.2).

        Text is read, not heard, but the rule is about *disclosure*, not about
        sound — a shoulder-surfer reads a screen as easily as a bystander hears
        a speaker.
        """
        return mask(kind, value)

    def privacy_check_required(self) -> bool:
        """spec §4.3 — ask before reading appointment details aloud.

        False here: a chat window is not overheard. A voice channel would return
        True and the assistant would confirm before reading anything out.
        """
        return self.capabilities.overhearable


DEFAULT_CHANNEL = TextChannel()
