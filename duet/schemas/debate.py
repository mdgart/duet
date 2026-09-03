"""Phase A schemas: spec drafts, verdicts, and the objection ledger (SPECS §4)."""

from __future__ import annotations

import hashlib
import re
import textwrap
from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["blocking", "major", "minor"]
VerdictKind = Literal["AGREE", "REVISE", "OBJECT"]
SEVERITY_RANK: dict[str, int] = {"minor": 0, "major": 1, "blocking": 2}

# SPECS §4.2 — the fixed skeleton every draft must fill.
SPEC_SECTIONS: tuple[tuple[str, str], ...] = (
    ("problem_statement", "Problem statement"),
    ("goals_non_goals", "Goals / Non-goals"),
    ("user_facing_behavior", "User-facing behavior"),
    ("architecture", "Architecture & key components"),
    ("data_model", "Data model"),
    ("external_dependencies", "External dependencies"),
    ("milestones", "Task-level milestones"),
    ("acceptance_criteria", "Acceptance criteria"),
    ("open_questions", "Open questions"),
)


class SpecDraft(BaseModel):
    """A full implementation spec. Every section is required, so a proposer
    cannot quietly drop the parts it finds hard."""

    title: str
    summary: str = Field(
        default="",
        description=(
            "Three to five sentences a decision-maker reads in under a minute: the "
            "problem being solved, the approach taken, and the one or two choices "
            "that would most change the result if they were wrong. Plain prose, no "
            "implementation detail, no bullet lists — the rest of the document is "
            "the detail."
        ),
    )
    """The human-facing half of the document.

    The spec proper is written for whoever builds it and runs to tens of
    thousands of characters; nobody approves that at a gate. This is what the
    human actually reads before deciding, written by the participant that wrote
    the spec, so it stays current with every revision at no extra cost.
    """

    addresses: list[str] = Field(
        default_factory=list,
        description=(
            "Objection ids from the ledger that this revision fixes, e.g. "
            '["OBJ-2", "OBJ-5"]. Ids only — no prose, no summaries. Empty list '
            "on a first draft, when no objections exist yet."
        ),
    )
    """Objection ids this revision claims to fix. A proposer can only close
    objections it raised itself (see ObjectionLedger.close_for) — claiming to
    have fixed someone else's is for them to judge, not the author.

    The description above reaches the model: structured-output modes require
    every field to be emitted, so an unexplained field gets filled with
    whatever the model guesses it means.
    """

    problem_statement: str
    goals_non_goals: str
    user_facing_behavior: str
    architecture: str
    data_model: str
    external_dependencies: str
    milestones: str
    acceptance_criteria: str
    open_questions: str

    def to_markdown(self) -> str:
        parts = [f"# {self.title}", ""]
        if self.summary.strip():
            parts += ["## In short", "", self.summary.strip(), ""]
        for field, heading in SPEC_SECTIONS:
            parts += [f"## {heading}", "", getattr(self, field).strip(), ""]
        return "\n".join(parts).rstrip() + "\n"

    def content_hash(self) -> str:
        """Pins the exact text a verdict was cast against (SPECS §4.3)."""
        return hashlib.sha256(self.to_markdown().encode()).hexdigest()[:12]


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


CANONICAL_SECTIONS = tuple(heading for _, heading in SPEC_SECTIONS)


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
