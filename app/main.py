"""FastAPI application.

Phase 0 exposes only ``GET /health``. It is deliberately more than a liveness
ping: it reports which startup checks passed, so a misconfigured environment
tells you what is wrong instead of failing later inside a conversation.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from app.config import ConfigError, Settings, get_clinic_config, get_settings

logger = logging.getLogger("frontdesk")

CheckStatus = Literal["ok", "missing", "error"]


class HealthChecks(BaseModel):
    settings: CheckStatus
    clinic_config: CheckStatus
    model_credentials: CheckStatus


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    version: str
    environment: str
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


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        summary="Clinic front-desk assistant prototype",
        lifespan=lifespan,
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
