"""Opt-in contract tests against real provider APIs (SPECS §12).

These cost money and need network, so they are skipped unless DUET_CONTRACT=1
is set *and* the relevant key is present. Skipping is deliberate rather than
silent: the tests still collect, so `pytest -v` shows what is not being checked.

    DUET_CONTRACT=1 uv run --extra dev pytest tests/contract -v

Override the models with DUET_CONTRACT_OPENAI_MODEL / _ANTHROPIC_MODEL. The
defaults are small ones — this suite validates adapter behaviour, not model
quality, and a full pass should cost cents.
"""

import os

import pytest

from duet.providers.base import build_provider
from duet.schemas.config import DuetConfig, ModelSpec

CONTRACT_ENABLED = os.environ.get("DUET_CONTRACT") == "1"

# One value per param, not three: a fixture's params are single objects, and
# pytest would otherwise read the tuple as three argument names.
PROVIDERS = [
    pytest.param(
        ("openai", os.environ.get("DUET_CONTRACT_OPENAI_MODEL", "gpt-5.4-mini"), "OPENAI_API_KEY"),
        id="openai",
    ),
    pytest.param(
        (
            "anthropic",
            os.environ.get("DUET_CONTRACT_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            "ANTHROPIC_API_KEY",
        ),
        id="anthropic",
    ),
]


@pytest.fixture(params=PROVIDERS)
def provider(request):
    """A live adapter, or a skip explaining precisely what is missing."""
    name, model, key_var = request.param
    if not CONTRACT_ENABLED:
        pytest.skip("contract tests are opt-in: set DUET_CONTRACT=1")
    if not os.environ.get(key_var):
        pytest.skip(f"{key_var} is not set")
    config = DuetConfig()
    return build_provider(ModelSpec(provider=name, model=model), config)
