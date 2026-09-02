"""The golden-idea suite: does a real debate converge on a real idea?

This is M1's exit criterion, not a unit test. It spends money and takes
minutes, so it is opt-in behind its own flag — separate from DUET_CONTRACT,
which costs cents and seconds:

    DUET_GOLDEN=1 uv run --extra dev pytest tests/golden -v -s

It doubles as the regression baseline for prompt changes. Convergence is
asserted; everything else observable about a run — rounds, cost, objections,
marker discipline — is recorded, so a prompt edit can be judged by diffing two
reports rather than by reading one transcript and forming an impression.
Set DUET_GOLDEN_REPORT=path.json to keep one.
"""

import json
import os

import pytest

from duet.schemas.config import DuetConfig, Participant, PricingEntry

GOLDEN_ENABLED = os.environ.get("DUET_GOLDEN") == "1"

# Small models by default: this measures whether the protocol converges, not
# how clever the participants are. Override to judge a flagship roster.
OPENAI_MODEL = os.environ.get("DUET_GOLDEN_OPENAI_MODEL", "gpt-5.4-mini")
ANTHROPIC_MODEL = os.environ.get("DUET_GOLDEN_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

PER_IDEA_BUDGET_USD = float(os.environ.get("DUET_GOLDEN_BUDGET", "1.00"))

# The suite needs the budget cap live: an unpriced model accrues $0.00 however
# much it spends, so the cap that stops a runaway would be inert. These are
# estimates for the small models above — good enough to bound a test run, not
# good enough to bill anyone. Models absent here skip rather than run blind.
ESTIMATED_PRICING = {
    "gpt-5.4-mini": PricingEntry(input_per_mtok=0.25, output_per_mtok=2.0),
    "gpt-5-mini": PricingEntry(input_per_mtok=0.25, output_per_mtok=2.0),
    "claude-haiku-4-5-20251001": PricingEntry(input_per_mtok=1.0, output_per_mtok=5.0),
}


@pytest.fixture(scope="session")
def golden_config() -> DuetConfig:
    if not GOLDEN_ENABLED:
        pytest.skip("golden-idea suite is opt-in: set DUET_GOLDEN=1")
    missing = [k for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY") if not os.environ.get(k)]
    if missing:
        pytest.skip(f"needs both providers; missing {', '.join(missing)}")

    config = DuetConfig(
        participants=[
            Participant(id="mini", provider="openai", model=OPENAI_MODEL, color="cyan"),
            Participant(id="haiku", provider="anthropic", model=ANTHROPIC_MODEL, color="dark_orange"),
        ]
    )
    # A per-idea cap so a runaway costs a dollar, not a session.
    config.budget.max_usd = PER_IDEA_BUDGET_USD
    for model in (OPENAI_MODEL, ANTHROPIC_MODEL):
        if config.pricing_for(model) is None:
            estimate = ESTIMATED_PRICING.get(model)
            if estimate is None:
                pytest.skip(
                    f"{model} has no price and no estimate; the budget cap would be inert. "
                    f"Add one to ESTIMATED_PRICING or to [pricing] in duet.toml."
                )
            config.pricing[model] = estimate
    return config


@pytest.fixture(scope="session")
def report() -> dict:
    """Collects one row per idea and prints a comparison table at the end."""
    rows: dict = {}
    yield rows
    if not rows:
        return
    print("\n\n=== golden ideas ===")
    header = f"{'idea':<12}{'outcome':<11}{'rounds':>7}{'cost':>9}{'objections':>12}{'markers':>28}"
    print(header)
    print("-" * len(header))
    for name, row in rows.items():
        markers = ", ".join(f"{k}={v}" for k, v in sorted(row["markers"].items())) or "none"
        print(
            f"{name:<12}{row['outcome']:<11}{row['rounds']:>7}{row['cost_usd']:>9.4f}"
            f"{row['objections']:>12}{markers:>28}"
        )
    total = sum(r["cost_usd"] for r in rows.values())
    print(f"{'total':<12}{'':<11}{'':>7}{total:>9.4f}")

    path = os.environ.get("DUET_GOLDEN_REPORT")
    if path:
        with open(path, "w") as handle:
            json.dump(rows, handle, indent=2, sort_keys=True)
        print(f"report written to {path}")
