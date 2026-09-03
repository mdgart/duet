"""Document templates: a debate produces the shape that suits the idea, not
always a software spec (SPECS §4.2)."""

from pathlib import Path

import pytest

from tests.conftest import FakeProvider, agrees, objects, parsed, roster

from duet.orchestrator.debate import DebateEngine
from duet.orchestrator.machine import Machine
from duet.schemas.templates import draft_model_for, get_template
from duet.store.session import SessionStore


def draft(template: str, title: str, addresses=None):
    """A complete draft for any template — every section filled."""
    model = draft_model_for(get_template(template))
    body = {s.id: f"{title} — {s.heading}" for s in get_template(template).sections}
    return model(title=title, summary=f"{title} in short.", addresses=addresses or [], **body)


def test_every_template_generates_a_usable_model():
    for name in ("software", "decision", "design"):
        d = draft(name, "X")
        md = d.to_markdown()
        for section in get_template(name).sections:
            assert f"## {section.heading}" in md
        assert d.content_hash()  # hashable, so votes can pin it


def test_a_decision_debate_converges_and_writes_a_decision_record(tmp_path: Path):
    cfg = roster("sol", "opus")
    store = SessionStore.new(root=tmp_path)
    providers = {
        "sol": FakeProvider("anthropic", "m", [parsed(o) for o in (
            draft("decision", "v1"),
            objects("Options considered", "no do-nothing option"),
            draft("decision", "v3", addresses=["OBJ-2"]))]),
        "opus": FakeProvider("anthropic", "m", [parsed(o) for o in (
            objects("Rationale", "reasoning is thin"),
            draft("decision", "v2", addresses=["OBJ-1"]),
            agrees())]),
    }
    machine = Machine(cfg, store, providers)
    result = machine.start("should we adopt X", template="decision")

    assert result.state == "GATE_1"
    spec = (store.dir / "artifacts" / "IMPLEMENTATION.md").read_text()
    assert "## Recommendation" in spec and "## Consequences & tradeoffs" in spec
    assert "## Data model" not in spec  # not a software spec


def test_the_template_is_recorded_and_replayed(tmp_path: Path):
    cfg = roster("sol", "opus")
    store = SessionStore.new(root=tmp_path)
    providers = {
        "sol": FakeProvider("a", "m", [parsed(draft("design", "v1"))]),
        "opus": FakeProvider("a", "m", [parsed(objects("Approach", "x"))]),
    }
    cfg.debate.max_rounds = 1
    Machine(cfg, store, providers).start("a process to design", template="design")

    reopened = Machine(cfg, SessionStore(store.session_id, root=tmp_path), {})
    assert reopened.template() == "design"


def test_an_unpicked_template_defaults_to_software(tmp_path: Path):
    store = SessionStore.new(root=tmp_path)
    store.append("state_change", {"to": "DESIGN_DEBATE", "brief": "x"})
    assert Machine(roster("a", "b"), store, {}).template() == "software"


def test_the_critic_sees_the_document_noun(tmp_path: Path):
    cfg = roster("sol", "opus")
    cfg.debate.max_rounds = 1
    store = SessionStore.new(root=tmp_path)
    providers = {
        "sol": FakeProvider("a", "m", [parsed(draft("decision", "v1"))]),
        "opus": FakeProvider("a", "m", [parsed(objects("Context", "x"))]),
    }
    Machine(cfg, store, providers).start("a choice", template="decision")

    critic_system = providers["opus"].requests[0].system
    assert "decision record" in critic_system and "implementation spec" not in critic_system
