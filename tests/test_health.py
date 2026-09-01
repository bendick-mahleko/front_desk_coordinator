"""Phase 0 exit test: the skeleton boots and reports its own configuration."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.config import ConfigError, get_clinic_config, get_settings, reset_config_cache
from app.main import create_app


@pytest.fixture(autouse=True)
def _clean_config_cache():
    reset_config_cache()
    yield
    reset_config_cache()


@pytest.fixture
def client(monkeypatch):
    """A client with a credential present, so /health should be fully ok."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    reset_config_cache()
    with TestClient(create_app()) as test_client:
        yield test_client


def test_health_returns_ok(client):
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"] == {
        "settings": "ok",
        "clinic_config": "ok",
        "model_credentials": "ok",
    }


def test_health_reports_service_identity(client):
    body = client.get("/health").json()

    assert body["service"] == "AI Front Desk Coordinator"
    assert body["version"] == __version__
    assert body["environment"] in {"dev", "test", "prod"}


def test_missing_credential_degrades_but_still_serves(monkeypatch):
    """A missing key must be visible, not silent — and must not brick the app."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)
    reset_config_cache()

    with TestClient(create_app()) as test_client:
        body = test_client.get("/health").json()

    assert body["status"] == "degraded"
    assert body["checks"]["model_credentials"] == "missing"
    assert any("ANTHROPIC_API_KEY" in line for line in body["detail"])


def test_strict_credentials_refuses_to_start(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("STRICT_CREDENTIALS", "true")
    monkeypatch.setattr("pathlib.Path.is_dir", lambda self: False)
    reset_config_cache()

    with pytest.raises(ConfigError, match="NO ANTHROPIC CREDENTIAL"), TestClient(create_app()):
        pass


def test_clinic_config_loads_with_expected_policy_defaults():
    """The knobs the spec leaves open must be present and readable (design §20)."""
    clinic = get_clinic_config()

    assert clinic.policy.verification_attempt_limit == 3
    assert clinic.policy.late_cancellation_hours == 24
    assert clinic.policy.max_slots_presented == 3
    assert set(clinic.locations) == {"main_clinic", "satellite_office"}
    assert clinic.location_aliases == {}, "aliases must stay empty until confirmed"
    assert str(clinic.tz) == "America/New_York"


def test_settings_resolve_configured_models():
    settings = get_settings()

    assert settings.agent_model == "claude-opus-5"
    assert settings.classifier_model == "claude-haiku-4-5"
