"""Emergency and advice screening, ahead of the agent loop (design §16, AD-05).

Specification §7 says emergencies are detected *before* routine scheduling
workflows. A model deep in a booking flow is exactly the situation where it
might not notice, so this runs on the raw turn before the transcript is even
assembled.

Two layers, in this order:

1. **A keyword fast path.** Deterministic, free, instant, and — crucially —
   independent of the model. If the classifier is down, rate limited or
   misconfigured, unambiguous emergency language is still caught.
2. **A Haiku classifier** for everything the keywords do not settle.

The cost is a model call on most turns. That is accepted deliberately: a check
that runs *after* the booking flow has started is not the check §7 asks for.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from app.config import Settings, get_settings

logger = logging.getLogger("frontdesk.safety")

PROMPTS = Path(__file__).parent.parent / "prompts"


class Label(StrEnum):
    EMERGENCY = "emergency"
    CLINICAL_ADVICE = "clinical_advice"
    STAFF_REQUEST = "staff_request"
    ROUTINE = "routine"


# Present-tense, high-confidence phrasing only. The bar for inclusion is that a
# false positive would be *tolerable* — an unnecessary "please call 999" is an
# annoyance, a missed emergency is not — but not so loose that ordinary booking
# conversations get derailed.
EMERGENCY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bchest (?:pain|pressure|tight)",
        r"\b(?:can'?t|cannot|struggling to|difficulty|trouble) breath",
        r"\b(?:can'?t|cannot) stop (?:the )?bleeding\b",
        r"\bbleeding (?:heavily|badly|a lot|everywhere)\b",
        r"\bhaving a (?:heart attack|stroke|seizure)\b",
        r"\bthink (?:i'?m|he'?s|she'?s|they'?re) having a (?:heart attack|stroke)\b",
        r"\b(?:face|arm) (?:is )?drooping\b",
        r"\bslurred speech\b",
        r"\b(?:unconscious|unresponsive|passed out|collapsed)\b",
        r"\b(?:overdose|overdosed)\b",
        r"\b(?:kill myself|end my life|suicidal|take my own life)\b",
        r"\bwant to (?:die|hurt myself)\b",
        r"\banaphylaxis\b",
        r"\bthroat (?:is )?closing\b",
        r"\bsevere allergic reaction\b",
        r"\bnot breathing\b",
        r"\bturning blue\b",
    )
)

# Phrases that place a symptom in the past or under control. Without these, "the
# chest pain I had last year" routes a routine booking to the emergency path and
# the assistant becomes unusable for anyone with a cardiac history.
PAST_TENSE_GUARDS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:had|have had|used to have)\b.{0,40}"
        r"\b(?:last|previous|back in|a while|years?|months?) \b",
        r"\bno longer\b",
        r"\bunder control\b",
        r"\bcleared up\b",
        r"\bfollow[- ]?up (?:for|about|on)\b",
        r"\bhistory of\b",
        r"\b(?:don'?t|do not|didn'?t|not) (?:have|having)\b",
    )
)


@dataclass(frozen=True)
class Screening:
    label: Label
    source: Literal["keyword", "retrieval", "model", "fallback", "skipped"]
    """How the label was arrived at.

    ``skipped`` means no screen ran, which happens in a clinical session: §7.1's
    rules are all about a patient describing their own symptoms, and §7.2
    replaces them for a clinician. Recorded as a source rather than by omitting
    the record, so the audit log shows a decision instead of a gap.
    """

    matched: str | None = None

    @property
    def is_emergency(self) -> bool:
        return self.label is Label.EMERGENCY


def keyword_screen(text: str) -> Screening | None:
    """Deterministic emergency detection. None means "not settled here"."""
    if any(guard.search(text) for guard in PAST_TENSE_GUARDS):
        return None
    for pattern in EMERGENCY_PATTERNS:
        match = pattern.search(text)
        if match:
            return Screening(Label.EMERGENCY, source="keyword", matched=match.group(0))
    return None


class Prescreen:
    """Classifies one inbound turn before the agent loop runs."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: Any = None,
        knowledge: Any = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self._knowledge = knowledge
        self._prompt = (PROMPTS / "classifier.md").read_text(encoding="utf-8")

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(**self._settings.client_kwargs())
        return self._client

    def classify(self, text: str) -> Screening:
        fast = keyword_screen(text)
        if fast is not None:
            logger.info("prescreen: emergency matched by keyword")
            return fast

        retrieved = self._red_flag_screen(text)
        if retrieved is not None:
            return retrieved

        try:
            return Screening(self._ask_model(text), source="model")
        except Exception as exc:  # noqa: BLE001 - never block a turn on this
            # Falling back to routine is safe *because* the keyword layer above
            # does not depend on the model: unambiguous emergency language has
            # already been caught. What is lost is the borderline case, and
            # blocking every conversation on a classifier outage would be worse.
            logger.warning("prescreen classifier unavailable (%s)", type(exc).__name__)
            return Screening(Label.ROUTINE, source="fallback")

    def _red_flag_screen(self, text: str) -> Screening | None:
        """Layer two (R2): does the description resemble a red-flag condition?

        Catches presentations the keyword list cannot enumerate — a description
        of stroke symptoms in the patient's own words rather than the word
        "stroke". The output is a label; no retrieved text is returned or shown.

        Retrieval failure is never fatal. The classifier still runs, and the
        keyword layer has already run, so a broken index degrades this to the
        two-layer screen it was before rather than taking the turn down.
        """
        if self._knowledge is None:
            return None
        try:
            from app.knowledge.chunking import Tier, require_tiers
            from app.knowledge.red_flags import RED_FLAG_MIN_SCORE, is_emergency
            from app.policy.decorator import current_session

            # Through the chokepoint like every other retrieval (§1.3). The
            # prescreen runs inside the session scope, but defensively: it is
            # also constructed standalone in tests, and a screen that raised
            # because no session was bound would fail *open* on the safety
            # layer, which is the wrong direction to fail.
            try:
                role = current_session().effective_role
            except Exception:  # noqa: BLE001
                from app.store.session import Role

                role = Role.PATIENT

            tiers = require_tiers([Tier.ROUTING_ONLY], role)
            hits = self._knowledge.search(text, tiers=tiers, k=3, min_score=RED_FLAG_MIN_SCORE)
        except Exception as exc:  # noqa: BLE001
            logger.warning("red-flag retrieval unavailable (%s)", type(exc).__name__)
            return None

        for hit in hits:
            if is_emergency(hit.disease):
                logger.info("prescreen: emergency matched by retrieval")
                # The disease name goes into the trace for a reviewer, never
                # into a patient-facing reply.
                return Screening(Label.EMERGENCY, source="retrieval", matched=hit.disease)
        return None

    def _ask_model(self, text: str) -> Label:
        response = self.client.messages.create(
            model=self._settings.route_model(self._settings.classifier_model),
            # Eight truncated a reply like "The label is clinical_advice"
            # mid-word, which then matched nothing and fell back to routine.
            max_tokens=24,
            system=self._prompt,
            messages=[{"role": "user", "content": text}],
        )
        raw = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        return _parse_label(raw)


def _parse_label(raw: str) -> Label:
    """Map the reply onto a label, erring towards caution.

    A classifier that answers with a sentence instead of a word must not silently
    become "routine", so the emergency token is looked for anywhere in the reply
    before falling back.
    """
    cleaned = raw.strip().strip(".").lower()
    if Label.EMERGENCY.value in cleaned:
        return Label.EMERGENCY
    for label in (Label.CLINICAL_ADVICE, Label.STAFF_REQUEST, Label.ROUTINE):
        if label.value in cleaned:
            return label
    logger.warning("prescreen classifier returned an unrecognised label")
    return Label.ROUTINE
