"""The CLI as a gate client: it renders and answers gates, it never drives."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import FakeProvider, agrees, objects, parsed, spec

from duet import cli
from duet.schemas.config import DuetConfig, Participant
from duet.store.session import SessionStore

runner = CliRunner()

SCRIPT = {
    "sol": [spec("v1"), objects("Data model", "no pagination"), spec("v3", addresses=["OBJ-2"])],
    "opus": [objects("Architecture", "no cache"), spec("v2", addresses=["OBJ-1"]), agrees()],
}


def stub(monkeypatch, tmp_path: Path):
    cfg = DuetConfig(participants=[
        Participant(id="sol", provider="openai", model="model-sol"),
        Participant(id="opus", provider="anthropic", model="model-opus"),
    ])
    monkeypatch.setenv("DUET_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(
        cli, "providers_for",
        lambda c: {pid: FakeProvider("fake", f"model-{pid}", [parsed(o) for o in script])
                   for pid, script in SCRIPT.items()},
    )
    return cfg


def test_new_debates_then_approves_at_the_gate(monkeypatch, tmp_path: Path):
    stub(monkeypatch, tmp_path)
    result = runner.invoke(cli.app, ["new", "a link-saving app"], input="approve\n")

    assert result.exit_code == 0, result.output
    assert "OBJECT" in result.output and "AGREE" in result.output
    assert "DESIGN_DEBATE → GATE_1" in result.output
    assert "DONE" in result.output
    # The gate shows the decision, not the build document: a summary and what
    # the debate cost and settled.
    assert "In short" in result.output
    assert "objections settled" in result.output
    assert "Problem statement" not in result.output, "the full spec should need `view`"


def test_new_accepts_a_brief_file(monkeypatch, tmp_path: Path):
    stub(monkeypatch, tmp_path)
    brief = tmp_path / "brief.md"
    brief.write_text("a link-saving app, offline first")
    result = runner.invoke(cli.app, ["new", f"@{brief}"], input="reject\n")
    assert result.exit_code == 0, result.output
    assert "FAILED" in result.output


def test_resume_picks_up_a_suspended_session(monkeypatch, tmp_path: Path):
    stub(monkeypatch, tmp_path)
    # Abandon the first run at the gate by sending no decision.
    started = runner.invoke(cli.app, ["new", "an app"], input="")
    session_id = started.output.split("session ")[1].split()[0]

    # A second invocation resumes from the log with exhausted scripts, proving
    # the debate is replayed rather than re-run.
    monkeypatch.setattr(cli, "providers_for", lambda c: {"sol": FakeProvider("f", "m", []),
                                                         "opus": FakeProvider("f", "m", [])})
    result = runner.invoke(cli.app, ["resume", session_id], input="approve\n")
    assert result.exit_code == 0, result.output
    assert "resuming" in result.output and "GATE_1" in result.output
    assert "DONE" in result.output


def test_edit_at_the_gate_makes_the_edited_spec_canonical(monkeypatch, tmp_path: Path):
    stub(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_edit_text", lambda text: text + "\n\nFIXED: must work offline\n")

    result = runner.invoke(cli.app, ["new", "an app"], input="edit\n")

    assert result.exit_code == 0, result.output
    assert "DONE" in result.output
    session = result.output.split("session ")[1].split()[0]
    spec = (tmp_path / "sessions" / session / "artifacts" / "IMPLEMENTATION.md").read_text()
    assert spec.endswith("FIXED: must work offline\n")


def test_edit_text_round_trips_through_the_editor(monkeypatch, tmp_path: Path):
    """$EDITOR is invoked on a temp file and whatever it saves comes back."""
    seen = {}

    def fake_editor(argv):
        seen["argv"] = argv
        Path(argv[-1]).write_text(Path(argv[-1]).read_text() + " EDITED")
        return 0

    monkeypatch.setenv("EDITOR", "my-editor --wait")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(cli.subprocess, "call", fake_editor)

    assert cli._edit_text("original") == "original EDITED"
    assert seen["argv"][:2] == ["my-editor", "--wait"]


def test_a_failed_editor_does_not_silently_discard_the_spec(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "call", lambda argv: 1)
    with pytest.raises(RuntimeError, match="edit abandoned"):
        cli._edit_text("original")


def test_a_spec_containing_brackets_is_shown_intact(monkeypatch, tmp_path: Path):
    """Rich reads square brackets as its own markup and drops what looks like a
    style tag — silently. At a gate that means approving a spec whose displayed
    text differs from the stored text, so the renderer must not interpret it."""
    stub(monkeypatch, tmp_path)
    store = SessionStore.new(root=tmp_path / "sessions")
    ref = store.save_artifact("IMPLEMENTATION.md", "# Spec\n\nStore ids as list[str] values.\n")

    with cli.console.capture() as captured:
        cli._show_artifact(store, ref)

    assert "list[str]" in captured.get()


def test_sessions_lists_the_run_with_its_owner(monkeypatch, tmp_path: Path):
    stub(monkeypatch, tmp_path)
    runner.invoke(cli.app, ["new", "an app", "--actor", "mauro"], input="approve\n")
    result = runner.invoke(cli.app, ["sessions"])
    assert result.exit_code == 0
    assert "DONE" in result.output and "mauro" in result.output


def test_resume_accepts_an_id_prefix(monkeypatch, tmp_path: Path):
    stub(monkeypatch, tmp_path)
    started = runner.invoke(cli.app, ["new", "an app"], input="")
    session_id = started.output.split("session ")[1].split()[0]

    monkeypatch.setattr(cli, "providers_for", lambda c: {"sol": FakeProvider("f", "m", []),
                                                         "opus": FakeProvider("f", "m", [])})
    result = runner.invoke(cli.app, ["resume", session_id[:8]], input="approve\n")
    assert result.exit_code == 0, result.output
    assert "DONE" in result.output


def test_resume_refuses_an_unknown_id(monkeypatch, tmp_path: Path):
    stub(monkeypatch, tmp_path)
    result = runner.invoke(cli.app, ["resume", "nosuchsession"])
    assert result.exit_code == 1
    assert "no session id starts with" in result.output


def test_budget_override_lifts_a_stop_without_editing_config(monkeypatch, tmp_path: Path):
    """A budget stop suspends the session; lifting it means raising the cap. That
    has to be possible at the moment you are blocked, not only by editing a file."""
    from duet.schemas.config import DuetConfig as _Cfg

    cfg = stub(monkeypatch, tmp_path)
    cfg.budget.max_usd = 0.0025  # two scripted calls in
    started = runner.invoke(cli.app, ["new", "an app"], input="")
    assert "BUDGET_STOP" in started.output, started.output
    session_id = started.output.split("session ")[1].split()[0]

    # Resuming at the same cap explains itself rather than silently doing nothing.
    blocked = runner.invoke(cli.app, ["resume", session_id], input="")
    assert "raise budget.max_usd" in blocked.output

    # Raising it continues the debate from the log. The stop landed after opus
    # drafted round 2, so the resumed run owes sol's critique of it and then
    # round 3 — the scripts pick up exactly there, which is itself the proof
    # that nothing is replayed twice.
    monkeypatch.setattr(cli, "providers_for", lambda c: {
        "sol": FakeProvider("fake", "model-sol", [parsed(o) for o in (
            objects("Data model", "no pagination"), spec("v3", addresses=["OBJ-2"]))]),
        "opus": FakeProvider("fake", "model-opus", [parsed(agrees())]),
    })
    lifted = runner.invoke(cli.app, ["resume", session_id, "--budget", "25"], input="approve\n")
    assert lifted.exit_code == 0, lifted.output
    assert "DONE" in lifted.output


def test_view_pages_the_full_spec_then_asks_again(monkeypatch, tmp_path: Path):
    """`view` is not a decision — the gate reopens after showing the document."""
    stub(monkeypatch, tmp_path)
    result = runner.invoke(cli.app, ["new", "an app"], input="view\napprove\n")

    assert result.exit_code == 0, result.output
    assert "Problem statement" in result.output  # the whole spec, on request
    assert "DONE" in result.output


def test_an_unrecognised_action_reprompts(monkeypatch, tmp_path: Path):
    stub(monkeypatch, tmp_path)
    result = runner.invoke(cli.app, ["new", "an app"], input="maybe\napprove\n")
    assert result.exit_code == 0, result.output
    assert "choose one of" in result.output
    assert "DONE" in result.output
