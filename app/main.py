"""FastAPI application.

Phase 0 exposes only ``GET /health``. It is deliberately more than a liveness
ping: it reports which startup checks passed, so a misconfigured environment
tells you what is wrong instead of failing later inside a conversation.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.config import ConfigError, Settings, get_clinic_config, get_settings
from app.orchestrator import Orchestrator
from app.policy.redaction import mask_phone
from app.store.models import SessionStore
from app.store.session import Session

logger = logging.getLogger("frontdesk")

CheckStatus = Literal["ok", "missing", "error"]


class HealthChecks(BaseModel):
    settings: CheckStatus
    clinic_config: CheckStatus
    model_credentials: CheckStatus


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


class SessionSummary(BaseModel):
    session_id: str
    status: str
    turn_index: int
    patient_id: str | None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    version: str
    environment: str
    provider: str
    agent_model: str
    checks: HealthChecks
    detail: list[str] = []


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def run_health_checks() -> HealthResponse:
    """Evaluate every startup check and describe the result."""
    detail: list[str] = []

    try:
        settings = get_settings()
        settings_status: CheckStatus = "ok"
    except Exception as exc:  # pragma: no cover - only on malformed env
        logger.error("settings failed to load: %s", exc)
        return HealthResponse(
            status="degraded",
            service="unknown",
            version="unknown",
            environment="unknown",
            provider="unknown",
            agent_model="unknown",
            checks=HealthChecks(settings="error", clinic_config="error", model_credentials="error"),
            detail=[f"settings: {exc}"],
        )

    try:
        get_clinic_config()
        clinic_status: CheckStatus = "ok"
    except ConfigError as exc:
        clinic_status = "error"
        detail.append(f"clinic_config: {exc}")
    except Exception as exc:
        clinic_status = "error"
        detail.append(f"clinic_config: invalid - {exc}")

    source = settings.credential_source()
    if source is None:
        credential_status: CheckStatus = "missing"
        detail.append(
            "model_credentials: none found. Set ANTHROPIC_API_KEY in .env or run `ant auth login`."
        )
    else:
        credential_status = "ok"
        detail.append(f"model_credentials: found via {source}")
        detail.append(
            f"model_routing: {settings.provider} -> {settings.route_model(settings.agent_model)}"
        )

    checks = HealthChecks(
        settings=settings_status,
        clinic_config=clinic_status,
        model_credentials=credential_status,
    )
    healthy = all(value == "ok" for value in checks.model_dump().values())

    return HealthResponse(
        status="ok" if healthy else "degraded",
        service=settings.app_name,
        version=settings.version,
        environment=settings.environment,
        provider=settings.provider,
        agent_model=settings.route_model(settings.agent_model),
        checks=checks,
        detail=detail,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging(settings)

    health = run_health_checks()
    for line in health.detail:
        logger.info("startup check - %s", line)

    if health.checks.clinic_config != "ok":
        # Clinic config is not optional: every enum-validated location, every
        # policy limit and the timezone all come from it.
        raise ConfigError("; ".join(health.detail))

    if health.checks.model_credentials != "ok":
        banner = (
            "NO ANTHROPIC CREDENTIAL FOUND - the API will start, but any request "
            "that needs the model will fail. Set ANTHROPIC_API_KEY in .env or run "
            "`ant auth login`."
        )
        if settings.strict_credentials:
            raise ConfigError(banner)
        logger.error("%s", banner)

    logger.info(
        "%s v%s ready - env=%s agent=%s classifier=%s",
        settings.app_name,
        settings.version,
        settings.environment,
        settings.agent_model,
        settings.classifier_model,
    )
    yield
    logger.info("shutting down")


def create_app(orchestrator: Orchestrator | None = None) -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        summary="Clinic front-desk assistant prototype",
        lifespan=lifespan,
    )
    # Built lazily: constructing an Orchestrator loads the clinic simulator and
    # would make /health unreachable if anything about it were misconfigured.
    app.state.orchestrator = orchestrator
    app.state.sessions = {}
    app.state.store = None

    def _orchestrator() -> Orchestrator:
        if app.state.orchestrator is None:
            app.state.orchestrator = Orchestrator()
        return app.state.orchestrator

    def _store() -> SessionStore:
        if app.state.store is None:
            app.state.store = SessionStore()
        return app.state.store

    def _session(session_id: str | None) -> Session:
        if session_id is None:
            session = Session()
        elif session_id in app.state.sessions:
            session = app.state.sessions[session_id]
        else:
            loaded = _store().load(session_id)
            if loaded is None:
                raise HTTPException(status_code=404, detail="unknown session")
            session = loaded
        app.state.sessions[session.session_id] = session
        return session

    @app.post("/chat", tags=["chat"])
    def chat(request: ChatRequest) -> EventSourceResponse:
        """Run one turn, streaming trace events then the reply.

        Server-sent events rather than a plain JSON reply: the trace is what
        makes the gate visible in a demo, and it arrives as the turn happens.
        """
        session = _session(request.session_id)
        orchestrator = _orchestrator()

        def stream() -> Iterator[dict[str, str]]:
            yield {"event": "session", "data": json.dumps({"session_id": session.session_id})}
            for event in orchestrator.stream_turn(session, request.message):
                yield {"event": event["kind"], "data": json.dumps(event, default=str)}
            _store().save(session)

        return EventSourceResponse(stream())

    @app.get("/outbox", tags=["clinic"])
    def outbox() -> list[dict[str, Any]]:
        """Messages the assistant has sent. Nothing leaves the machine."""
        sim = _orchestrator().simulator
        return [
            {
                "message_id": receipt.message_id,
                "phone_number": mask_phone(receipt.phone_number),
                "message_type": receipt.message_type.value,
                "delivery_status": receipt.delivery_status.value,
                "sent_at": receipt.sent_at.isoformat(),
            }
            for receipt in reversed(sim.messages.outbox())
        ]

    @app.get("/staff/queue", tags=["clinic"])
    def staff_queue() -> list[dict[str, Any]]:
        """Open escalations, newest first."""
        sim = _orchestrator().simulator
        return [
            {
                "ticket_id": ticket.ticket_id,
                "reason": ticket.reason.value,
                "priority": ticket.priority.value,
                "notes": ticket.notes,
                "patient_id": ticket.patient_id,
                "created_at": ticket.created_at.isoformat(),
            }
            for ticket in reversed(sim.staff.tickets())
        ]

    @app.get("/session/{session_id}", response_model=SessionSummary, tags=["chat"])
    def session_summary(session_id: str) -> SessionSummary:
        session = _session(session_id)
        return SessionSummary(
            session_id=session.session_id,
            status=session.status.value,
            turn_index=session.turn_index,
            patient_id=session.patient_id,
        )

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health() -> HealthResponse:
        """Report startup check results.

        ``ok`` means every check passed. ``degraded`` means the service is up
        but something it needs is missing — ``checks`` says which.
        """
        return run_health_checks()

    return app


app = create_app()
