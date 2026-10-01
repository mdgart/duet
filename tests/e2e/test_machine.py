"""State machine + gates: suspension, cross-process resume, arbitration.

The M1 exit criterion is here — a session suspended at Gate 1 must resume in a
process that never saw the debate.
"""

from pathlib import Path
import hashlib

import pytest

from tests.conftest import FakeProvider, agrees, objects, parsed, roster, spec

from duet.orchestrator.machine import GateConflict, Machine, submit_gate_decision
from duet.store.session import SessionStore

CONSENSUS_SCRIPT = {
    "sol": [spec("v1"), objects("Data model", "no pagination"), spec("v3", addresses=["OBJ-2"])],
    "opus": [objects("Architecture", "no cache"), spec("v2", addresses=["OBJ-1"]), agrees()],
}


def machine_for(cfg, tmp_path: Path, scripts: dict[str, list], session_id: str | None = None):
    store = SessionStore(session_id, root=tmp_path) if session_id else SessionStore.new(root=tmp_path)
    providers = {
        pid: FakeProvider("anthropic", f"model-{pid}", [parsed(o) for o in script])
        for pid, script in scripts.items()
    }
    return Machine(cfg, store, providers), store


def test_runs_to_gate_1_and_suspends(tmp_path: Path):
    machine, store = machine_for(roster("sol", "opus"), tmp_path, CONSENSUS_SCRIPT)
    result = machine.start("a link-saving app")

    assert result.suspended and result.state == "GATE_1"
    assert result.gate.options == ["approve", "edit", "revise", "reject"]
    assert (store.dir / "artifacts" / "IMPLEMENTATION.md").is_file()
    assert [e.type for e in store.events()].count("gate_pending") == 1


def test_gate_1_approval_resumes_in_a_fresh_process(tmp_path: Path):
    """The M1 exit criterion: nothing carries over but the log."""
    cfg = roster("sol", "opus")
    machine, store = machine_for(cfg, tmp_path, CONSENSUS_SCRIPT)
    machine.start("a link-saving app")
    session_id = store.session_id
    del machine, store

    # A new process: new store, new machine, and providers that would raise if
    # the resumed run tried to call a model at all.
    fresh_store = SessionStore(session_id, root=tmp_path)
    submit_gate_decision(fresh_store, action="approve", actor="mauro")

    resumed, _ = machine_for(cfg, tmp_path, {"sol": [], "opus": []}, session_id=session_id)
    result = resumed.advance()

    assert result.state == "DONE"
    assert "approved by mauro" in result.detail
    decisions = [e for e in fresh_store.events() if e.type == "gate_decision"]
    assert decisions[0].payload["actor"] == "mauro"


def test_gate_decision_is_pinned_to_the_draft_it_answers(tmp_path: Path):
    machine, store = machine_for(roster("sol", "opus"), tmp_path, CONSENSUS_SCRIPT)
    result = machine.start("an app")
    submit_gate_decision(store, action="approve", actor="mauro")
    decision = [e for e in store.events() if e.type == "gate_decision"][0]
    assert decision.payload["draft_hash"] == result.gate.draft_hash
    assert decision.payload["gate_seq"] == result.gate.gate_seq


def test_gate_helper_rejects_a_stale_expected_version(tmp_path: Path):
    machine, store = machine_for(roster("sol", "opus"), tmp_path, CONSENSUS_SCRIPT)
    result = machine.start("an app")
    with pytest.raises(GateConflict, match="gate has changed"):
        submit_gate_decision(store, action="approve", actor="mauro", expected_draft_hash="old")
    with pytest.raises(GateConflict, match="gate has changed"):
        submit_gate_decision(store, action="approve", actor="mauro",
                             expected_draft_hash=result.gate.draft_hash,
                             expected_gate_seq=result.gate.gate_seq + 1)
    assert not any(e.type == "gate_decision" for e in store.events())


def test_resume_rejects_a_decision_for_different_text(tmp_path: Path):
    machine, store = machine_for(roster("sol", "opus"), tmp_path, CONSENSUS_SCRIPT)
    machine.start("an app")
    store.append("gate_decision", {"gate": "gate_1", "action": "approve", "actor": "mauro",
                                  "draft_hash": "stale"})
    with pytest.raises(GateConflict, match="does not match"):
        machine.advance()
    assert machine.current_state() == "GATE_1"


def test_edit_revision_recovery_is_idempotent(tmp_path: Path):
    cfg = roster("sol", "opus")
    machine, store = machine_for(cfg, tmp_path, CONSENSUS_SCRIPT)
    result = machine.start("an app")
    edited = "# A recovered edit\n\nFIXED: work offline."
    submit_gate_decision(store, action="edit", actor="mauro", edited_markdown=edited,
                         expected_gate_seq=result.gate.gate_seq,
                         expected_draft_hash=result.gate.draft_hash)
    # The process stopped after logging the canonical revision, before writing
    # the artifact or transitioning to DONE. Resume must finish both steps.
    store.append("artifact_revision", {
        "artifact": "artifacts/IMPLEMENTATION.md", "markdown": edited,
        "hash": hashlib.sha256(edited.encode()).hexdigest()[:12],
        "previous_hash": result.gate.draft_hash, "actor": "mauro",
        "gate_seq": result.gate.gate_seq,
    })
    fresh = SessionStore(store.session_id, root=tmp_path)
    assert Machine(cfg, fresh, {}).advance().state == "DONE"
    assert (fresh.dir / "artifacts" / "IMPLEMENTATION.md").read_text() == edited
    assert sum(e.type == "artifact_revision" for e in fresh.events()) == 1


def test_unoffered_action_is_refused(tmp_path: Path):
    machine, store = machine_for(roster("sol", "opus"), tmp_path, CONSENSUS_SCRIPT)
    machine.start("an app")
    with pytest.raises(ValueError, match="not offered"):
        submit_gate_decision(store, action="resolve", actor="mauro")


def test_decision_without_an_open_gate_is_refused(tmp_path: Path):
    store = SessionStore.new(root=tmp_path)
    with pytest.raises(ValueError, match="no gate is currently open"):
        submit_gate_decision(store, action="approve", actor="mauro")


def test_reject_ends_the_session(tmp_path: Path):
    cfg = roster("sol", "opus")
    machine, store = machine_for(cfg, tmp_path, CONSENSUS_SCRIPT)
    machine.start("an app")
    submit_gate_decision(store, action="reject", actor="mauro")
    assert machine.advance().state == "FAILED"


def test_deadlock_opens_arbitration_with_a_report(tmp_path: Path):
    cfg = roster("sol", "opus")
    cfg.debate.max_rounds = 2
    machine, store = machine_for(cfg, tmp_path, {
        "sol": [spec("v1"), objects("Data model", "sol point", "blocking")],
        "opus": [objects("Architecture", "opus point", "blocking"), spec("v2")],
    })
    result = machine.start("an app")

    assert result.suspended and result.state == "ARBITRATION"
    assert result.gate.options == ["resolve", "abort"]
    report = (store.dir / "artifacts" / result.gate.artifacts[0].split("/")[-1]).read_text()
    assert "# Disagreement report" in report
    assert "| sol | upholds |" in report and "| opus | did not raise |" in report


def test_revise_reopens_the_debate_with_feedback_as_fixed(tmp_path: Path):
    cfg = roster("sol", "opus")
    scripts = {
        "sol": CONSENSUS_SCRIPT["sol"] + [agrees()],
        "opus": CONSENSUS_SCRIPT["opus"] + [spec("v4")],
    }
    machine, store = machine_for(cfg, tmp_path, scripts)
    machine.start("an app")
    submit_gate_decision(store, action="revise", actor="mauro", feedback="must work offline")

    result = machine.advance()
    assert result.state == "GATE_1" and result.suspended
    assert "FIXED: must work offline" in machine.brief()
    assert machine.round_cap_bonus() == 1


def test_a_budget_stop_suspends_and_resumes_on_a_raised_cap(tmp_path: Path):
    """SPECS §7.2 calls a budget stop resumable. It has to be: a cap low enough
    to catch a runaway would otherwise destroy the session it caught."""
    cfg = roster("sol", "opus")
    cfg.budget.max_usd = 0.0025  # two calls in
    machine, store = machine_for(cfg, tmp_path, CONSENSUS_SCRIPT)
    stopped = machine.start("an app")

    assert stopped.state == "BUDGET_STOP"
    assert "raise budget.max_usd and resume" in machine.advance().detail

    # Raise the cap; the debate picks up from the log rather than restarting.
    cfg.budget.max_usd = 25.0
    resumed = machine.advance()
    assert resumed.state == "GATE_1" and resumed.suspended
    drafts = [e.payload["round"] for e in store.events() if e.type == "draft"]
    assert drafts == sorted(set(drafts)), "a resumed run must not repeat rounds"


def test_resume_uses_the_sessions_roster_not_the_current_config(tmp_path: Path):
    """Swapping the roster mid-debate orphans every open objection: only the
    participant who raised one may close it, so a new roster can only deadlock."""
    from duet.orchestrator.machine import pin_roster

    cfg = roster("sol", "opus")
    machine, store = machine_for(cfg, tmp_path, CONSENSUS_SCRIPT)
    machine.start("an app")

    changed = roster("mini", "haiku")
    bound, notice = pin_roster(changed, store)

    assert bound.roster.ids == ["sol", "opus"]
    assert "session roster is sol, opus" in notice and "config says mini, haiku" in notice


def test_an_unchanged_roster_resumes_without_comment(tmp_path: Path):
    from duet.orchestrator.machine import pin_roster

    cfg = roster("sol", "opus")
    machine, store = machine_for(cfg, tmp_path, CONSENSUS_SCRIPT)
    machine.start("an app")
    bound, notice = pin_roster(roster("sol", "opus"), store)
    assert notice == "" and bound.roster.ids == ["sol", "opus"]
