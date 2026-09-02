"""Reviewing a human-edited spec (SPECS §13.1).

The human takes the proposer turn: they supply the revision, the models
critique it. One round, no revision cycle.
"""

from pathlib import Path

from tests.conftest import FakeProvider, agrees, objects, parsed, roster, spec

from duet.orchestrator.machine import Machine, submit_gate_decision
from duet.store.session import SessionStore

CONSENSUS = {
    "sol": [spec("v1"), objects("Data model", "no pagination"), spec("v3", addresses=["OBJ-2"])],
    "opus": [objects("Architecture", "no cache"), spec("v2", addresses=["OBJ-1"]), agrees()],
}


def machine_for(cfg, tmp_path, scripts, session_id=None):
    store = SessionStore(session_id, root=tmp_path) if session_id else SessionStore.new(root=tmp_path)
    providers = {
        pid: FakeProvider("anthropic", f"model-{pid}", [parsed(o) for o in script])
        for pid, script in scripts.items()
    }
    return Machine(cfg, store, providers), store


def approved_session(cfg, tmp_path, extra):
    scripts = {k: list(v) + extra.get(k, []) for k, v in CONSENSUS.items()}
    machine, store = machine_for(cfg, tmp_path, scripts)
    machine.start("a link-saving app")
    submit_gate_decision(store, action="approve", actor="mauro")
    machine.advance()
    assert machine.current_state() == "DONE"
    return machine, store


def test_review_reopens_an_approved_spec_and_reaches_the_gate(tmp_path: Path):
    cfg = roster("sol", "opus")
    machine, store = approved_session(cfg, tmp_path, {"sol": [agrees()], "opus": [agrees()]})

    result = machine.review("# Edited spec\n\nNow with offline support.\n", "mauro")

    assert result.suspended and result.state == "GATE_1"
    assert "2/2 agreed" in result.detail
    assert (store.dir / "artifacts" / "IMPLEMENTATION.md").read_text().startswith("# Edited spec")


def test_the_human_draft_is_logged_as_a_round_by_its_author(tmp_path: Path):
    cfg = roster("sol", "opus")
    machine, store = approved_session(cfg, tmp_path, {"sol": [agrees()], "opus": [agrees()]})
    machine.review("# Edited spec\n", "mauro")

    drafts = [e.payload for e in store.events() if e.type == "draft"]
    assert drafts[-1]["participant"] == "mauro" and drafts[-1]["human"] is True
    assert drafts[-1]["round"] == 4  # the debate ran 3, the review is the 4th


def test_a_human_vote_cannot_manufacture_consensus(tmp_path: Path):
    """The author's assent is recorded, but consensus is a claim about models."""
    cfg = roster("sol", "opus")
    machine, store = approved_session(
        cfg, tmp_path, {"sol": [agrees()], "opus": [objects("Data model", "edit broke pagination")]}
    )

    result = machine.review("# Edited spec\n", "mauro")

    assert result.state == "GATE_1" and result.suspended
    assert "unresolved objections from opus" in result.detail
    # mauro agreed, sol agreed: two AGREE votes, but only one is a participant.
    assert result.gate is not None
    votes = ["mauro", "sol"]
    assert all(v in [e.payload["participant"] for e in store.events()
                     if e.type in ("draft", "verdict")] for v in votes)


def test_objections_from_the_review_reach_the_ledger(tmp_path: Path):
    cfg = roster("sol", "opus")
    machine, store = approved_session(
        cfg, tmp_path, {"sol": [agrees()], "opus": [objects("Security", "no auth on the API")]}
    )
    machine.review("# Edited spec\n", "mauro")

    verdicts = [e.payload for e in store.events() if e.type == "verdict"]
    assert verdicts[-1]["verdict"]["objections"][0]["claim"] == "no auth on the API"
    assert verdicts[-1]["round"] == 4


def test_review_is_refused_mid_debate(tmp_path: Path):
    """Only from a settled spec — reviewing a draft the models are still
    arguing over would inject a fourth opinion into an open round."""
    cfg = roster("sol", "opus")
    cfg.debate.max_rounds = 1
    machine, _ = machine_for(cfg, tmp_path, {"sol": [spec("v1")], "opus": [objects("A", "x")]})
    machine.start("an app")
    assert machine.current_state() == "ARBITRATION"

    result = machine.review("# Edited\n", "mauro")
    assert not result.suspended
    assert "cannot review from ARBITRATION" in result.detail
