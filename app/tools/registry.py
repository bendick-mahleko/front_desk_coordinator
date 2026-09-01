"""The tool registry — where a Claude tool, the policy gate and a backend meet.

One composite decorator assembles four layers around every implementation.
Order matters, and reads outward from the function:

    beta_tool( serialise( gated( normalise_errors( impl ) ) ) )

* ``normalise_errors`` catches ``BackendError`` so a backend failure never
  reaches the agent loop as an exception (P3-T4).
* ``gated`` is the policy gate. It runs *outside* normalisation so a denied call
  never enters the implementation at all, and *inside* serialisation so the
  provenance ledger still sees real result objects rather than JSON.
* ``serialise`` converts results to JSON-safe values for the model.
* ``beta_tool`` publishes the schema, taken from the Pydantic argument model so
  there is still exactly one source of truth (AD-02).

The schema Claude receives is therefore generated from the same class that
validates the call at execution time. They cannot drift.
"""

from __future__ import annotations

import functools
import inspect
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, cast

from anthropic import beta_tool
from anthropic.lib.tools import BetaFunctionTool
from pydantic import BaseModel

from app.clinic_sim import ClinicSimulator
from app.policy.decorator import current_session, gated
from app.ports import BackendError
from app.tools.idempotency import idempotency_key, needs_key
from app.tools.schemas import ARGUMENT_MODELS

_REGISTRY: dict[str, BetaFunctionTool] = {}

_BACKENDS: ContextVar[ClinicSimulator | None] = ContextVar("backends", default=None)

DOMAIN_MODULES = (
    "app.tools.patients",
    "app.tools.scheduling",
    "app.tools.insurance",
    "app.tools.messaging",
    "app.tools.clinic",
    "app.tools.escalation",
    "app.tools.knowledge",
)

# What the model is told when a backend fails. Specification §6 requires the
# assistant to say the request could not be completed and offer a retry, staff
# escalation or a callback — so every one of these names a next step.
BACKEND_REMEDIES: dict[str, str] = {
    "upstream_timeout": (
        "The records system did not respond. Tell the patient the request could not be "
        "completed and offer to try again or have staff call them back."
    ),
    "appointment_not_found": (
        "Call get_patient_appointments to list the patient's appointments and confirm "
        "which one they mean."
    ),
    "slot_unavailable": (
        "That time was taken before the booking went through. Begin your reply by "
        "telling the patient that specific time is no longer available — do not "
        "silently offer alternatives as though nothing happened — then call "
        "search_available_appointments and offer what is left. Never imply it was "
        "booked."
    ),
    "double_booking": (
        "The patient already has an appointment at that time. Confirm what they want "
        "before trying again."
    ),
    "payer_unavailable": (
        "The payer did not respond. Escalate with escalate_to_staff for manual review."
    ),
    "rejected": (
        "The payer rejected the request. Escalate with escalate_to_staff for manual review."
    ),
    "invalid_number": (
        "The gateway rejected that number. Ask the patient to confirm their mobile number."
    ),
    "send_failed": (
        "The message could not be sent. Tell the patient, and offer to try again or "
        "escalate to staff."
    ),
    "not_found": "No matching record. Offer new-patient registration.",
}

GENERIC_BACKEND_REMEDY = (
    "Tell the patient the request could not be completed, and offer to try again, "
    "have staff help, or arrange a callback."
)


# ------------------------------------------------------------- backends ---


@contextmanager
def backend_scope(sim: ClinicSimulator) -> Iterator[ClinicSimulator]:
    """Bind the clinic backends for the duration of a turn."""
    token = _BACKENDS.set(sim)
    try:
        yield sim
    finally:
        _BACKENDS.reset(token)


_KNOWLEDGE: ContextVar[Any] = ContextVar("knowledge_base", default=None)


@contextmanager
def knowledge_scope(store: Any) -> Iterator[Any]:
    """Bind the knowledge base for the duration of a turn."""
    token = _KNOWLEDGE.set(store)
    try:
        yield store
    finally:
        _KNOWLEDGE.reset(token)


def knowledge_base() -> Any:
    store = _KNOWLEDGE.get()
    if store is None:
        raise NoBackendsError(
            "no knowledge base is bound; wrap the call in registry.knowledge_scope(...)"
        )
    return store


class NoBackendsError(RuntimeError):
    """A tool ran with no clinic backends bound."""


def backends() -> ClinicSimulator:
    sim = _BACKENDS.get()
    if sim is None:
        raise NoBackendsError(
            "no clinic backends are bound; wrap the call in registry.backend_scope(...)"
        )
    return sim


# --------------------------------------------------------------- layers ---


def normalise_errors(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Turn a ``BackendError`` into a tool result the model can act on.

    An exception inside the agent loop ends the turn; a result lets the
    assistant explain and offer a next step, which is what §6 asks for.
    """

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def inner(**kwargs: Any) -> Any:
            try:
                return fn(**kwargs)
            except BackendError as exc:
                return {
                    "error": exc.code,
                    "message": "The request could not be completed.",
                    "remedy": BACKEND_REMEDIES.get(exc.code, GENERIC_BACKEND_REMEDY),
                }

        return inner

    return wrap


def serialise(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Render a result as a JSON **string** for the tool_result block.

    The runner puts a tool's return value straight into
    ``{"type": "tool_result", "content": <value>}``, and the Messages API
    accepts a string or a list of content blocks there — not a bare object.
    Returning a dict happened to work against the first-party endpoint but is
    not a shape the schema permits, and a stricter implementation rejects the
    *next* request in the loop with a validation error that points at the
    message rather than at the tool. Serialising here makes the shape correct
    everywhere.

    Runs outside the gate on purpose: the provenance ledger absorbs identifiers
    from real result objects, so converting to JSON any earlier would leave it
    with nothing to read.

    This layer also **hides the underlying signature** from the SDK, which
    matters more than it looks. ``BetaFunctionTool.call()`` runs its own
    pydantic validation against whatever signature it can see and raises
    ``ValueError`` when arguments do not fit. That validation happens *outside*
    everything we wrap, so a malformed call from the model would raise into the
    agent loop and end the turn — instead of returning the ``invalid_arguments``
    denial that design §7 check 1 specifies, which the model can recover from.

    Presenting ``(**kwargs)`` leaves the gate as the only validator. The schema
    the model sees is unaffected: it comes from the Pydantic model passed as
    ``input_schema``, not from the signature.
    """

    @functools.wraps(fn)
    def inner(**kwargs: Any) -> str:
        return json.dumps(_to_json(fn(**kwargs)), default=str)

    del inner.__wrapped__
    inner.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [inspect.Parameter("kwargs", inspect.Parameter.VAR_KEYWORD)]
    )
    return inner


def _to_json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list | tuple):
        return [_to_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_json(item) for key, item in value.items()}
    return value


def tool(name: str) -> Callable[[Callable[..., Any]], BetaFunctionTool]:
    """Register one function as a gated Claude tool."""
    if name not in ARGUMENT_MODELS:
        raise KeyError(f"{name!r} has no argument model; add one to tools/schemas.py")

    def wrap(fn: Callable[..., Any]) -> BetaFunctionTool:
        pipeline = normalise_errors(name)(fn)
        pipeline = gated(name)(pipeline)
        pipeline = serialise(pipeline)
        built: BetaFunctionTool = beta_tool(
            name=name,
            input_schema=ARGUMENT_MODELS[name],
            strict=True,
        )(pipeline)
        _REGISTRY[name] = built
        return built

    return wrap


def key_for(fn_name: str, **args: Any) -> str | None:
    """The idempotency key for this call, or None if the function is read-only."""
    if not needs_key(fn_name):
        return None
    return idempotency_key(current_session().session_id, fn_name, args)


# -------------------------------------------------------------- registry ---


def load() -> dict[str, BetaFunctionTool]:
    """Import the domain modules so their tools self-register."""
    import importlib

    for module in DOMAIN_MODULES:
        importlib.import_module(module)
    return dict(_REGISTRY)


def all_tools() -> list[BetaFunctionTool]:
    """Every tool, for the ``tools=`` argument of the agent loop."""
    return list(load().values())


def get(name: str) -> BetaFunctionTool:
    return load()[name]


def tool_definitions() -> list[dict[str, Any]]:
    """The JSON the model actually receives. Snapshotted by the schema test."""
    return [cast("dict[str, Any]", definition.to_dict()) for definition in all_tools()]
