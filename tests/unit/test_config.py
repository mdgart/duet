from pathlib import Path

import pytest

from duet.schemas.config import DEFAULT_PRICING, load_config


def test_defaults_without_file(tmp_path: Path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.roster.ids == ["mini", "haiku"]
    assert cfg.roster.get("mini").model == "gpt-5.4-mini"
    assert cfg.roster.get("haiku").model == "claude-haiku-4-5-20251001"
    assert cfg.utility.mapper.model == "claude-haiku-4-5"
    assert cfg.debate.max_rounds == 8
    assert cfg.budget.max_usd == 5.0
    assert cfg.sandbox.network == "none"


def test_file_overrides(tmp_path: Path):
    p = tmp_path / "duet.toml"
    p.write_text(
        """
[[participants]]
id = "terra"
provider = "openai"
model = "gpt-5.6-terra"

[[participants]]
id = "opus"
provider = "anthropic"
model = "claude-opus-5"

[budget]
max_usd = 5.0
"""
    )
    cfg = load_config(p)
    assert cfg.roster.ids == ["terra", "opus"]
    assert cfg.roster.get("terra").model == "gpt-5.6-terra"
    assert cfg.budget.max_usd == 5.0


def test_roster_of_three_from_file(tmp_path: Path):
    p = tmp_path / "duet.toml"
    p.write_text(
        """
[[participants]]
id = "sol"
provider = "openai"
model = "gpt-5.6-sol"

[[participants]]
id = "opus"
provider = "anthropic"
model = "claude-opus-5"

[[participants]]
id = "terra"
provider = "openai"
model = "gpt-5.6-terra"

[debate]
consensus = "quorum"
quorum = 0.67

[build]
coder = "opus"
"""
    )
    cfg = load_config(p)
    assert len(cfg.roster) == 3
    coder, reviewers = cfg.roles_for_task(0)
    assert coder.id == "opus"
    assert [r.id for r in reviewers] == ["sol", "terra"]
    assert cfg.debate.required_agreements(3) == 3  # ceil(0.67 * 3)


def test_bad_roster_in_file_is_rejected(tmp_path: Path):
    p = tmp_path / "duet.toml"
    p.write_text(
        """
[[participants]]
id = "solo"
provider = "openai"
model = "gpt-5.6-sol"
"""
    )
    with pytest.raises(ValueError, match="at least two participants"):
        load_config(p)


def test_pricing_overlay_keeps_defaults(tmp_path: Path):
    p = tmp_path / "duet.toml"
    p.write_text(
        """
[pricing."gpt-5.6-terra"]
input_per_mtok = 2.5
output_per_mtok = 15.0
"""
    )
    cfg = load_config(p)
    assert cfg.pricing_for("gpt-5.6-terra").input_per_mtok == 2.5
    for model in DEFAULT_PRICING:
        assert cfg.pricing_for(model) is not None


def test_profile_overlay(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DUET_HOME", str(tmp_path / "home"))
    profiles = tmp_path / "home" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "cheap.toml").write_text("[budget]\nmax_usd = 1.0\n")
    cfg = load_config(tmp_path / "missing.toml", profile="cheap")
    assert cfg.budget.max_usd == 1.0
    assert cfg.roster.ids == ["mini", "haiku"]  # roster untouched by the overlay


def test_profile_replaces_the_whole_roster(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DUET_HOME", str(tmp_path / "home"))
    profiles = tmp_path / "home" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "trio.toml").write_text(
        """
[[participants]]
id = "a"
provider = "openai"
model = "gpt-5.6-terra"

[[participants]]
id = "b"
provider = "anthropic"
model = "claude-sonnet-5"

[[participants]]
id = "c"
provider = "anthropic"
model = "claude-haiku-4-5"
"""
    )
    base = tmp_path / "duet.toml"
    base.write_text(
        """
[[participants]]
id = "sol"
provider = "openai"
model = "gpt-5.6-sol"

[[participants]]
id = "opus"
provider = "anthropic"
model = "claude-opus-5"
"""
    )
    cfg = load_config(base, profile="trio")
    assert cfg.roster.ids == ["a", "b", "c"]
