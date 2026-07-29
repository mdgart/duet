"""Configuration schema and loader for duet.toml (SPECS §9)."""

from __future__ import annotations

import os

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ProviderName = Literal["openai", "anthropic"]


class ModelsConfig(BaseModel):
    openai: str = "gpt-5.6-sol"
    anthropic: str = "claude-opus-5"
    mapper: str = "claude-haiku-4-5"


class DebateConfig(BaseModel):
    max_rounds: int = 8
    min_objection_rounds: int = 2
    reopen_limit: int = 3


class BuildConfig(BaseModel):
    coder: ProviderName = "anthropic"
    alternate_roles: bool = False
    max_review_cycles: int = 6
    max_tool_calls_per_turn: int = 25
    context_budget_tokens: int = 60_000


class BudgetConfig(BaseModel):
    max_usd: float = 25.0
    max_wall_minutes: int = 90


class SandboxConfig(BaseModel):
    runner: Literal["docker", "local-unsafe"] = "docker"
    network: Literal["none", "allowlist"] = "none"
    network_allowlist: list[str] = Field(
        default_factory=lambda: ["pypi.org", "registry.npmjs.org"]
    )


class PricingEntry(BaseModel):
    """USD per million tokens."""

    input_per_mtok: float
    output_per_mtok: float


# Defaults are best-known published prices; override in [pricing] of duet.toml.
DEFAULT_PRICING: dict[str, PricingEntry] = {
    "gpt-5.6-sol": PricingEntry(input_per_mtok=5.0, output_per_mtok=30.0),
    "claude-opus-5": PricingEntry(input_per_mtok=15.0, output_per_mtok=75.0),
    "claude-haiku-4-5": PricingEntry(input_per_mtok=1.0, output_per_mtok=5.0),
}


class DuetConfig(BaseModel):
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    debate: DebateConfig = Field(default_factory=DebateConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    pricing: dict[str, PricingEntry] = Field(
        default_factory=lambda: dict(DEFAULT_PRICING)
    )

    def pricing_for(self, model_id: str) -> PricingEntry | None:
        return self.pricing.get(model_id)


def duet_home() -> Path:
    return Path(os.environ.get("DUET_HOME", Path.home() / ".duet"))


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(
    path: Path | None = None, profile: str | None = None
) -> DuetConfig:
    """Load config: defaults <- ./duet.toml (or `path`) <- profile overlay."""
    data: dict = {}
    candidate = path or Path("duet.toml")
    if candidate.is_file():
        data = tomllib.loads(candidate.read_text())
    if profile:
        profile_path = duet_home() / "profiles" / f"{profile}.toml"
        if not profile_path.is_file():
            raise FileNotFoundError(f"profile not found: {profile_path}")
        data = _deep_merge(data, tomllib.loads(profile_path.read_text()))
    # User pricing entries overlay defaults rather than replacing the table.
    if "pricing" in data:
        merged = {k: v.model_dump() for k, v in DEFAULT_PRICING.items()}
        data["pricing"] = _deep_merge(merged, data["pricing"])
    return DuetConfig.model_validate(data)
