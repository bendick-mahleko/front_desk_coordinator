"""The ``@gated`` decorator — where the gate actually stops a call.

Every tool function is wrapped by this in Phase 3. The model cannot reach a
backend without passing through it (AD-01, AD-03).

A denial returns a structured result instead of executing. It is not an
exception: the model receives it as a tool result, reads the remedy, and
re-plans within the same turn. That is what makes an over-eager call a
conversational event rather than a crash (design §11).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from app.config import ClinicConfig
from app.policy import provenance
from app.policy.gates import PolicyGate, Verdict
from app.store.session import Session

T = TypeVar("T")

_CURRENT_SESSION: ContextVar[Session | None] = ContextVar("current_session", default=None)
_GATE: ContextVar[PolicyGate | None] = ContextVar("current_gate", default=None)


class NoActiveSessionError(RuntimeError):
    """A gated function was called outside a session scope.

    Fails loudly rather than defaulting to an empty session, which would grant
    OPEN-level access to a caller that never established one.
    """


@dataclass(frozen=True)
class ToolDenial:
    """What the model receives when the gate says no."""

    function: str
    code: str
    message: str
    remedy: str
    required: str | None = None
    actual: str | None = None

    def as_tool_result(self) -> dict[str, Any]:
        """The payload the tool layer returns with ``is_error: true``."""
        return {
            "error": self.code,
            "message": self.message,
            "remedy": self.remedy,
            "required_level": self.required,
            "current_level": self.actual,
        }

    @classmethod
    def from_verdict(cls, function: str, verdict: Verdict) -> ToolDenial:
        return cls(
            function=function,
            code=verdict.code.value if verdict.code else "denied",
            message=verdict.message,
            remedy=verdict.remedy,
            required=verdict.required.value if verdict.required else None,
            actual=verdict.actual.value if verdict.actual else None,
        )


class AuditSink(Protocol):
    """Phase 6 supplies the hash-chained implementation."""

    def gate_decision(self, function: str, verdict: Verdict, session: Session) -> None: ...

    def tool_result(self, function: str, result: Any, session: Session) -> None: ...


class NullAuditSink:
    """Records nothing. The default until Phase 6 wires the real writer."""

    def gate_decision(self, function: str, verdict: Verdict, session: Session) -> None:
        return None

    def tool_result(self, function: str, result: Any, session: Session) -> None:
        return None


_NULL_AUDIT = NullAuditSink()
_AUDIT: ContextVar[AuditSink | None] = ContextVar("audit_sink", default=None)


# ------------------------------------------------------------------ scope ---


@contextmanager
def session_scope(
    session: Session,
    clinic: ClinicConfig | None = None,
    gate: PolicyGate | None = None,
    audit: AuditSink | None = None,
) -> Iterator[Session]:
    """Bind a session (and its gate) for the duration of a turn."""
    session_token = _CURRENT_SESSION.set(session)
    gate_token = _GATE.set(gate or PolicyGate(clinic))
    audit_token = _AUDIT.set(audit) if audit is not None else None
    try:
        yield session
    finally:
        if audit_token is not None:
            _AUDIT.reset(audit_token)
        _GATE.reset(gate_token)
        _CURRENT_SESSION.reset(session_token)


def current_session() -> Session:
    session = _CURRENT_SESSION.get()
    if session is None:
        raise NoActiveSessionError(
            "no session is bound; wrap the call in policy.decorator.session_scope(...)"
        )
    return session


def current_gate() -> PolicyGate:
    gate = _GATE.get()
    if gate is None:
        raise NoActiveSessionError("no gate is bound; use session_scope(...)")
    return gate


def current_audit() -> AuditSink:
    return _AUDIT.get() or _NULL_AUDIT


# -------------------------------------------------------------- decorator ---


def gated(name: str) -> Callable[[Callable[..., T]], Callable[..., T | dict[str, Any]]]:
    """Wrap a tool function in the policy gate.

    The wrapped function receives the *validated* arguments as keywords, so an
    implementation never re-parses what the gate already coerced.
    """

    def wrap(fn: Callable[..., T]) -> Callable[..., T | dict[str, Any]]:
        @functools.wraps(fn)
        def inner(**kwargs: Any) -> T | dict[str, Any]:
            session = current_session()
            gate = current_gate()
            audit = current_audit()

            verdict = gate.evaluate(name, kwargs, session)
            audit.gate_decision(name, verdict, session)  # every decision, always

            if not verdict.allowed:
                return ToolDenial.from_verdict(name, verdict).as_tool_result()

            assert verdict.args is not None
            result = fn(**verdict.args.model_dump())

            provenance.absorb(result, session)
            audit.tool_result(name, result, session)
            return result

        inner.__gated_name__ = name  # type: ignore[attr-defined]
        return inner

    return wrap


def is_gated(fn: Callable[..., Any]) -> bool:
    """True if ``fn`` passes through the gate. Phase 3's coverage test uses this."""
    return hasattr(fn, "__gated_name__")
