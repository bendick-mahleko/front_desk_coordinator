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
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from app.channel import ClinicalChannel
from app.config import ConfigError, Settings, get_clinic_config, get_settings
from app.orchestrator import Orchestrator
from app.policy.redaction import mask_phone
from app.store.audit import AuditWriter
from app.store.models import AuditMirror, SessionStore
from app.store.session import Role, Session

logger = logging.getLogger("frontdesk")

CheckStatus = Literal["ok", "missing", "error"]


class HealthChecks(BaseModel):
    settings: CheckStatus
    clinic_config: CheckStatus
    model_credentials: CheckStatus


class ChatRequest(BaseModel):
    """The only thing a caller may say to a patient session.

    ``extra="forbid"`` deliberately, matching ``StrictArgs`` on every tool
    argument model. Pydantic's default is to *ignore* an unknown field, which
    meant ``{"message": "hi", "role": "clinical_assistant"}`` returned 200 and
    dropped the role. It could not elevate anything — the role is bound at
    session establishment and nothing here reads a body field — but a 200 is an
    answer, and the answer it gives is the wrong one. An integrator would ship
    code believing it worked.

    §3.2 makes channel and role configuration, *"not a runtime decision"*. An
    endpoint that accepts the words and quietly discards them is a worse
    expression of that than one that refuses them.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


class SessionSummary(BaseModel):
    session_id: str
    status: str
    turn_index: int
    patient_id: str | None

    ledger: dict[str, list[str]] = Field(default_factory=dict)
    """What this conversation has been handed, and may therefore refer to.

    The provenance rule — an identifier may only be passed into a function if a
    previous result produced it — is invisible until it refuses something. Read
    here so the UI can show it accumulating, which makes ``unknown_reference``
    explain itself.

    Every value is a clinic-issued reference rather than a fact about a person:
    the same set ``SAFE_REFERENCE_FIELDS`` exempts from the redactor. No name, no
    date of birth, no contact detail can appear here.
    """

    clinical: dict[str, Any] | None = None
    """Authentication state, for a clinical session only. Never a credential."""


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
            # The audit writer has to be wired in here. Phase 6 built it and the
            # eval runner passes one, but the served application defaulted to
            # None — so the running system produced no audit log at all, which
            # is the one thing specification §8 asks for by name.
            app.state.orchestrator = Orchestrator(
                audit=AuditWriter(directory=settings.audit_dir),
                mirror=AuditMirror(),
            )
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

    def _summary(session: Session) -> SessionSummary:
        clinical: dict[str, Any] | None = None
        if session.role is Role.CLINICAL_ASSISTANT:
            clinical = {
                "authenticated": session.clinical_authentication_valid,
                "staff_id": session.staff_id,
                "role": session.asserted_role.value if session.asserted_role else None,
                "expires_at": session.expires_at.isoformat() if session.expires_at else None,
            }
        return SessionSummary(
            session_id=session.session_id,
            status=session.status.value,
            turn_index=session.turn_index,
            patient_id=session.patient_id,
            ledger={
                "patient_ids": sorted(session.seen_patient_ids),
                "appointment_ids": sorted(session.seen_appointment_ids),
                "slot_ids": sorted(session.seen_slot_ids),
            },
            clinical=clinical,
        )

    @app.post("/clinical/session", response_model=SessionSummary, tags=["clinical"])
    def establish_clinical_session() -> SessionSummary:
        """Establish a session whose principal is clinical_assistant (spec §3.2).

        A separate endpoint rather than a role field on ``/chat``, and that is the
        whole design. §3.2: *"A Clinical Assistant session is never established on
        a patient-facing channel. Channel eligibility is clinic configuration, not
        a runtime decision."* A role parameter on the patient endpoint would make
        it exactly the runtime decision that sentence forbids — anything that can
        be asked for in a request body can be asked for by whoever can reach the
        endpoint.

        Establishing a session is not authenticating. The session comes back as
        ``clinical_assistant`` with its *effective* role SYSTEM, which can call
        nothing but ``authenticate_clinical_user`` (§4.13).
        """
        clinic = get_clinic_config()
        if not clinic.clinical.enabled:
            raise HTTPException(status_code=404, detail="clinical review is not enabled")

        channel = ClinicalChannel.name
        if not clinic.clinical.allows_channel(channel):
            # Configuration disagreeing with itself. 503 rather than 404: the
            # capability exists and the clinic has misconfigured it, which is an
            # operator problem and should read like one.
            raise HTTPException(
                status_code=503,
                detail=(
                    f"clinical channel {channel!r} is not eligible in this clinic's configuration"
                ),
            )

        session = Session(role=Role.CLINICAL_ASSISTANT, channel=channel)
        app.state.sessions[session.session_id] = session
        _store().save(session)
        return _summary(session)

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

    @app.get("/config", tags=["ops"])
    def config() -> dict[str, Any]:
        """The settings this process is actually running with.

        Every value here is either a model name, a path, a count or a policy
        knob. **No secret is ever included** — the credential is reported by
        *source* ("ANTHROPIC_API_KEY", "ant profile") and never by value, and a
        test asserts that no configured key appears in the response.
        """
        clinic = get_clinic_config()
        knowledge = _knowledge_summary()

        return {
            "service": {
                "name": settings.app_name,
                "version": settings.version,
                "environment": settings.environment,
            },
            "language_model": {
                "provider": settings.provider,
                "agent_model": settings.route_model(settings.agent_model),
                "classifier_model": settings.route_model(settings.classifier_model),
                "effort": settings.effort,
                "thinking": "adaptive",
                "server_side_fallbacks": settings.fallbacks_enabled,
                "credential_source": settings.credential_source() or "none",
            },
            "knowledge_base": {
                "embedding_provider": settings.embedding_provider,
                "embedding_model": (
                    settings.embedding_model
                    if settings.embedding_provider == "openrouter"
                    else "hashing-v1 (deterministic, offline)"
                ),
                "vector_store_path": str(settings.vector_store_path),
                "min_similarity": settings.knowledge_min_score,
                **knowledge,
            },
            "clinic_policy": {
                "clinic": clinic.name,
                "timezone": clinic.timezone,
                "verification_attempt_limit": clinic.policy.verification_attempt_limit,
                "late_cancellation_hours": clinic.policy.late_cancellation_hours,
                "max_slots_presented": clinic.policy.max_slots_presented,
                "emergency_number": clinic.policy.emergency_number,
            },
            "clinical": {
                "enabled": clinic.clinical.enabled,
                "session_minutes": clinic.clinical.session_minutes,
                "channels": list(clinic.clinical.channels),
                "permitted_roles": [role.value for role in clinic.clinical.permitted_roles],
                "directory": "SimulatedIdentityProvider",
                # No credential material, ever. The identity provider holds the
                # tokens; this process checks one and discards it (§3.2 item 2).
            },
            "storage": {
                "database_url": settings.database_url,
                "audit_dir": str(settings.audit_dir),
            },
        }

    def _knowledge_summary() -> dict[str, Any]:
        """Index state, reported without letting a failure take /config down."""
        try:
            store = _orchestrator().knowledge
            if store is None:
                return {"status": "not built — run `uv run build-kb`", "chunks": 0}
            return {
                "status": "ready",
                "store": type(store).__name__,
                "chunks": store.count(),
            }
        except Exception as exc:  # noqa: BLE001
            return {"status": f"unavailable ({type(exc).__name__})", "chunks": 0}

    @app.get("/session/{session_id}", response_model=SessionSummary, tags=["chat"])
    def session_summary(session_id: str) -> SessionSummary:
        return _summary(_session(session_id))

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health() -> HealthResponse:
        """Report startup check results.

        ``ok`` means every check passed. ``degraded`` means the service is up
        but something it needs is missing — ``checks`` says which.
        """
        return run_health_checks()

    return app


app = create_app()
