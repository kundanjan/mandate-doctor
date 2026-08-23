"""Shared test fixtures."""

import pytest

from mandate_doctor.core.policy import reset_budget


@pytest.fixture(autouse=True)
def _reset_retry_budget():
    """Reset the global retry budget before every test to prevent state leakage."""
    reset_budget()
