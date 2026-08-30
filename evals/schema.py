"""The scenario format (P8-T1).

A scenario is a scripted conversation plus assertions about what the *system*
did — not about what it said. Correctness here is mostly ordering and refusal,
and asserting on prose measures the wrong thing: a reply can be word-perfect
while the call behind it was unauthorised, and can be clumsily worded while
every gate decision was right.

So assertions read the audit log. `expect_tools` is an ordering claim,
`expect_gate` is an authorization claim, and `forbid_tools` is the one that
matters most in the adversarial set — proof that something did *not* happen.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

SCENARIO_DIR = Path(__file__).parent / "scenarios"


class Fault(BaseModel):
    """A backend failure the scenario asks for, so the branch is reachable."""

    model_config = ConfigDict(extra="forbid")

    port: str
    operation: str
    code: str
    once: bool = True


class GateExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    function: str
    decision: Literal["allow", "deny"]
    code: str | None = None
    """The denial code, when the decision is deny."""


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["intent", "failure", "adversarial"]
    spec: str = ""
    """The specification clause this scenario exists to prove."""
    description: str = ""

    turns: list[str] = Field(min_length=1)
    inject: list[Fault] = Field(default_factory=list)

    # --- assertions -------------------------------------------------------
    expect_tools: list[str] = Field(default_factory=list)
    """Functions that must be called, in this order. Other calls may occur
    between them — the model is allowed to be thorough, not to skip a step."""

    forbid_tools: list[str] = Field(default_factory=list)
    """Functions that must never be called. The adversarial claim."""

    expect_gate: list[GateExpectation] = Field(default_factory=list)
    expect_status: str | None = None
    expect_escalation_reason: str | None = None
    expect_escalation_priority: str | None = None

    expect_reply_contains: list[str] = Field(default_factory=list)
    forbid_reply_contains: list[str] = Field(default_factory=list)
    """Used sparingly, and only for claims that are genuinely about wording —
    a disclaimer that must be said, a value that must never be printed."""

    expect_no_claim: list[str] = Field(default_factory=list)
    """Words the assistant must not use before the final turn, such as
    claiming an appointment is booked before the call succeeded."""

    judge: str | None = None
    """A rubric question for the LLM judge, when the claim is about tone or
    phrasing and cannot be asserted mechanically."""

    @model_validator(mode="after")
    def _adversarial_must_forbid_something(self) -> Scenario:
        # An adversarial scenario that asserts nothing negative is not
        # adversarial; it is a happy path with a scary name.
        if self.kind == "adversarial" and not (
            self.forbid_tools
            or self.forbid_reply_contains
            or any(e.decision == "deny" for e in self.expect_gate)
        ):
            raise ValueError(
                f"{self.name}: an adversarial scenario must forbid a tool, forbid a "
                "phrase, or expect a denial"
            )
        return self


def load(path: Path) -> Scenario:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Scenario.model_validate(raw)


def load_all(directory: Path | None = None) -> list[Scenario]:
    directory = directory or SCENARIO_DIR
    return [load(path) for path in sorted(directory.glob("*.yaml"))]
