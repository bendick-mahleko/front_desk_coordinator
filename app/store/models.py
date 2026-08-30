"""Durable session storage — SQLite write-behind.

The session is authoritative in process and written through after every turn.
That keeps the hot path in memory while surviving a restart mid-conversation.

The row stores the session as JSON rather than a wide table: the shape belongs
to ``Session`` (design §6), and mirroring its fields into columns would create a
second schema to keep in step. Queries against session *contents* are not a
requirement — the audit log in Phase 6 is what gets queried.

**No raw identifier value is ever written**, because ``Session`` cannot hold
one. The transcript is passed through the redactor on the way in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Field, SQLModel, create_engine, select
from sqlmodel import Session as DBSession

from app.config import Settings, get_settings
from app.policy.redaction import redact_value
from app.store.session import Session


class SessionRecord(SQLModel, table=True):
    """One conversation, serialised."""

    __tablename__ = "sessions"

    session_id: str = Field(primary_key=True)
    created_at: datetime
    updated_at: datetime
    status: str = Field(index=True)
    """Denormalised for the staff view — the only field worth an index."""
    turn_index: int
    payload: str
    """The Session as JSON."""


class AuditRow(SQLModel, table=True):
    """A queryable mirror of the JSONL log.

    The file is authoritative — it is the thing that is hash-chained. This
    exists so the staff view and the eval runner can ask questions like "every
    denied call in this session" without parsing a day of JSON.
    """

    __tablename__ = "audit"

    event_id: str = Field(primary_key=True)
    ts: str = Field(index=True)
    session_id: str = Field(index=True)
    turn: int
    event: str = Field(index=True)
    function: str | None = Field(default=None, index=True)
    outcome: str | None = None
    latency_ms: int | None = None
    payload: str
    """The full record as written, so the row and the line cannot diverge."""


def _sqlite_path(url: str) -> Path | None:
    if not url.startswith("sqlite"):
        return None
    _, _, tail = url.partition(":///")
    if not tail or tail == ":memory:":
        return None
    return Path(tail)


def build_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    path = _sqlite_path(settings.database_url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_url, echo=False)
    SQLModel.metadata.create_all(engine)
    return engine


class SessionStore:
    """Write-behind persistence for conversation sessions."""

    def __init__(self, engine: Engine | None = None, settings: Settings | None = None) -> None:
        self._engine = engine or build_engine(settings)

    def save(self, session: Session) -> None:
        payload = _redacted_payload(session)
        now = datetime.now(UTC)
        with DBSession(self._engine) as db:
            row = db.get(SessionRecord, session.session_id)
            if row is None:
                row = SessionRecord(
                    session_id=session.session_id,
                    created_at=session.created_at,
                    updated_at=now,
                    status=session.status.value,
                    turn_index=session.turn_index,
                    payload=payload,
                )
            else:
                row.updated_at = now
                row.status = session.status.value
                row.turn_index = session.turn_index
                row.payload = payload
            db.add(row)
            db.commit()

    def load(self, session_id: str) -> Session | None:
        with DBSession(self._engine) as db:
            row = db.get(SessionRecord, session_id)
        return Session.model_validate_json(row.payload) if row else None

    def list_ids(self, limit: int = 50) -> list[str]:
        with DBSession(self._engine) as db:
            rows = db.exec(
                select(SessionRecord).order_by(SessionRecord.updated_at.desc()).limit(limit)  # type: ignore[attr-defined]
            ).all()
        return [row.session_id for row in rows]

    def delete(self, session_id: str) -> None:
        with DBSession(self._engine) as db:
            row = db.get(SessionRecord, session_id)
            if row is not None:
                db.delete(row)
                db.commit()


class AuditMirror:
    """Writes each audit record into SQLite alongside the JSONL file."""

    def __init__(self, engine: Engine | None = None, settings: Settings | None = None) -> None:
        self._engine = engine or build_engine(settings)

    def mirror(self, record: Any) -> None:
        with DBSession(self._engine) as db:
            db.add(
                AuditRow(
                    event_id=record.event_id,
                    ts=record.ts,
                    session_id=record.session_id,
                    turn=record.turn,
                    event=record.event,
                    function=record.function,
                    outcome=record.outcome,
                    latency_ms=record.latency_ms,
                    payload=record.model_dump_json(exclude_none=True),
                )
            )
            db.commit()

    def for_session(self, session_id: str) -> list[AuditRow]:
        with DBSession(self._engine) as db:
            return list(
                db.exec(
                    select(AuditRow).where(AuditRow.session_id == session_id).order_by(AuditRow.ts)
                ).all()
            )

    def denials(self, session_id: str) -> list[AuditRow]:
        return [row for row in self.for_session(session_id) if row.outcome == "denied"]


def _redacted_payload(session: Session) -> str:
    """Serialise, with the transcript swept on the way to disk (spec §4.2)."""
    if not session.transcript:
        return session.model_dump_json()

    safe = session.model_copy(
        update={
            "transcript": [
                {key: redact_value(key, value) for key, value in entry.items()}
                for entry in session.transcript
            ]
        }
    )
    return safe.model_dump_json()
