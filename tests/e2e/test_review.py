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


# --- arbitration rulings -----------------------------------------------------


def deadlocked(cfg, tmp_path: Path, after: dict | None = None):
    """Deadlock at the cap. `after` supplies the turns a resolve would resume into.

    r1 opus raises OBJ-1 (Architecture); r2 sol raises OBJ-2 (Data model).
    """
    cfg.debate.max_rounds = 2
    after = after or {}
    machine, store = machine_for(cfg, tmp_path, {
        "sol": [spec("v1"), objects("Data model", "sol point", "blocking")] + after.get("sol", []),
        "opus": [objects("Architecture", "opus point", "blocking"), spec("v2")] + after.get("opus", []),
    })
    return machine, store, machine.start("an app")


def test_the_arbitration_gate_carries_what_is_unresolved(tmp_path: Path):
    """A client cannot ask for a ruling per objection without knowing them."""
    machine, _, result = deadlocked(roster("sol", "opus"), tmp_path)

    assert result.state == "ARBITRATION"
    ids = {o["id"] for o in result.gate.objections}
    assert ids == {"OBJ-1", "OBJ-2"}
    first = result.gate.objections[0]
    assert {"id", "severity", "section", "claim", "held_by"} <= set(first)
    assert first["held_by"], "an open objection is held by whoever will not close it"


def test_rulings_become_separate_fixed_items(tmp_path: Path):
    """The report promises a decision per objection, so one blob is not enough."""
    cfg = roster("sol", "opus")
    # A resolve raises the cap by 2, so the debate resumes: sol proposes r3
    # closing its own OBJ-2, opus agrees having closed OBJ-1.
    machine, store, _ = deadlocked(cfg, tmp_path, after={
        "sol": [spec("v3", addresses=["OBJ-2"])],
        "opus": [agrees(resolved=["OBJ-1"])],
    })
    submit_gate_decision(
        store, action="resolve", actor="mauro",
        feedback="FIXED: OBJ-1 — drop the cache entirely\nFIXED: OBJ-2 — page size is 50",
    )
    machine.advance()

    brief = machine.brief()
    assert "FIXED: OBJ-1 — drop the cache entirely" in brief
    assert "FIXED: OBJ-2 — page size is 50" in brief
    assert "FIXED: FIXED:" not in brief, "already-marked rulings must not be re-prefixed"


def test_unmarked_revision_feedback_still_gets_the_marker(tmp_path: Path):
    cfg = roster("sol", "opus")
    scripts = {"sol": CONSENSUS["sol"] + [agrees()], "opus": CONSENSUS["opus"] + [spec("v4")]}
    machine, store = machine_for(cfg, tmp_path, scripts)
    machine.start("an app")
    submit_gate_decision(store, action="revise", actor="mauro", feedback="must work offline")
    machine.advance()
    assert "FIXED: must work offline" in machine.brief()


def test_the_disagreement_report_opens_with_what_it_is_about(tmp_path: Path):
    """A report you must read in full to know whether to resolve or abort is a
    report that gets aborted."""
    from duet.orchestrator.arbitration import disagreement_report
    from duet.orchestrator.ledger import ObjectionLedger
    from duet.schemas.debate import Objection, Verdict

    cfg = roster("sol", "opus")
    ledger = ObjectionLedger()
    ledger.merge_round(1, {"sol": Verdict(verdict="OBJECT", objections=[
        Objection(severity="blocking", section="Architecture / endpoint control",
                  claim="clock source is unspecified", evidence="e", proposed_change="p"),
        Objection(severity="major", section="Architecture / endpoint control",
                  claim="lease issuance has no freshness limit", evidence="e", proposed_change="p"),
    ])})
    report = disagreement_report(ledger, cfg.roster, "round cap", [2, 2], [(2, 0), (0, 0)])

    head = report.split("## Was it converging?")[0]
    assert "2 objection(s) still open, 1 of them blocking" in head
    assert "held by sol" in head
    assert "Architecture / endpoint control" in head
    assert "one decision wearing several hats" in head
