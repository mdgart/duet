import pytest

from duet.providers.base import RetryableError, compute_cost, with_retries
from duet.schemas.config import DuetConfig


def test_compute_cost_known_model(config: DuetConfig):
    # gpt-5.6-sol: $5 in / $30 out per MTok
    cost = compute_cost(config, "gpt-5.6-sol", input_tokens=1_000_000, output_tokens=100_000)
    assert cost == pytest.approx(5.0 + 3.0)


def test_compute_cost_unknown_model_is_zero(config: DuetConfig):
    assert compute_cost(config, "mystery-model", 1000, 1000) == 0.0


def test_with_retries_recovers(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RetryableError("503")
        return "ok"

    assert with_retries(flaky) == "ok"
    assert calls["n"] == 3


def test_with_retries_exhausts(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)

    def always_fails():
        raise RetryableError("429")

    with pytest.raises(RetryableError):
        with_retries(always_fails, max_attempts=2)
