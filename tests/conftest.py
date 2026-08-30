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
