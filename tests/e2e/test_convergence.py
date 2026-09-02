"""Aggregate non-convergence detection.

The reopen limit catches a debate stuck on one objection. It is blind to the
other failure — every round closes real objections and raises equally many new
ones, so nothing repeats, the ledger grows forever, and only the round cap
stops it. Observed live: 32 objections over 8 rounds, 32 distinct keys, zero
reopens, $0.41 spent to learn nothing.
"""

from pathlib import Path

from tests.conftest import FakeProvider, agrees, objects, parsed, roster, spec

from duet.orchestrator.arbitration import convergence_trend
from duet.orchestrator.debate import DebateEngine
from duet.store.session import SessionStore


def engine_for(cfg, tmp_path: Path, scripts: dict[str, list]):
    store = SessionStore.new(root=tmp_path)
    providers = {
        pid: FakeProvider("anthropic", f"model-{pid}", [parsed(o) for o in script])
        for pid, script in scripts.items()
    }
    return DebateEngine(cfg, store, providers), store


def test_a_debate_raising_new_objections_forever_is_stopped_early(tmp_path: Path):
    cfg = roster("sol", "opus")
    cfg.debate.min_objection_rounds = 0
    cfg.debate.stall_rounds = 2
    cfg.debate.max_rounds = 8
    # Every round: a fresh objection, nothing closed. Nothing ever repeats, so
    # the reopen limit stays silent no matter how long this runs.
    engine, _ = engine_for(cfg, tmp_path, {
        "sol": [spec("v1"), objects("A", "first sol point"), spec("v3")],
        "opus": [objects("B", "first opus point"), spec("v2"), objects("C", "second opus point")],
    })
    result = engine.run("an app")

    assert result.outcome == "DEADLOCK"
    assert "no progress in 2 rounds" in result.detail
    assert result.state.round == 3  # stopped at 3 of 8 — five rounds unspent
    assert not result.state.ledger.thrashing(cfg.debate.reopen_limit)  # reopen limit saw nothing


def test_a_debate_still_reaching_new_lows_is_left_alone(tmp_path: Path):
    """The real failed run kept setting new bests (4→3→2→1) before running out
    of rounds. That is slow progress, not a stall, and must not be aborted."""
    cfg = roster("sol", "opus")
    cfg.debate.stall_rounds = 2
    engine, _ = engine_for(cfg, tmp_path, {
        "sol": [spec("v1"), objects("Data model", "no pagination"), spec("v3", addresses=["OBJ-2"])],
        "opus": [objects("Architecture", "no cache"), spec("v2", addresses=["OBJ-1"]), agrees()],
    })
    result = engine.run("an app")

    assert result.outcome == "CONSENSUS"
    assert result.state.open_history == [1, 1, 0]


def test_the_trace_survives_replay(tmp_path: Path):
    """A resumed debate must remember it was stalling, or the detector resets."""
    cfg = roster("sol", "opus")
    cfg.debate.min_objection_rounds = 0
    cfg.debate.max_rounds = 2
    engine, store = engine_for(cfg, tmp_path, {
        "sol": [spec("v1"), objects("A", "sol point")],
        "opus": [objects("B", "opus point"), spec("v2")],
    })
    original = engine.run("an app").state.open_history

    replayed = DebateEngine.replay(cfg, SessionStore(store.session_id, root=tmp_path), {})
    assert replayed.state.open_history == original


def test_the_trend_tells_arbitration_which_failure_this_was():
    assert "still improving" in "\n".join(convergence_trend([5, 4, 2, 1]))
    assert "getting worse" in "\n".join(convergence_trend([4, 3, 4, 2, 6, 1, 4, 7]))
    assert "stalled" in "\n".join(convergence_trend([4, 2, 3, 3, 4]))
    assert convergence_trend([]) == []


def test_churn_separates_a_stuck_debate_from_a_still_working_one():
    """`2 → 5 → 2 → 5` cannot distinguish "closed three, opened three" from
    "did nothing". Gross movement can, and that is the question about whether a
    debate retires complexity or manufactures it."""
    busy = convergence_trend([2, 5, 2, 5], churn=[(2, 0), (5, 2), (2, 5), (5, 2)])
    text = "\n".join(busy)
    assert "r1: +2/-0" in text and "14 raised, 9 closed" in text
    assert "opening objections faster than it retires them" in text
    assert "smallest deletion that closes several at once" in text

    finishing = convergence_trend([5, 3, 1], churn=[(5, 0), (1, 3), (0, 3)])
    assert "at least as fast as they arrive" in "\n".join(finishing)
    assert "round cap — not the design" in "\n".join(finishing)


def test_the_trend_still_reads_without_churn_data():
    """Sessions logged before churn was measured must still render."""
    assert "still improving" in "\n".join(convergence_trend([5, 4, 2, 1]))


def test_churn_counts_closures_the_proposer_makes(tmp_path: Path):
    """A proposer closes its own objections via `addresses`, before any critique.
    A snapshot taken after that reports "0 closed" for a round that closed
    several — which told a human the debate was hopeless when it was working."""
    cfg = roster("sol", "opus")
    cfg.debate.min_objection_rounds = 0
    cfg.debate.max_rounds = 3
    engine, _ = engine_for(cfg, tmp_path, {
        # sol objects in r1... wait: r1 proposer is sol, so opus critiques.
        "sol": [spec("v1"), objects("Data model", "sol point"), spec("v3", addresses=["OBJ-2"])],
        "opus": [objects("Architecture", "opus point"), spec("v2", addresses=["OBJ-1"]), agrees()],
    })
    result = engine.run("an app")

    raised = sum(r for r, _ in result.state.churn)
    closed = sum(c for _, c in result.state.churn)
    assert raised == 2
    assert closed == 2, f"proposer closures were dropped: {result.state.churn}"
    assert all(e.status != "open" for e in result.state.ledger.entries)


def test_replayed_churn_matches_the_live_run(tmp_path: Path):
    """A resumed session must not report a different history than it lived."""
    from duet.orchestrator.debate import DebateEngine
    from duet.store.session import SessionStore

    cfg = roster("sol", "opus")
    cfg.debate.min_objection_rounds = 0
    cfg.debate.max_rounds = 3
    engine, store = engine_for(cfg, tmp_path, {
        "sol": [spec("v1"), objects("Data model", "sol point"), spec("v3", addresses=["OBJ-2"])],
        "opus": [objects("Architecture", "opus point"), spec("v2", addresses=["OBJ-1"]), agrees()],
    })
    live = engine.run("an app").state.churn

    replayed = DebateEngine.replay(cfg, SessionStore(store.session_id, root=tmp_path), {})
    assert replayed.state.churn == live
