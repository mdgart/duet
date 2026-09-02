"""Reference material reaches every model at every stage (PLAN §6, SPECS §4.2).

Context is not the brief. The brief is what to build and the debate revises it;
context is what is already true, and the loop must never argue with it or
rewrite it.
"""

from pathlib import Path

import pytest

from tests.conftest import FakeProvider, agrees, objects, parsed, roster, spec

from duet.orchestrator.debate import ContextTooLarge, DebateEngine
from duet.orchestrator.machine import Machine, submit_gate_decision
from duet.store.session import SessionStore

CONTEXT = "We already run Postgres 16. Do not introduce a second datastore."

SCRIPT = {
    "sol": [spec("v1"), objects("Data model", "no pagination"), spec("v3", addresses=["OBJ-2"])],
    "opus": [objects("Architecture", "no cache"), spec("v2", addresses=["OBJ-1"]), agrees()],
}


def machine_for(cfg, tmp_path: Path, scripts=SCRIPT):
    store = SessionStore.new(root=tmp_path)
    providers = {
        pid: FakeProvider("anthropic", f"model-{pid}", [parsed(o) for o in script])
        for pid, script in scripts.items()
    }
    return Machine(cfg, store, providers), store, providers


def test_context_reaches_proposers_and_critics_alike(tmp_path: Path):
    """A critic that cannot see the constraint objects to it — the failure this
    feature exists to prevent."""
    machine, _, providers = machine_for(roster("sol", "opus"), tmp_path)
    machine.start("a link-saving app", context=CONTEXT)

    for pid, provider in providers.items():
        assert provider.requests, f"{pid} was never called"
        for request in provider.requests:
            body = request.messages[0].content
            assert CONTEXT in body, f"{pid} did not receive the context"
            assert "not open to objection" in body


def test_context_is_stored_as_an_artifact_and_replayed(tmp_path: Path):
    cfg = roster("sol", "opus")
    machine, store, _ = machine_for(cfg, tmp_path)
    machine.start("an app", context=CONTEXT)

    assert (store.dir / "artifacts" / "CONTEXT.md").read_text() == CONTEXT
    reopened = Machine(cfg, SessionStore(store.session_id, root=tmp_path), {})
    assert reopened.context() == CONTEXT


def test_a_gate_1_revision_rewrites_the_brief_but_never_the_context(tmp_path: Path):
    """The brief is the proposal and moves; context is the situation and does not."""
    cfg = roster("sol", "opus")
    scripts = {"sol": SCRIPT["sol"] + [agrees()], "opus": SCRIPT["opus"] + [spec("v4")]}
    machine, store, _ = machine_for(cfg, tmp_path, scripts)
    machine.start("an app", context=CONTEXT)
    submit_gate_decision(store, action="revise", actor="mauro", feedback="must work offline")
    machine.advance()

    assert "FIXED: must work offline" in machine.brief()
    assert machine.context() == CONTEXT


def test_oversized_context_is_refused_before_any_spend(tmp_path: Path):
    """Refused, not truncated: acting on half a constraint is worse than none,
    and it is re-sent on every turn so it multiplies across the run."""
    cfg = roster("sol", "opus")
    cfg.build.context_budget_tokens = 1000
    machine, store, providers = machine_for(cfg, tmp_path)

    with pytest.raises(ContextTooLarge, match="re-sent every turn"):
        machine.start("an app", context="x" * 4000)
    assert not any(p.requests for p in providers.values()), "spent money before refusing"
    assert store.cost_usd == 0.0


def test_context_within_budget_is_accepted(tmp_path: Path):
    cfg = roster("sol", "opus")
    cfg.build.context_budget_tokens = 1000
    machine, _, _ = machine_for(cfg, tmp_path)
    result = machine.start("an app", context="x" * 1900)  # under half the budget
    assert result.state == "GATE_1"


def test_no_context_is_the_ordinary_case(tmp_path: Path):
    machine, store, _ = machine_for(roster("sol", "opus"), tmp_path)
    result = machine.start("an app")
    assert result.state == "GATE_1"
    assert machine.context() == ""
    assert not (store.dir / "artifacts" / "CONTEXT.md").exists()


# --- loading (CLI side) ------------------------------------------------------


def test_several_context_files_combine_under_their_names(tmp_path: Path, monkeypatch):
    """Named blocks, not one anonymous blob: a model that must weigh a
    convention against an architecture note needs to know which is which."""
    from duet import cli

    arch = tmp_path / "arch.md"
    arch.write_text("Postgres 16 is already deployed.")
    conventions = tmp_path / "conventions.md"
    conventions.write_text("We use pytest, never unittest.")

    combined = cli._load_context([arch, conventions])

    assert "## arch.md" in combined and "## conventions.md" in combined
    assert combined.index("## arch.md") < combined.index("## conventions.md")  # order kept
    assert "Postgres 16" in combined and "pytest" in combined


def test_a_missing_context_file_aborts_rather_than_being_skipped(tmp_path: Path):
    """Silently dropping a file the human asked for produces a debate that
    ignores a constraint they believe it has."""
    import typer

    from duet import cli

    present = tmp_path / "there.md"
    present.write_text("real")
    with pytest.raises(typer.Exit):
        cli._load_context([present, tmp_path / "absent.md"])


def test_an_explicit_path_overrides_the_default_file(tmp_path: Path, monkeypatch):
    from duet import cli

    default = tmp_path / "CONTEXT.md"
    default.write_text("the default one")
    explicit = tmp_path / "other.md"
    explicit.write_text("the chosen one")
    monkeypatch.chdir(tmp_path)

    assert "the default one" in cli._load_context([])
    assert cli._load_context([explicit]) == "## other.md\n\nthe chosen one"
