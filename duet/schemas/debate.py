"""Phase A schemas: spec drafts, verdicts, and the objection ledger (SPECS §4)."""

from __future__ import annotations

import re
import textwrap
from typing import Literal

from pydantic import BaseModel, Field

from duet.schemas.templates import (  # noqa: F401 — re-exported
    DEFAULT_TEMPLATE,
    TEMPLATES,
    DraftBase,
    Section,
    Template,
    all_headings,
    draft_model_for,
    get_template,
)

Severity = Literal["blocking", "major", "minor"]
VerdictKind = Literal["AGREE", "REVISE", "OBJECT"]
SEVERITY_RANK: dict[str, int] = {"minor": 0, "major": 1, "blocking": 2}

# The software template's generated model. Kept under its familiar name because
# it is the default shape and what the contract and golden suites exercise; a
# debate uses whatever template its session selected (draft_model_for).
SpecDraft = draft_model_for(get_template("software"))
SPEC_SECTIONS = get_template("software").pairs()


class Objection(BaseModel):
    # No id field: canonical OBJ-n ids are assigned by the ledger, so asking a
    # model to invent one only produces numbers the orchestrator discards.
    severity: Severity
    headline: str = Field(
        default="",
        description=(
            "The objection in one line, at most about twelve words — what is wrong, "
            "not the full argument. This is what a human scans when ten are open; the "
            "claim below is the detail they read when one matters."
        ),
    )
    section: str
    claim: str
    evidence: str
    proposed_change: str
    reopens: str = Field(
        default="",
        description=(
            "If this restates an objection already in the ledger, put its id here "
            '(e.g. "OBJ-3"). Empty if the point is new. Say so even when you have '
            "reworded it — the same concern raised under new wording is a reopen."
        ),
    )
    """Which ledger entry this restates, as judged by the critic.

    Text matching cannot do this. In an observed debate the same concern was
    raised in all eight rounds with a median claim similarity of 0.07: the
    models restate an issue in genuinely different words each time, so no
    threshold separates a reopen from a new point. The critic has the ledger in
    context and knows; the orchestrator verifies the id exists and does the
    counting, so this informs the ledger without being trusted blindly.
    """


class WaivedObjection(BaseModel):
    id: str
    reason: str


class Verdict(BaseModel):
    verdict: VerdictKind
    objections: list[Objection] = Field(default_factory=list)
    resolved_objections: list[str] = Field(default_factory=list)
    waived_objections: list[WaivedObjection] = Field(default_factory=list)
    agreement_score: float = Field(default=0.0, ge=0.0, le=1.0)


CANONICAL_SECTIONS = all_headings()


def canonical_section(section: str) -> str:
    """Map a model's section label onto one of the fixed skeleton headings.

    Critics compose labels freely — "Data model — tags storage", "Data model /
    tags storage", "Data model — tags storage, User-facing behavior — add
    command" all appeared in one debate for the same concern. Left raw, each
    variant becomes a different dedup key and the same objection is recorded as
    a new one every round.
    """
    lowered = section.lower()
    # Leftmost wins, not first-in-skeleton: a composite label like "Data model
    # — tags storage, User-facing behavior — add command" leads with the
    # section it is really about.
    hits = [(lowered.index(h.lower()), h) for h in CANONICAL_SECTIONS if h.lower() in lowered]
    return min(hits)[1] if hits else section.strip()


def normalize_claim(section: str, claim: str) -> str:
    """Dedup key for corroboration (SPECS §4.1).

    Deliberately crude and deterministic: lowercase, punctuation stripped,
    whitespace collapsed. The orchestrator must never call a model to decide
    whether two objections are the same one.
    """
    text = f"{canonical_section(section)} :: {claim}".lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class LedgerEntry(BaseModel):
    """One objection tracked across rounds, with who raised it."""

    id: str
    severity: Severity
    headline: str = ""
    section: str
    claim: str
    evidence: str
    proposed_change: str
    raised_by: list[str] = Field(default_factory=list)
    closed_by: dict[str, Literal["resolved", "waived"]] = Field(default_factory=dict)
    resolution: str = ""
    first_round: int = 0
    reopen_count: int = 0

    @property
    def status(self) -> Literal["open", "resolved", "waived"]:
        """An objection several participants raised closes only when all of
        them agree it is closed; one holdout keeps it open. Waiving is weaker
        than resolving, so a mixed close is reported as waived."""
        if not self.raised_by or any(p not in self.closed_by for p in self.raised_by):
            return "open"
        return "waived" if "waived" in self.closed_by.values() else "resolved"

    @property
    def one_line(self) -> str:
        """A scannable summary: the critic's headline, or a trimmed claim for
        entries logged before headlines existed."""
        if self.headline.strip():
            return self.headline.strip()
        return textwrap.shorten(self.claim, 76, placeholder="…")

    @property
    def key(self) -> str:
        return normalize_claim(self.section, self.claim)

    @property
    def corroborated(self) -> bool:
        """Raised independently by more than one participant — the signal a
        cross-lab roster exists to produce (SPECS §4.1)."""
        return len(self.raised_by) > 1

    @property
    def blocking_or_major(self) -> bool:
        return self.severity in ("blocking", "major")
