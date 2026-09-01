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

Revision 3 adds a third reason. §3.2: *"A Clinical Assistant session is never
established on a patient-facing channel. Channel eligibility is clinic
configuration, not a runtime decision."* So whether a channel is patient-facing
is a property of the channel, checked when a session is established — not a flag
someone can pass in with a request.
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

    patient_facing: bool = True
    """A member of the public may be at the other end.

    The default is the safe one: a channel is assumed to reach a patient unless
    it is declared otherwise, so adding a channel and forgetting this flag
    cannot accidentally open a clinical session to the public (spec §3.2).
    """


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
    capabilities = Capabilities(
        spoken=False, overhearable=False, supports_masking=True, patient_facing=True
    )

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


class ClinicalChannel:
    """The staff-side surface a Clinical Assistant session runs on.

    Not patient-facing, which is the whole point: §3.2 forbids establishing a
    clinical session on a channel a member of the public can reach, and §7.3
    forbids clinician-only material appearing in any patient-visible artifact.
    Keeping them apart at the channel makes the second rule structural rather
    than a matter of care.

    Contact details are still masked here. A clinician reviewing dosage
    reference does not need a patient's phone number read back to them in chat,
    and un-masking would be a widening nobody asked for — the tier is what this
    channel opens up, not the redactor.
    """

    name = "clinical"
    capabilities = Capabilities(
        spoken=False, overhearable=False, supports_masking=True, patient_facing=False
    )

    def render(self, text: str) -> str:
        return mask_contact_details(text)

    def mask_identifier(self, kind: str, value: str) -> str:
        return mask(kind, value)

    def privacy_check_required(self) -> bool:
        return self.capabilities.overhearable


CHANNELS: dict[str, Channel] = {
    TextChannel.name: TextChannel(),
    ClinicalChannel.name: ClinicalChannel(),
}

DEFAULT_CHANNEL = CHANNELS[TextChannel.name]


def channel_for(name: str) -> Channel:
    """Look up a channel by name. Unknown names are an error, not a default.

    Falling back to the text channel would silently make an unrecognised name
    patient-facing, which is the failure this lookup exists to prevent.
    """
    try:
        return CHANNELS[name]
    except KeyError:
        raise ValueError(f"unknown channel {name!r}") from None


def is_patient_facing(name: str) -> bool:
    return channel_for(name).capabilities.patient_facing
