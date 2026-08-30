"""Chain verifier — `uv run verify-audit` (P6-T3).

Walks an audit file and checks two things at every record: that its own hash
matches its contents, and that its ``prev_hash`` matches the record before it.
Either check failing localises the tampering to a line number.

The verifier runs at the end of every eval in Phase 8, so a change that starts
writing an unverifiable log is caught by the suite rather than by an auditor.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.policy.redaction import SAFE_REFERENCE_FIELDS, contains_protected_data
from app.store.audit import GENESIS_HASH, AuditRecord

SCAN_EXEMPT = frozenset(
    {
        # Hex digests. They would trip the ZIP pattern on any run of five digits
        # and carry nothing about a person.
        "hash",
        "prev_hash",
        "event_id",
        # Clinic-issued references only — `extract_refs` filters to
        # SAFE_REFERENCE_FIELDS, so nothing here is a fact about a patient. They
        # are exempt because slot ids embed a calendar date
        # (SL-2026-09-07-1-1), which is indistinguishable from a date of birth
        # to a pattern scan and is not one.
        "refs",
    }
)


@dataclass
class Problem:
    line: int
    event_id: str
    kind: str
    detail: str


@dataclass
class VerificationReport:
    path: Path
    records: int = 0
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def render(self) -> str:
        head = f"{self.path}: {self.records} record(s)"
        if self.ok:
            return f"{head} — chain intact"
        lines = [f"{head} — {len(self.problems)} problem(s)"]
        lines.extend(
            f"  line {p.line} [{p.kind}] {p.event_id[:8]}: {p.detail}" for p in self.problems
        )
        return "\n".join(lines)


def verify_file(path: Path, check_pii: bool = True) -> VerificationReport:
    report = VerificationReport(path=path)
    if not path.exists():
        report.problems.append(Problem(0, "-", "missing", f"no such file: {path}"))
        return report

    expected_prev = GENESIS_HASH
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        report.records += 1
        try:
            record = AuditRecord.model_validate_json(line)
        except Exception as exc:  # noqa: BLE001 - any malformed line is a problem
            report.problems.append(Problem(number, "-", "malformed", str(exc)[:120]))
            # The chain cannot continue past a line that will not parse.
            break

        if record.prev_hash != expected_prev:
            report.problems.append(
                Problem(
                    number,
                    record.event_id,
                    "broken_link",
                    f"prev_hash {record.prev_hash[:12]} does not follow {expected_prev[:12]}",
                )
            )
        recomputed = record.compute_hash()
        if recomputed != record.hash:
            report.problems.append(
                Problem(
                    number,
                    record.event_id,
                    "altered",
                    f"contents hash to {recomputed[:12]}, record claims {record.hash[:12]}",
                )
            )
        if check_pii:
            leak = _pii_leak(line)
            if leak:
                report.problems.append(Problem(number, record.event_id, "pii", leak))

        expected_prev = record.hash

    return report


def _pii_leak(line: str) -> str | None:
    """A second, independent check that no protected value reached the log.

    The writer redacts on the way in; this asserts it on the way out. Two
    mechanisms, because a redaction gap is silent by nature.

    Only *string* values are scanned. Serialising the whole record to JSON and
    matching against that conflates types: a latency of 12170 ms is five digits
    and is not a ZIP code, and an alarm that cries wolf on every slow turn is an
    alarm nobody reads.
    """
    payload = json.loads(line)
    for field_name, value in payload.items():
        # SAFE_REFERENCE_FIELDS is checked here as well as inside _strings:
        # session_id sits at the top level, and a random hex id occasionally
        # contains five consecutive digits, which reads as a ZIP to the sweep.
        if field_name in SCAN_EXEMPT or field_name in SAFE_REFERENCE_FIELDS:
            continue
        for text in _strings(value):
            if contains_protected_data(text):
                return f"protected data in field {field_name!r}"
    return None


def _strings(node: Any) -> Iterator[str]:
    """Every string leaf, skipping clinic-issued references wherever they sit.

    A reference is exempt because of what it *is*, not where it appears: an
    appointment id (AP-77301) contains five digits and a slot id
    (SL-2026-09-07-1-1) contains a date, and neither is a fact about a person.
    Exempting only the top-level ``refs`` block left the same identifiers
    tripping the scan inside ``args``.
    """
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, item in node.items():
            if key in SAFE_REFERENCE_FIELDS:
                continue
            yield from _strings(item)
    elif isinstance(node, list | tuple):
        for item in node:
            yield from _strings(item)


def verify_directory(directory: Path, check_pii: bool = True) -> list[VerificationReport]:
    return [verify_file(path, check_pii) for path in sorted(directory.glob("audit-*.jsonl"))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the audit chain.")
    parser.add_argument(
        "target",
        nargs="?",
        default="audit",
        help="an audit .jsonl file, or a directory of them (default: audit/)",
    )
    parser.add_argument(
        "--no-pii-scan",
        action="store_true",
        help="skip the protected-data scan and check only chain integrity",
    )
    args = parser.parse_args(argv)

    target = Path(args.target)
    reports = (
        [verify_file(target, not args.no_pii_scan)]
        if target.is_file()
        else verify_directory(target, not args.no_pii_scan)
    )

    if not reports:
        print(f"no audit files found in {target}")
        return 0

    for report in reports:
        print(report.render())
    return 0 if all(report.ok for report in reports) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
