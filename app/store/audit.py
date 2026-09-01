"""The audit log — append-only, hash-chained (design §15, AD-06).

Specification §8 asks for auditable records of verification, function calls,
results, errors and escalation outcomes. "Auditable" implies *detectable
tampering*: a log anyone can quietly edit evidences nothing. Each record carries
the SHA-256 of the record before it, so removing or altering one breaks every
hash after it and the verifier says exactly where.

What is written is as important as that it is written. The log records that a
demographics call happened, for which patient reference, with what outcome — it
never records the demographics. Specification §4.2 permits the verification
result, the timestamp and the method; this holds itself to that everywhere.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.policy.redaction import SAFE_REFERENCE_FIELDS, redact_args, redact_text

GENESIS_HASH = "0" * 64


class EventKind:
    """The event vocabulary. Strings rather than an enum so a future kind added
    by a later phase does not invalidate an existing chain."""

    TURN_STARTED = "turn_started"
    PRESCREEN = "prescreen"
    GATE_DECISION = "gate_decision"
    TOOL_RESULT = "tool_result"
    VERIFICATION = "verification"
    ESCALATION = "escalation"
    CLINICAL_AUTH = "clinical_auth"
    REFUSAL = "refusal"
    MODEL_ERROR = "model_error"
    TURN_COMPLETED = "turn_completed"


class AuditRecord(BaseModel):
    """One line of the log."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    ts: str
    session_id: str
    turn: int
    event: str

    function: str | None = None
    args: dict[str, Any] | None = None
    gate: dict[str, Any] | None = None
    outcome: str | None = None
    latency_ms: int | None = None
    refs: dict[str, str] = Field(default_factory=dict)
    detail: dict[str, Any] | None = None
    error: str | None = None

    prev_hash: str
    hash: str = ""

    def payload(self) -> dict[str, Any]:
        """Everything the hash covers — the record minus the hash itself."""
        data = self.model_dump(exclude={"hash"}, exclude_none=True)
        return data

    def compute_hash(self) -> str:
        return _digest(self.payload())


def _digest(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def extract_refs(value: Any) -> dict[str, str]:
    """Pull the clinic-issued identifiers out of a result.

    These are references the clinic minted rather than facts about a person, so
    they are safe to log — and the log is useless for tracing a complaint
    without them.
    """
    refs: dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, BaseModel):
            walk(node.model_dump())
        elif isinstance(node, dict):
            for key, item in node.items():
                if key in SAFE_REFERENCE_FIELDS and isinstance(item, str) and item:
                    refs.setdefault(key, item)
                else:
                    walk(item)
        elif isinstance(node, list | tuple):
            for item in node:
                walk(item)

    walk(value)
    return refs


def summarise_outcome(result: Any) -> str:
    """A one-word outcome. Never the payload.

    Specification §15 forbids logging demographic payloads, message bodies and
    symptom text, so a tool result contributes its shape and not its content.
    """
    if isinstance(result, dict):
        if "error" in result:
            return str(result["error"])
        return "ok"
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            return "ok"
        return summarise_outcome(parsed)
    return "ok"


class AuditWriter:
    """Appends hash-chained records to a JSONL file.

    One file per day. The writer reads the tail of an existing file on start so
    a restart continues the chain rather than beginning a second one.
    """

    def __init__(self, directory: Path | str = "audit", day: str | None = None) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._day = day or datetime.now(UTC).strftime("%Y-%m-%d")
        self._path = self._dir / f"audit-{self._day}.jsonl"
        self._lock = threading.Lock()
        self._last_hash = self._read_last_hash()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def last_hash(self) -> str:
        return self._last_hash

    def _read_last_hash(self) -> str:
        if not self._path.exists():
            return GENESIS_HASH
        last = GENESIS_HASH
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                last = json.loads(line).get("hash", last)
        return last

    def append(
        self,
        *,
        session_id: str,
        turn: int,
        event: str,
        **fields: Any,
    ) -> AuditRecord:
        with self._lock:
            record = AuditRecord(
                event_id=uuid.uuid4().hex,
                ts=datetime.now(UTC).isoformat(),
                session_id=session_id,
                turn=turn,
                event=event,
                prev_hash=self._last_hash,
                **fields,
            )
            record.hash = record.compute_hash()
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(record.model_dump_json(exclude_none=True) + "\n")
            self._last_hash = record.hash
            return record

    def records(self) -> Iterator[AuditRecord]:
        if not self._path.exists():
            return
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield AuditRecord.model_validate_json(line)

    # ------------------------------------------------------ typed events ---

    def turn_started(self, session_id: str, turn: int) -> AuditRecord:
        return self.append(session_id=session_id, turn=turn, event=EventKind.TURN_STARTED)

    def prescreen(self, session_id: str, turn: int, detail: dict[str, Any]) -> AuditRecord:
        return self.append(
            session_id=session_id,
            turn=turn,
            event=EventKind.PRESCREEN,
            detail=detail,
            outcome=str(detail.get("label")),
        )

    def gate_decision(
        self,
        session_id: str,
        turn: int,
        function: str,
        args: dict[str, Any],
        gate: dict[str, Any],
        latency_ms: int | None = None,
    ) -> AuditRecord:
        return self.append(
            session_id=session_id,
            turn=turn,
            event=EventKind.GATE_DECISION,
            function=function,
            # The log-safe view: field-aware redaction, then a pattern sweep.
            args=redact_args(args),
            gate=gate,
            outcome="allowed" if gate.get("decision") == "allow" else "denied",
            latency_ms=latency_ms,
            refs=extract_refs(args),
        )

    def tool_result(
        self, session_id: str, turn: int, function: str, result: Any, latency_ms: int | None = None
    ) -> AuditRecord:
        return self.append(
            session_id=session_id,
            turn=turn,
            event=EventKind.TOOL_RESULT,
            function=function,
            outcome=summarise_outcome(result),
            latency_ms=latency_ms,
            refs=extract_refs(result),
        )

    def verification(
        self, session_id: str, turn: int, detail: dict[str, Any], patient_id: str | None = None
    ) -> AuditRecord:
        """spec §4.2 — the result, the timestamp and the method. Nothing else."""
        return self.append(
            session_id=session_id,
            turn=turn,
            event=EventKind.VERIFICATION,
            outcome="verified" if detail.get("verified") else "failed",
            detail=detail,
            refs={"patient_id": patient_id} if patient_id else {},
        )

    def escalation(
        self, session_id: str, turn: int, detail: dict[str, Any], ticket_id: str
    ) -> AuditRecord:
        return self.append(
            session_id=session_id,
            turn=turn,
            event=EventKind.ESCALATION,
            outcome=str(detail.get("priority", "routine")),
            detail=detail,
            refs={"ticket_id": ticket_id},
        )

    def clinical_auth(
        self, session_id: str, turn: int, detail: dict[str, Any], outcome: str
    ) -> AuditRecord:
        """spec §4.13 — *"Record authentication outcome, staff identifier,
        asserted role, timestamp, and channel in the audit log."*

        The timestamp is the record's own ``ts``. The staff identifier goes in
        ``refs`` rather than ``detail`` so it survives redaction: it is a
        clinic-issued reference to an employee, which §3.2's last bullet requires
        for every clinical call to be *"auditable to a named individual"* — an
        audit log that redacted it could not do the one job §3.2 gives it.

        No credential material reaches here. ``detail`` is built by the caller
        from the assertion, which has no field that could carry a token.
        """
        return self.append(
            session_id=session_id,
            turn=turn,
            event=EventKind.CLINICAL_AUTH,
            outcome=outcome,
            detail=detail,
            refs={"staff_id": detail.get("staff_id", "")} if detail.get("staff_id") else {},
        )

    def refusal(self, session_id: str, turn: int, category: str) -> AuditRecord:
        return self.append(
            session_id=session_id,
            turn=turn,
            event=EventKind.REFUSAL,
            outcome="refused",
            detail={"category": redact_text(category)},
        )

    def model_error(self, session_id: str, turn: int, error: str) -> AuditRecord:
        return self.append(
            session_id=session_id, turn=turn, event=EventKind.MODEL_ERROR, error=error
        )

    def turn_completed(
        self, session_id: str, turn: int, outcome: str | None, latency_ms: int | None = None
    ) -> AuditRecord:
        return self.append(
            session_id=session_id,
            turn=turn,
            event=EventKind.TURN_COMPLETED,
            outcome=outcome or "ok",
            latency_ms=latency_ms,
        )
