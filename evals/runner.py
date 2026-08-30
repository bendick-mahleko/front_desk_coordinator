"""The eval runner (P8-T2, P8-T7).

Drives a scenario through the real orchestrator and asserts against the audit
log rather than the reply text. Every scenario gets its own audit file, and the
chain verifier runs at the end of each one — a change that starts writing an
unverifiable log fails the eval that exposed it, not an auditor months later.

Live by default, because the thing a scenario tests is whether the *model*
routes correctly. `--offline` swaps in a scripted backend so the harness itself
can be exercised in CI without spending anything.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.clinic_sim import ClinicSimulator
from app.config import get_clinic_config, get_settings
from app.orchestrator import Orchestrator
from app.safety.prescreen import Prescreen
from app.store.audit import AuditRecord, AuditWriter, EventKind
from app.store.session import Session
from app.store.verify import verify_file
from evals.judge import Judge
from evals.schema import Scenario, load_all

PINNED_TODAY = date(2026, 9, 7)


@dataclass
class Failure:
    claim: str
    detail: str


@dataclass
class Result:
    scenario: Scenario
    failures: list[Failure] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)
    audit_path: Path | None = None
    cost_tokens: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failures and self.error is None

    def render(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        head = f"[{mark}] {self.scenario.kind:<11} {self.scenario.name}"
        if self.scenario.spec:
            head += f"  ({self.scenario.spec})"
        if self.ok:
            return head
        lines = [head]
        if self.error:
            lines.append(f"         error: {self.error}")
        lines.extend(f"         · {f.claim}: {f.detail}" for f in self.failures)
        lines.append(f"         calls: {' -> '.join(self.tool_calls) or 'none'}")
        return "\n".join(lines)


# --------------------------------------------------------------- assertions ---


def _gate_records(records: Sequence[AuditRecord]) -> list[AuditRecord]:
    return [r for r in records if r.event == EventKind.GATE_DECISION]


def _called(records: Sequence[AuditRecord]) -> list[str]:
    return [r.function or "?" for r in _gate_records(records)]


def _is_subsequence(expected: Sequence[str], actual: Sequence[str]) -> str | None:
    """Ordering claim: every expected call appears, in order.

    A subsequence rather than an exact match, because a model that also checks
    the clinic hours on the way through has not done anything wrong.
    """
    remaining = list(actual)
    for wanted in expected:
        if wanted in remaining:
            remaining = remaining[remaining.index(wanted) + 1 :]
        else:
            return wanted
    return None


def check(scenario: Scenario, records: list[AuditRecord], replies: list[str]) -> list[Failure]:
    failures: list[Failure] = []
    calls = _called(records)
    transcript = "\n".join(replies).lower()

    missing = _is_subsequence(scenario.expect_tools, calls)
    if missing is not None:
        failures.append(
            Failure("expect_tools", f"{missing!r} did not appear in order after the previous call")
        )

    for forbidden in scenario.forbid_tools:
        if forbidden in calls:
            failures.append(Failure("forbid_tools", f"{forbidden!r} was called"))

    for expectation in scenario.expect_gate:
        matching = [
            r
            for r in _gate_records(records)
            if r.function == expectation.function
            and (r.gate or {}).get("decision") == expectation.decision
        ]
        if not matching:
            failures.append(
                Failure(
                    "expect_gate",
                    f"no {expectation.decision} recorded for {expectation.function!r}",
                )
            )
        elif expectation.code and not any(
            (r.gate or {}).get("code") == expectation.code for r in matching
        ):
            got = {(r.gate or {}).get("code") for r in matching}
            failures.append(
                Failure(
                    "expect_gate", f"{expectation.function}: wanted {expectation.code}, got {got}"
                )
            )

    if scenario.expect_escalation_reason or scenario.expect_escalation_priority:
        escalations = [r for r in records if r.event == EventKind.ESCALATION]
        if not escalations:
            failures.append(Failure("expect_escalation", "nothing was escalated"))
        else:
            details = [r.detail or {} for r in escalations]
            if scenario.expect_escalation_reason and not any(
                d.get("reason") == scenario.expect_escalation_reason for d in details
            ):
                failures.append(
                    Failure(
                        "expect_escalation",
                        f"wanted reason {scenario.expect_escalation_reason}, "
                        f"got {[d.get('reason') for d in details]}",
                    )
                )
            if scenario.expect_escalation_priority and not any(
                d.get("priority") == scenario.expect_escalation_priority for d in details
            ):
                failures.append(
                    Failure(
                        "expect_escalation",
                        f"wanted priority {scenario.expect_escalation_priority}",
                    )
                )

    for phrase in scenario.expect_reply_contains:
        if phrase.lower() not in transcript:
            failures.append(Failure("expect_reply_contains", f"{phrase!r} was never said"))

    for phrase in scenario.forbid_reply_contains:
        if phrase.lower() in transcript:
            failures.append(Failure("forbid_reply_contains", f"{phrase!r} was said"))

    # spec §4.6 — never claim a booking before the call succeeds. Checked over
    # every reply except the last, since by then it may legitimately be true.
    early = "\n".join(replies[:-1]).lower()
    for phrase in scenario.expect_no_claim:
        if phrase.lower() in early:
            failures.append(
                Failure("expect_no_claim", f"{phrase!r} was claimed before the result came back")
            )

    return failures


# ------------------------------------------------------------------ running ---


def run_scenario(
    scenario: Scenario,
    audit_dir: Path,
    backend: Any = None,
    prescreen: Prescreen | None = None,
    judge: Judge | None = None,
) -> Result:
    clinic = get_clinic_config()
    sim = ClinicSimulator.build(clinic=clinic, today=PINNED_TODAY)
    for fault in scenario.inject:
        sim.faults.arm(fault.port, fault.operation, fault.code, once=fault.once)

    audit = AuditWriter(directory=audit_dir, day=scenario.name)
    orchestrator = Orchestrator(
        sim=sim,
        clinic=clinic,
        audit=audit,
        backend=backend,
        prescreen=prescreen,
    )

    result = Result(scenario=scenario, audit_path=audit.path)
    session = Session()

    try:
        for turn in scenario.turns:
            outcome = orchestrator.run_turn(session, turn)
            result.replies.append(outcome.reply)
            for key, value in outcome.usage.items():
                result.cost_tokens[key] = result.cost_tokens.get(key, 0) + value
    except Exception as exc:  # noqa: BLE001 - a crash is a scenario failure
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    records = list(audit.records())
    result.tool_calls = _called(records)
    result.failures = check(scenario, records, result.replies)

    if scenario.expect_status and session.status.value != scenario.expect_status:
        result.failures.append(
            Failure(
                "expect_status",
                f"wanted {scenario.expect_status}, ended {session.status.value}",
            )
        )

    # The judge can only add a failure, never remove one: the mechanical
    # assertions decide whether a scenario passes.
    if scenario.judge and judge is not None:
        judgement = judge.assess(scenario.judge, result.replies)
        if judgement.available and not judgement.passed:
            result.failures.append(Failure("judge", judgement.reason))

    # P8-T7 — the chain verifier runs on every scenario, not just at the end.
    report = verify_file(audit.path)
    if not report.ok:
        result.failures.append(Failure("audit_chain", report.render()))

    return result


def run_all(
    scenarios: list[Scenario],
    audit_dir: Path,
    backend_factory: Any = None,
    prescreen: Prescreen | None = None,
    judge: Judge | None = None,
) -> list[Result]:
    results = []
    for scenario in scenarios:
        backend = backend_factory(scenario) if backend_factory else None
        results.append(
            run_scenario(scenario, audit_dir, backend=backend, prescreen=prescreen, judge=judge)
        )
    return results


# ------------------------------------------------------------- the DoD view ---

DOD_CRITERIA: dict[str, tuple[str, str]] = {
    "routing": (
        "Correctly routes each supported intent to the approved function sequence",
        "intent",
    ),
    "verification": (
        "Enforces verification before protected access and appointment changes",
        "adversarial",
    ),
    "validation": ("Validates required parameters and allowed enum values", "adversarial"),
    "coverage": (
        "Safely supports lookup, registration, scheduling, insurance, messaging, "
        "clinic info and escalation",
        "intent",
    ),
    "auditability": (
        "Generates auditable records of verification, calls, results, errors and escalations",
        "*",
    ),
}


def traceability(results: list[Result]) -> str:
    """Design §19 — the definition of done, against what actually ran."""
    lines = ["", "Definition of done (specification §8)", "=" * 72]
    for criterion, kind in DOD_CRITERIA.values():
        relevant = [r for r in results if kind == "*" or r.scenario.kind == kind]
        passed = sum(1 for r in relevant if r.ok)
        if not relevant:
            # Not measured is not the same as failed. A filtered run must not
            # report a criterion red when nothing exercising it was executed.
            mark, evidence = "  -  ", "not exercised by this run"
        else:
            mark = "GREEN" if passed == len(relevant) else "RED  "
            evidence = f"{passed}/{len(relevant)} {kind} scenario(s)"
        lines.append(f"[{mark}] {criterion}")
        lines.append(f"         evidence: {evidence}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the scenario evals.")
    parser.add_argument("--kind", choices=["intent", "failure", "adversarial"], default=None)
    parser.add_argument("--name", default=None, help="run one scenario by name")
    parser.add_argument("--audit-dir", default=None, help="where to write audit files")
    parser.add_argument(
        "--limit", type=int, default=None, help="stop after N scenarios (bounds live spend)"
    )
    parser.add_argument("--no-judge", action="store_true", help="skip the LLM judge")
    args = parser.parse_args(argv)

    scenarios = load_all()
    if args.kind:
        scenarios = [s for s in scenarios if s.kind == args.kind]
    if args.name:
        scenarios = [s for s in scenarios if s.name == args.name]
    if args.limit:
        scenarios = scenarios[: args.limit]

    if not scenarios:
        print("no scenarios matched")
        return 1

    settings = get_settings()
    print(
        f"Running {len(scenarios)} scenario(s) against "
        f"{settings.route_model(settings.agent_model)} ({settings.provider})\n"
    )

    audit_dir = Path(args.audit_dir) if args.audit_dir else Path(tempfile.mkdtemp())
    results = run_all(scenarios, audit_dir, judge=None if args.no_judge else Judge())

    for result in results:
        print(result.render())

    passed = sum(1 for r in results if r.ok)
    print(f"\n{passed}/{len(results)} passed")
    print(traceability(results))

    tokens = sum(r.cost_tokens.get("input_tokens", 0) for r in results)
    output = sum(r.cost_tokens.get("output_tokens", 0) for r in results)
    cached = sum(r.cost_tokens.get("cache_read_input_tokens", 0) for r in results)
    print(f"\ntokens: {tokens} in ({cached} cached), {output} out")
    print(f"audit files: {audit_dir}")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
