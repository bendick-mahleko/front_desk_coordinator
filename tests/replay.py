"""P4-T8 — a recorded-transcript backend, so integration tests need no API.

A live model makes integration tests slow, expensive and non-deterministic, and
the thing they are actually testing is the *orchestration*: does the loop bind
the session, does the gate fire, does a denial come back as something the loop
can continue from, does the audit trail record it.

``ScriptedBackend`` replays a fixed sequence of model actions. Everything below
the model is real — the registry, the gate, the ledger, the simulator — so a
scripted turn exercises the same code a live turn does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.orchestrator import ModelTurn, TurnRecorder
from app.safety.prescreen import Label, Screening, keyword_screen
from app.tools import registry


@dataclass
class Call:
    """One tool call the model would have made."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class Say:
    """The assistant's final text for the turn."""

    text: str


@dataclass
class Refuse:
    """A safety refusal from the model."""

    category: str = "unspecified"


Action = Call | Say | Refuse


@dataclass
class ScriptedBackend:
    """Replays scripted turns. One script entry per call to ``run``."""

    script: list[list[Action]]
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 1200,
            "output_tokens": 90,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 1100,
        }
    )
    turns_run: int = 0
    seen_roles: list[Any] = field(default_factory=list)
    """Which principal each turn ran as, so a test can assert §2's tool split
    reached the model call rather than only the registry."""
    seen_system: list[list[dict[str, Any]]] = field(default_factory=list)
    seen_messages: list[list[dict[str, Any]]] = field(default_factory=list)
    results: list[Any] = field(default_factory=list)

    def run(
        self,
        *,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        recorder: TurnRecorder,
        role: Any = None,
    ) -> ModelTurn:
        self.seen_roles.append(role)
        self.seen_system.append(system)
        self.seen_messages.append(messages)

        if self.turns_run >= len(self.script):
            raise AssertionError(
                f"the script has {len(self.script)} turns but turn "
                f"{self.turns_run + 1} was requested"
            )
        actions = self.script[self.turns_run]
        self.turns_run += 1

        # A cached prefix only exists from the second turn onwards, which is
        # exactly what the cache assertion in the tests checks for.
        usage = dict(self.usage)
        if self.turns_run > 1:
            usage["cache_read_input_tokens"] = usage.pop("cache_creation_input_tokens", 0)
            usage["cache_creation_input_tokens"] = 0

        tools = registry.load()
        text = ""
        for action in actions:
            if isinstance(action, Call):
                # Through .call(), so the gate, the ledger and the audit sink all
                # behave exactly as they would under the real runner.
                self.results.append(json.loads(tools[action.name].call(action.args)))
                if recorder.should_break:
                    break
            elif isinstance(action, Refuse):
                return ModelTurn(
                    text="", usage=usage, stop_reason="refusal", refusal=action.category
                )
            else:
                text = action.text

        return ModelTurn(
            text=text,
            tool_calls=list(recorder.tool_calls),
            usage=usage,
            stop_reason="end_turn",
        )


@dataclass
class ScriptedPrescreen:
    """A pre-screen with no model behind it.

    The keyword layer is real — it is deterministic by design — and everything
    it does not settle returns a label the test chose.
    """

    label: Label = Label.ROUTINE
    seen: list[str] = field(default_factory=list)

    def classify(self, text: str) -> Screening:
        self.seen.append(text)
        fast = keyword_screen(text)
        if fast is not None:
            return fast
        return Screening(self.label, source="model")


@dataclass
class ExplodingBackend:
    """Raises, to exercise the orchestrator's failure path."""

    exc: Exception

    def run(self, **_: Any) -> ModelTurn:
        raise self.exc


def booking_script(slot_getter: Any) -> list[list[Action]]:
    """The specification §5 booking sequence, as a one-turn script.

    ``slot_getter`` is called after the search so the chosen slot comes from the
    real search result rather than being hard-coded — a hard-coded slot would
    make the test pass while proving nothing about provenance.
    """
    return [
        [
            Call(
                "check_patient_exists",
                {"first_name": "Amara", "last_name": "Osei", "date_of_birth": "1978-03-04"},
            ),
            Call(
                "verify_patient_identity",
                {
                    "patient_id": "PT-4101",
                    "identifier_1_type": "dob",
                    "identifier_1_value": "1978-03-04",
                    "identifier_2_type": "address_zip",
                    "identifier_2_value": "98101",
                },
            ),
            Say("You're verified — what can I help you with?"),
        ]
    ]
