"""Shared fixtures.

``today`` is pinned so slot generation is reproducible: the simulator builds a
rolling 30-day grid from a seed and a base date, and a floating base would make
availability drift between runs.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.clinic_sim import ClinicSimulator
from app.config import get_clinic_config, reset_config_cache

# A Monday, so weekday arithmetic in tests reads clearly.
PINNED_TODAY = date(2026, 9, 7)


class LiveCallAttempted(BaseException):
    """Raised when a test reaches the network. Deliberately not an Exception."""


@pytest.fixture(autouse=True)
def _isolate_from_dotenv(monkeypatch):
    """Tests read the shipped defaults, never a developer's local .env.

    A .env is normal during development — it is how you point the app at a
    different model or provider. Without this, whether the suite passes depends
    on what happens to be in an untracked file, which is the worst kind of
    flake: it reproduces for one person and nobody else.
    """
    from app.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    reset_config_cache()
    yield
    reset_config_cache()


@pytest.fixture(autouse=True)
def _no_live_model_calls(request, monkeypatch):
    """Fail loudly if a test reaches the model.

    Phase 5 added a classifier that runs on every turn, and it quietly made the
    whole suite hit the network — slow, costly and non-deterministic, while
    still passing. A guard is the only way that stays fixed.

    Mark a test ``@pytest.mark.live`` to opt out.
    """
    if request.node.get_closest_marker("live"):
        return

    import anthropic._base_client as base

    def blocked(self, *args, **kwargs):
        # BaseException, not Exception: production code catches Exception
        # broadly so a model outage cannot take a turn down, and that would
        # swallow this guard and leave it silently useless.
        raise LiveCallAttempted(
            "this test tried to call the model. Inject a stub backend or "
            "ScriptedPrescreen, or mark the test @pytest.mark.live."
        )

    monkeypatch.setattr(base.SyncAPIClient, "request", blocked)


@pytest.fixture(scope="session")
def clinic():
    reset_config_cache()
    return get_clinic_config()


@pytest.fixture
def today() -> date:
    return PINNED_TODAY


@pytest.fixture
def sim(clinic, today) -> ClinicSimulator:
    return ClinicSimulator.build(clinic=clinic, today=today)
