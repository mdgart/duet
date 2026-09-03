"""Document templates — the shape a debate's output takes (SPECS §4.2).

Duet debates more than software. A decision, a strategy, a product design and an
implementation spec want different sections, and forcing all of them through a
software skeleton — "Data model", "External dependencies" — is what makes a
non-code debate feel like it is fighting the format.

A template is a named set of sections. The debate's structured-output model is
generated from it, so the proposer fills exactly the sections that suit the
idea, and everything downstream reads the sections generically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import hashlib

from pydantic import BaseModel, Field, create_model

# Guidance shared by every template, kept here so a generated model and the
# hand-read software spec cannot drift apart.
SUMMARY_GUIDANCE = (
    "Three to five sentences a decision-maker reads in under a minute: the problem, "
    "the approach, and the one or two choices that would most change the result if "
    "they were wrong. Plain prose, no bullet lists — the rest of the document is the detail."
)
ADDRESSES_GUIDANCE = (
    'Objection ids from the ledger that this revision fixes, e.g. ["OBJ-2"]. Ids only, '
    "empty on a first draft."
)


@dataclass(frozen=True)
class Section:
    id: str        # a valid identifier — becomes the model field name
    heading: str   # the markdown heading a reader sees
    guidance: str  # what belongs here; reaches the model as the field description


@dataclass(frozen=True)
class Template:
    name: str          # selector, e.g. "software"
    label: str         # one-line human label
    noun: str          # what the document is called in prompts, e.g. "implementation spec"
    when: str          # when to choose this, for the picker and --template help
    sections: tuple[Section, ...]

    def headings(self) -> tuple[str, ...]:
        return tuple(s.heading for s in self.sections)

    def pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple((s.id, s.heading) for s in self.sections)


class DraftBase(BaseModel):
    """Common shape for every generated draft: a title, a human summary, the
    objections this revision closes, and template-specific sections."""

    section_pairs: ClassVar[tuple[tuple[str, str], ...]] = ()

    title: str
    summary: str = Field(default="", description=SUMMARY_GUIDANCE)
    addresses: list[str] = Field(default_factory=list, description=ADDRESSES_GUIDANCE)

    def to_markdown(self) -> str:
        parts = [f"# {self.title}", ""]
        if self.summary.strip():
            parts += ["## In short", "", self.summary.strip(), ""]
        for field, heading in self.section_pairs:
            parts += [f"## {heading}", "", str(getattr(self, field)).strip(), ""]
        return "\n".join(parts).rstrip() + "\n"

    def content_hash(self) -> str:
        """Pins the exact text a verdict was cast against (SPECS §4.3)."""
        return hashlib.sha256(self.to_markdown().encode()).hexdigest()[:12]


_MODEL_CACHE: dict[str, type[DraftBase]] = {}


def draft_model_for(template: Template) -> type[DraftBase]:
    """The structured-output model for a template — one required field per
    section, generated once and cached so identity comparisons hold."""
    if template.name in _MODEL_CACHE:
        return _MODEL_CACHE[template.name]
    fields = {
        s.id: (str, Field(description=s.guidance) if s.guidance else (str, ...))
        for s in template.sections
    }
    # Field(description=...) needs the (type, Field) tuple form; a bare required
    # field is (type, ...). Normalise.
    fields = {
        s.id: ((str, Field(description=s.guidance)) if s.guidance else (str, ...))
        for s in template.sections
    }
    model = create_model(f"Draft_{template.name.title()}", __base__=DraftBase, **fields)
    model.section_pairs = template.pairs()
    _MODEL_CACHE[template.name] = model
    return model


SOFTWARE = Template(
    name="software",
    label="Implementation spec",
    noun="implementation spec",
    when="A software system to be built — a tool, service, library, or feature.",
    sections=(
        # Guidance left empty: this shape is golden-suite-validated as bare
        # fields, and changing the schema it sends is a prompt change.
        Section("problem_statement", "Problem statement", ""),
        Section("goals_non_goals", "Goals / Non-goals", ""),
        Section("user_facing_behavior", "User-facing behavior", ""),
        Section("architecture", "Architecture & key components", ""),
        Section("data_model", "Data model", ""),
        Section("external_dependencies", "External dependencies", ""),
        Section("milestones", "Task-level milestones", ""),
        Section("acceptance_criteria", "Acceptance criteria", ""),
        Section("open_questions", "Open questions", ""),
    ),
)

DECISION = Template(
    name="decision",
    label="Decision record",
    noun="decision record",
    when="A choice to be made and justified — should we do X, and which way.",
    sections=(
        Section("context", "Context", "The situation that forces a decision, and what is at stake."),
        Section("options", "Options considered", "The real alternatives, each with a line on why it is viable — including doing nothing."),
        Section("recommendation", "Recommendation", "The option chosen, stated plainly in one or two sentences."),
        Section("rationale", "Rationale", "Why this option over the others; the reasoning a reader could disagree with."),
        Section("consequences", "Consequences & tradeoffs", "What choosing this commits you to, including what you give up."),
        Section("risks", "Risks & mitigations", "What could make this the wrong call, and how each risk is bounded."),
        Section("open_questions", "Open questions", "What remains genuinely undecided, marked for the human."),
    ),
)

DESIGN = Template(
    name="design",
    label="Design brief",
    noun="design brief",
    when="A system, product, or process to design that is not (only) software.",
    sections=(
        Section("problem", "Problem", "What is wrong today and for whom; why it is worth solving now."),
        Section("goals_non_goals", "Goals / Non-goals", "What success requires, and what is explicitly out of scope."),
        Section("approach", "Approach", "The shape of the solution — the central idea, not the detail."),
        Section("components", "Key components", "The moving parts and how they fit together."),
        Section("risks", "Risks & unknowns", "What could break the approach, and what is not yet known."),
        Section("success", "How we would know it worked", "The observable signals that would show this succeeded."),
        Section("open_questions", "Open questions", "What remains genuinely undecided, marked for the human."),
    ),
)

TEMPLATES: dict[str, Template] = {t.name: t for t in (SOFTWARE, DECISION, DESIGN)}
DEFAULT_TEMPLATE = "software"


def get_template(name: str) -> Template:
    if name not in TEMPLATES:
        raise ValueError(f"unknown template {name!r}; available: {', '.join(TEMPLATES)}")
    return TEMPLATES[name]


def all_headings() -> tuple[str, ...]:
    """Every heading across every template, for section normalisation. Shared
    headings (Open questions, Goals / Non-goals) collapse to one, which is
    correct — they mean the same thing whichever template is in play."""
    seen: dict[str, None] = {}
    for template in TEMPLATES.values():
        for heading in template.headings():
            seen.setdefault(heading, None)
    return tuple(seen)
