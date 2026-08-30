"""Structured logging (P6-T6).

One emit path. A gate decision becomes a log line, an audit record and a UI
trace event from the same call, so the three cannot disagree about what
happened — which is exactly the failure that makes an audit trail worthless.

Log lines carry the same redacted view the audit record does. Nothing here is a
second place where a date of birth could escape.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from app.config import Settings


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # JSON in prod, human-readable in dev. The processors before the renderer
    # are identical, so the two differ only in presentation.
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.environment == "prod"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)


def bind_session(session_id: str, turn: int) -> None:
    """Attach the session to every line emitted for the rest of this turn."""
    structlog.contextvars.bind_contextvars(session_id=session_id, turn=turn)


def clear_session() -> None:
    structlog.contextvars.clear_contextvars()
