"""Settings and clinic configuration.

Two separate concerns deliberately kept apart:

* ``Settings``      — how this process runs (ports, models, credentials, paths).
                      Comes from the environment / ``.env``.
* ``ClinicConfig``  — how the *clinic* behaves (hours, locations, attempt
                      limits, cancellation window). Comes from ``clinic.yaml``.

Clinic policy is data, not code: changing the verification attempt limit or the
late-cancellation window must never require a deploy (design §4, AD-01).
"""

from __future__ import annotations

import os
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

LocationKey = Literal["main_clinic", "satellite_office"]

OPENROUTER_BASE_URL = "https://openrouter.ai/api"
"""The SDK appends ``/v1/messages``, so the base stops at ``/api``.

OpenRouter exposes an Anthropic-native Messages endpoint, not just an
OpenAI-compatible one, so the first-party SDK works against it unchanged —
tools, strict schemas, adaptive thinking, effort, prompt caching and
mid-conversation system messages all pass through the translation."""

OPENROUTER_MODEL_IDS: dict[str, str] = {
    "claude-opus-5": "anthropic/claude-opus-5",
    "claude-opus-4-8": "anthropic/claude-opus-4.8",
    "claude-opus-4-7": "anthropic/claude-opus-4.7",
    "claude-sonnet-5": "anthropic/claude-sonnet-5",
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4.6",
    "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
    "claude-fable-5": "anthropic/claude-fable-5",
}
"""First-party model ids to their OpenRouter slugs.

Note Haiku: ``claude-haiku-4-5`` first-party, ``claude-haiku-4.5`` on
OpenRouter. Hyphen versus dot, and getting it wrong is a 404."""

WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


class ConfigError(RuntimeError):
    """Raised when configuration is missing or unusable. Never swallowed."""


# --------------------------------------------------------------- clinic ---


class DayHours(BaseModel):
    """Opening hours for one weekday. Both fields null means closed."""

    open: str | None = None
    close: str | None = None

    @property
    def is_closed(self) -> bool:
        return self.open is None or self.close is None


class LocationConfig(BaseModel):
    name: str
    address: str
    parking: str
    accessibility: str


class PolicyConfig(BaseModel):
    """Knobs the specification leaves to "clinic policy" (design §20)."""

    verification_attempt_limit: int = Field(default=3, ge=1, le=10)
    late_cancellation_hours: int = Field(default=24, ge=0)
    late_cancellation_notice: str
    max_slots_presented: int = Field(default=3, ge=1, le=5)
    verification_expires_after_minutes: int | None = None
    emergency_number: str = "911"
    """What the assistant tells someone to call. US default; this is data, not a
    constant, because it is wrong everywhere else."""


class ClinicConfig(BaseModel):
    """The clinic's own configuration, loaded from clinic.yaml."""

    name: str
    timezone: str
    hours: dict[str, DayHours]
    holidays: list[date] = Field(default_factory=list)
    locations: dict[LocationKey, LocationConfig]
    location_aliases: dict[str, LocationKey] = Field(default_factory=dict)
    providers: list[str]
    policy: PolicyConfig

    @field_validator("timezone")
    @classmethod
    def _timezone_must_resolve(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - env dependent
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _all_weekdays_present(self) -> ClinicConfig:
        missing = [day for day in WEEKDAYS if day not in self.hours]
        if missing:
            raise ValueError(f"clinic hours missing for: {', '.join(missing)}")
        return self

    @property
    def tz(self) -> ZoneInfo:
        """The clinic's timezone — the only one that may be used for dates.

        Relative expressions such as "next Tuesday" resolve against the clinic,
        never against the server (spec §4.5).
        """
        return ZoneInfo(self.timezone)

    @classmethod
    def load(cls, path: Path) -> ClinicConfig:
        if not path.is_file():
            raise ConfigError(
                f"clinic config not found at {path}. "
                "Copy clinic.yaml into place or set CLINIC_CONFIG_PATH."
            )
        try:
            raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"clinic config at {path} is not valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"clinic config at {path} must be a YAML mapping")
        return cls.model_validate(raw)


# ------------------------------------------------------------- settings ---


class Settings(BaseSettings):
    """Process configuration, from the environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "AI Front Desk Coordinator"
    version: str = "0.0.1"
    environment: Literal["dev", "test", "prod"] = "dev"

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"

    # Credentials. Any may be blank — the SDK also resolves a profile stored by
    # `ant auth login`, which credential_source() checks for separately.
    anthropic_api_key: str | None = None
    anthropic_auth_token: str | None = None
    openrouter_api_key: str | None = None
    strict_credentials: bool = False

    model_provider: Literal["auto", "anthropic", "openrouter"] = "auto"
    """Where model calls go. "auto" prefers a first-party Anthropic credential
    and falls back to OpenRouter when only that key is present."""

    agent_model: str = "claude-opus-5"
    classifier_model: str = "claude-haiku-4-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    """Thinking depth. Front-desk routing is constrained enough that medium
    holds up; raise it if the Phase 8 evals show routing errors."""

    server_side_fallbacks: bool = True
    """Route around a safety refusal rather than returning an empty turn.

    Ignored on OpenRouter, whose translation rejects the ``fallbacks`` parameter
    and its beta flag with a 400. See ``fallbacks_enabled``."""

    clinic_config_path: Path = Path("clinic.yaml")
    database_url: str = "sqlite:///./data/frontdesk.db"
    audit_dir: Path = Path("audit")
    """Where the hash-chained log is written. Gitignored; a real deployment
    would point this at append-only storage."""

    # ------------------------------------------------------------ routing ---

    @property
    def provider(self) -> Literal["anthropic", "openrouter"]:
        if self.model_provider != "auto":
            return self.model_provider
        if self.anthropic_api_key or self.anthropic_auth_token:
            return "anthropic"
        if self.openrouter_api_key:
            return "openrouter"
        # No explicit key: the SDK may still resolve an `ant auth login` profile.
        return "anthropic"

    @property
    def fallbacks_enabled(self) -> bool:
        """Server-side refusal fallbacks are a first-party feature only."""
        return self.server_side_fallbacks and self.provider == "anthropic"

    def route_model(self, model: str) -> str:
        """Translate a first-party model id for the configured provider."""
        if self.provider != "openrouter" or "/" in model:
            return model
        return OPENROUTER_MODEL_IDS.get(model, f"anthropic/{model}")

    def client_kwargs(self) -> dict[str, Any]:
        """Constructor arguments for the Anthropic SDK client."""
        if self.provider == "openrouter":
            if not self.openrouter_api_key:
                raise ConfigError("model_provider is openrouter but OPENROUTER_API_KEY is unset")
            return {"api_key": self.openrouter_api_key, "base_url": OPENROUTER_BASE_URL}
        return {}

    @property
    def resolved_clinic_config_path(self) -> Path:
        path = self.clinic_config_path
        return path if path.is_absolute() else PROJECT_ROOT / path

    def credential_source(self) -> str | None:
        """Where a model credential would come from, or None if nowhere.

        An unset ``ANTHROPIC_API_KEY`` does not mean there are no credentials:
        the SDK falls back to a profile written by ``ant auth login``. Checking
        only the env var would report a false negative.
        """
        if self.anthropic_api_key:
            return "ANTHROPIC_API_KEY"
        if self.anthropic_auth_token:
            return "ANTHROPIC_AUTH_TOKEN"
        if self.openrouter_api_key:
            return "OPENROUTER_API_KEY"
        profile_dir = Path(os.path.expanduser("~")) / ".config" / "anthropic"
        if profile_dir.is_dir() and any(profile_dir.iterdir()):
            return "ant profile"
        return None

    def require_credentials(self) -> str:
        """Assert a credential exists. Call before the first model request."""
        source = self.credential_source()
        if source is None:
            raise ConfigError(
                "No model credential found. Set ANTHROPIC_API_KEY or "
                "OPENROUTER_API_KEY in .env, or run `ant auth login`."
            )
        return source


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_clinic_config() -> ClinicConfig:
    return ClinicConfig.load(get_settings().resolved_clinic_config_path)


def reset_config_cache() -> None:
    """Drop cached config. For tests that manipulate the environment."""
    get_settings.cache_clear()
    get_clinic_config.cache_clear()
