"""Configuration schema and loader for duet.toml (SPECS §9)."""

from __future__ import annotations

import math
import os

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]
from pathlib import Path
from typing import Iterator, Literal, Sequence

from pydantic import BaseModel, Field, model_validator


class ModelSpec(BaseModel):
    """A provider adapter plus the model id to call on it."""

    provider: str
    model: str


class Participant(ModelSpec):
    """One voice in the loop, addressed by id everywhere else in the system.

    Ids — not provider names — identify who spoke, so two participants may
    share a provider on different models (or the same model at different
    temperatures) without the log or the role assignment becoming ambiguous.
    """

    id: str
    badge: str = ""
    color: str = "white"

    @model_validator(mode="after")
    def _default_badge(self) -> "Participant":
        if not self.badge:
            self.badge = self.id.upper()
        return self


# Cross-lab, but the small model from each lab. Decorrelated blind spots are the
# premise and survive the downgrade; a flagship pair does not survive an
# accident — an unconverged 7-round debate on them cost $12 where the same idea
# cost $0.41 here. Flagships are one flag away (`--profile flagship`), which is
# the right way round: expensive should be chosen, not defaulted into.
DEFAULT_ROSTER: list[Participant] = [
    Participant(id="mini", provider="openai", model="gpt-5.4-mini", badge="MINI", color="cyan"),
    Participant(
        id="haiku",
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        badge="HAIKU",
        color="dark_orange",
    ),
]


class Roster:
    """A sequence of participants. Two is the default, not a structural limit."""

    def __init__(self, participants: Sequence[Participant]):
        self._participants: list[Participant] = list(participants)

    def __len__(self) -> int:
        return len(self._participants)

    def __iter__(self) -> Iterator[Participant]:
        return iter(self._participants)

    def __getitem__(self, index: int) -> Participant:
        return self._participants[index]

    @property
    def ids(self) -> list[str]:
        return [p.id for p in self._participants]

    def get(self, participant_id: str) -> Participant:
        for p in self._participants:
            if p.id == participant_id:
                return p
        raise ValueError(f"no participant {participant_id!r} in roster {self.ids}")

    def index_of(self, participant_id: str) -> int:
        return self.ids.index(self.get(participant_id).id)

    def others(self, participant_id: str) -> list[Participant]:
        """Everyone except one — the cross-validation primitive."""
        self.get(participant_id)
        return [p for p in self._participants if p.id != participant_id]

    def proposer_for_round(self, round_number: int) -> Participant:
        """Round-robin proposer, 1-based round numbers (SPECS §4.1).

        With two participants this is exactly the alternating propose/critique
        protocol; with N it rotates and the remaining N-1 critique.
        """
        if round_number < 1:
            raise ValueError("round_number is 1-based")
        return self._participants[(round_number - 1) % len(self._participants)]


class DebateConfig(BaseModel):
    max_rounds: int = 8
    min_objection_rounds: int = 2
    reopen_limit: int = 3
    stall_rounds: int = 4
    """Rounds of no progress before the debate is called stuck. Progress means
    driving open blocking/major objections to a new low; a debate that cannot
    do that in this many rounds is generating objections as fast as it closes
    them, and the round cap is an expensive way to find that out."""
    consensus: Literal["unanimous", "quorum"] = "unanimous"
    quorum: float = Field(default=1.0, gt=0.0, le=1.0)
    max_output_tokens: int = 32768
    """Per-turn output cap. Every proposer turn re-emits the whole spec, and
    JSON-escaped markdown is token-hungry, so this needs real headroom: a
    flagship model overran 16k on a round-2 revision. Overshooting costs a
    whole turn, so err high — you are billed for what is generated, not for
    the ceiling."""

    def required_agreements(self, n_participants: int) -> int:
        """How many AGREE verdicts on one draft hash close the debate."""
        if self.consensus == "unanimous":
            return n_participants
        return min(n_participants, max(2, math.ceil(self.quorum * n_participants)))


class BuildConfig(BaseModel):
    coder: str | None = None  # participant id; None -> first in roster
    reviewers: list[str] | None = None  # None -> every participant but the coder
    alternate_roles: bool = False
    max_review_cycles: int = 6
    max_tool_calls_per_turn: int = 25
    context_budget_tokens: int = 60_000


class BudgetConfig(BaseModel):
    max_usd: float = 5.0
    """A guard, not a formality. A converged debate costs cents on the default
    roster and a dollar or two on flagships, so 25 was high enough to let a
    runaway reach $12 unchallenged. Hitting this suspends the session; raise it
    and resume to continue."""
    max_wall_minutes: int = 90


class SandboxConfig(BaseModel):
    runner: Literal["docker", "local-unsafe"] = "docker"
    network: Literal["none", "allowlist"] = "none"
    network_allowlist: list[str] = Field(
        default_factory=lambda: ["pypi.org", "registry.npmjs.org"]
    )


class UtilityConfig(BaseModel):
    """Non-debate helper models (repo mapping, deterministic summarisation)."""

    mapper: ModelSpec = Field(
        default_factory=lambda: ModelSpec(provider="anthropic", model="claude-haiku-4-5")
    )


class PricingEntry(BaseModel):
    """USD per million tokens."""

    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float | None = None
    """Rate for input tokens served from the provider's prompt cache.

    Left unset the meter charges cached tokens at full price, which overstates
    spend — measured at ~13% high against a real invoice, because a debate
    re-sends the system prompt, brief and draft every turn and providers cache
    that automatically. Overstating is the safe direction for something whose
    job is to stop a runaway, so this is opt-in: set it to bill accurately.
    """


# Defaults are best-known published prices; override in [pricing] of duet.toml.
# Dated model ids are listed alongside their aliases: an id absent from this
# table is treated as free, which silently disables the budget cap for it.
DEFAULT_PRICING: dict[str, PricingEntry] = {
    "gpt-5.6-sol": PricingEntry(input_per_mtok=5.0, output_per_mtok=30.0),
    "claude-opus-5": PricingEntry(input_per_mtok=15.0, output_per_mtok=75.0),
    "claude-haiku-4-5": PricingEntry(input_per_mtok=1.0, output_per_mtok=5.0),
    "claude-haiku-4-5-20251001": PricingEntry(input_per_mtok=1.0, output_per_mtok=5.0),
    # Estimate. The default roster must be priced or the budget cap is inert,
    # which is the failure this table exists to prevent; correct it if the real
    # figure differs.
    "gpt-5.4-mini": PricingEntry(input_per_mtok=0.25, output_per_mtok=2.0),
}


class DuetConfig(BaseModel):
    participants: list[Participant] = Field(
        default_factory=lambda: [p.model_copy(deep=True) for p in DEFAULT_ROSTER]
    )
    debate: DebateConfig = Field(default_factory=DebateConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    utility: UtilityConfig = Field(default_factory=UtilityConfig)
    pricing: dict[str, PricingEntry] = Field(
        default_factory=lambda: dict(DEFAULT_PRICING)
    )

    @model_validator(mode="after")
    def _check_roster(self) -> "DuetConfig":
        ids = [p.id for p in self.participants]
        if len(ids) < 2:
            raise ValueError("at least two participants are required")
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate participant ids: {duplicates}")
        if self.build.coder is not None and self.build.coder not in ids:
            raise ValueError(f"build.coder {self.build.coder!r} is not one of {ids}")
        for reviewer in self.build.reviewers or []:
            if reviewer not in ids:
                raise ValueError(f"build.reviewers entry {reviewer!r} is not one of {ids}")
        return self

    @property
    def roster(self) -> Roster:
        return Roster(self.participants)

    def roles_for_task(self, task_index: int = 0) -> tuple[Participant, list[Participant]]:
        """Resolve (coder, reviewers) for a task (SPECS §5.6).

        Reviewers default to every other participant — the N-way generalisation
        of "the other model reviews". With alternate_roles the coder rotates
        through the roster per task.
        """
        roster = self.roster
        base = roster.index_of(self.build.coder) if self.build.coder else 0
        offset = task_index if self.build.alternate_roles else 0
        coder = roster[(base + offset) % len(roster)]
        if self.build.reviewers is None:
            reviewers = roster.others(coder.id)
        else:
            reviewers = [roster.get(r) for r in self.build.reviewers if r != coder.id]
        if not reviewers:
            raise ValueError(
                f"task {task_index} leaves coder {coder.id!r} with no reviewer; "
                f"widen build.reviewers or disable build.alternate_roles"
            )
        return coder, reviewers

    def pricing_for(self, model_id: str) -> PricingEntry | None:
        return self.pricing.get(model_id)

    def unpriced_participants(self) -> list[Participant]:
        """Roster members with no price, and therefore no cost accounting.

        An unpriced model accrues $0.00 however much it spends, so
        `budget.max_usd` can never trip for it — the runaway-cost guard is
        silently inert rather than merely approximate. Worth saying out loud
        before a run, not discovering on an invoice.
        """
        return [p for p in self.participants if self.pricing_for(p.model) is None]


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
    """Load config: defaults <- ./duet.toml (or `path`) <- profile overlay.

    A file or profile that declares [[participants]] replaces the roster
    wholesale rather than merging entry-by-entry, so a profile can swap in a
    different set of models without inheriting stale ones.
    """
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
