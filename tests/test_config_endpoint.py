"""The settings view's data (`GET /config`).

The load-bearing test is the leak one. A settings panel is a natural place for a
credential to end up by accident — it is the one screen whose whole purpose is
displaying configuration — so the assertion is that no configured secret value
appears anywhere in the response, checked against keys deliberately planted in
the environment.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.orchestrator import Orchestrator
from tests.replay import Say, ScriptedBackend, ScriptedPrescreen

PLANTED_ANTHROPIC = "sk-ant-planted-secret-value-do-not-leak"
PLANTED_OPENROUTER = "sk-or-v1-planted-secret-value-do-not-leak"


@pytest.fixture
def client(sim, clinic, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", PLANTED_ANTHROPIC)
    monkeypatch.setenv("OPENROUTER_API_KEY", PLANTED_OPENROUTER)

    from app.config import reset_config_cache

    reset_config_cache()
    orchestrator = Orchestrator(
        sim=sim,
        clinic=clinic,
        prescreen=ScriptedPrescreen(),
        backend=ScriptedBackend(script=[[Say("hi")]]),
        knowledge=None,
    )
    app = create_app(orchestrator=orchestrator)
    with TestClient(app) as test_client:
        yield test_client
    reset_config_cache()


# ------------------------------------------------------------- the leak ---


def test_no_credential_value_ever_appears(client):
    """The whole reason this endpoint needs a test."""
    body = client.get("/config").text

    assert PLANTED_ANTHROPIC not in body
    assert PLANTED_OPENROUTER not in body
    assert "sk-ant-" not in body
    assert "sk-or-" not in body


def test_the_credential_is_reported_by_source_not_by_value(client):
    payload = client.get("/config").json()

    assert payload["language_model"]["credential_source"] == "ANTHROPIC_API_KEY"


def test_no_field_anywhere_is_named_like_a_secret(client):
    """Guards against a later addition slipping one in under a new key."""

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                assert "api_key" not in key.lower(), f"{path}.{key}"
                assert "token" not in key.lower() or key == "input_tokens", f"{path}.{key}"
                assert "secret" not in key.lower(), f"{path}.{key}"
                walk(value, f"{path}.{key}")

    walk(client.get("/config").json())


# ------------------------------------------------------- what it reports ---


def test_it_reports_the_models_actually_in_use(client):
    model = client.get("/config").json()["language_model"]

    assert model["agent_model"]
    assert model["classifier_model"]
    assert model["provider"] in {"anthropic", "openrouter"}
    assert model["thinking"] == "adaptive"


def test_it_reports_the_embedding_configuration(client):
    knowledge = client.get("/config").json()["knowledge_base"]

    assert knowledge["embedding_provider"] in {"openrouter", "hashing"}
    assert knowledge["embedding_model"]
    assert "min_similarity" in knowledge


def test_it_reports_the_clinic_policy_knobs(client):
    policy = client.get("/config").json()["clinic_policy"]

    assert policy["verification_attempt_limit"] == 3
    assert policy["late_cancellation_hours"] == 24
    assert policy["emergency_number"] == "911"


def test_model_ids_are_reported_as_the_provider_sees_them(sim, clinic, monkeypatch):
    """The routed id, not the first-party one — otherwise the panel would show a
    model name that was never sent."""
    from app.config import reset_config_cache

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", PLANTED_OPENROUTER)
    monkeypatch.setenv("MODEL_PROVIDER", "openrouter")
    monkeypatch.setenv("AGENT_MODEL", "claude-opus-5")
    reset_config_cache()

    app = create_app(
        orchestrator=Orchestrator(
            sim=sim,
            clinic=clinic,
            prescreen=ScriptedPrescreen(),
            backend=ScriptedBackend(script=[[Say("hi")]]),
            knowledge=None,
        )
    )
    with TestClient(app) as test_client:
        model = test_client.get("/config").json()["language_model"]

    assert model["agent_model"] == "anthropic/claude-opus-5"
    assert model["server_side_fallbacks"] is False, "unavailable on OpenRouter"
    reset_config_cache()


def test_an_unbuilt_index_is_reported_rather_than_crashing(client):
    knowledge = client.get("/config").json()["knowledge_base"]

    assert knowledge["chunks"] == 0
    assert "build-kb" in knowledge["status"]


def test_the_whole_payload_serialises(client):
    """It is rendered by a UI, so it has to be plain JSON."""
    json.dumps(client.get("/config").json())
