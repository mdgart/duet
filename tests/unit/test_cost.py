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


def test_unpriced_participants_are_reported(config: DuetConfig):
    """An unpriced model spends real money and accrues $0.00, which quietly
    disables the budget cap — the one guard against a runaway session."""
    from duet.schemas.config import Participant

    cfg = DuetConfig(participants=[
        Participant(id="known", provider="anthropic", model="claude-opus-5"),
        Participant(id="mystery", provider="openai", model="gpt-未来"),
    ])
    assert [p.id for p in cfg.unpriced_participants()] == ["mystery"]
    assert config.unpriced_participants() == []  # the default roster is priced


def test_dated_and_aliased_model_ids_are_both_priced(config: DuetConfig):
    """Providers are called with dated ids; pricing must not depend on aliases."""
    for model in ("claude-haiku-4-5", "claude-haiku-4-5-20251001"):
        assert config.pricing_for(model) is not None


def test_cached_input_is_charged_at_its_own_rate_when_configured():
    """Providers report input_tokens *including* cached ones, so the cached
    share is split out — not added on top."""
    from duet.schemas.config import PricingEntry

    cfg = DuetConfig()
    cfg.pricing["m"] = PricingEntry(
        input_per_mtok=10.0, output_per_mtok=30.0, cached_input_per_mtok=1.0
    )
    # 1M input of which 800k cached, 100k output.
    cost = compute_cost(cfg, "m", 1_000_000, 100_000, cached_input_tokens=800_000)
    assert cost == pytest.approx(0.2 * 10.0 + 0.8 * 1.0 + 0.1 * 30.0)


def test_without_a_cached_rate_the_meter_errs_high_rather_than_low():
    """A guard against runaway spend must never under-report."""
    from duet.schemas.config import PricingEntry

    cfg = DuetConfig()
    cfg.pricing["m"] = PricingEntry(input_per_mtok=10.0, output_per_mtok=30.0)
    full = compute_cost(cfg, "m", 1_000_000, 0)
    with_cache_reported = compute_cost(cfg, "m", 1_000_000, 0, cached_input_tokens=900_000)
    assert with_cache_reported == full == pytest.approx(10.0)
