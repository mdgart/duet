from pathlib import Path

from duet.schemas.config import DEFAULT_PRICING, load_config


def test_defaults_without_file(tmp_path: Path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.models.openai == "gpt-5.6-sol"
    assert cfg.models.anthropic == "claude-opus-5"
    assert cfg.debate.max_rounds == 8
    assert cfg.budget.max_usd == 25.0
    assert cfg.sandbox.network == "none"


def test_file_overrides(tmp_path: Path):
    p = tmp_path / "duet.toml"
    p.write_text(
        """
[models]
openai = "gpt-5.6-terra"

[budget]
max_usd = 5.0
"""
    )
    cfg = load_config(p)
    assert cfg.models.openai == "gpt-5.6-terra"
    assert cfg.models.anthropic == "claude-opus-5"  # untouched default
    assert cfg.budget.max_usd == 5.0


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
