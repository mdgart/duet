"""Scripted Phase A debates — the deterministic state-machine tests of SPECS §12.

Every protocol test runs at N = 2 and N = 3, per the roster-size requirement.
"""

from pathlib import Path

import pytest

from tests.conftest import FakeProvider, agrees, objects, parsed, roster, spec

from duet.orchestrator.debate import DebateEngine
from duet.store.session import SessionStore


def engine_for(cfg, tmp_path: Path, scripts: dict[str, list]):
    store = SessionStore.new(root=tmp_path)
    providers = {
        pid: FakeProvider(cfg.roster.get(pid).provider, cfg.roster.get(pid).model,
                          [parsed(o) for o in script])
        for pid, script in scripts.items()
    }
    return DebateEngine(cfg, store, providers), store


def test_two_participants_reach_consensus(tmp_path: Path):
    cfg = roster("sol", "opus")
    # r1 sol proposes / opus critiques · r2 opus proposes / sol critiques
    # r3 sol proposes / opus critiques -> AGREE
    engine, store = engine_for(cfg, tmp_path, {
        "sol": [spec("v1"), objects("Data model", "no pagination"), spec("v3", addresses=["OBJ-2"])],
        "opus": [objects("Architecture", "no cache layer"), spec("v2", addresses=["OBJ-1"]), agrees()],
    })
    result = engine.run("a link-saving app")

    assert result.outcome == "CONSENSUS"
    assert result.state.round == 3
    assert sorted(result.state.agreements_on(result.state.draft_hash)) == ["opus", "sol"]
    assert [e.status for e in result.state.ledger.entries] == ["resolved", "resolved"]
    assert store.cost_usd == pytest.approx(0.006)  # six model calls


def test_three_participants_reach_consensus(tmp_path: Path):
    cfg = roster("sol", "opus", "haiku")
    # Ids are assigned in sorted-participant order as each round merges, so
    # r1 yields OBJ-1 (haiku) and OBJ-2 (opus); r2 yields OBJ-3 (haiku), OBJ-4 (sol).
    engine, _ = engine_for(cfg, tmp_path, {
        # proposes r1, critiques r2 and r3
        "sol": [spec("v1"), objects("Data model", "sol objection"), agrees(resolved=["OBJ-4"])],
        # critiques r1, proposes r2, critiques r3
        "opus": [objects("Architecture", "opus objection"), spec("v2", addresses=["OBJ-2"]), agrees()],
        # critiques r1 and r2, proposes r3
        "haiku": [objects("Data model", "haiku objection"), objects("Goals", "haiku second"),
                  spec("v3", addresses=["OBJ-1", "OBJ-3"])],
    })
    result = engine.run("a link-saving app")

    assert result.outcome == "CONSENSUS"
    assert result.state.agreements_on(result.state.draft_hash) == ["haiku", "opus", "sol"]
    assert all(e.status != "open" for e in result.state.ledger.entries)


def test_corroborated_objection_needs_every_raiser_to_close(tmp_path: Path):
    """Two critics raise the same point; one draft cannot close it alone."""
    cfg = roster("sol", "opus", "haiku")
    cfg.debate.max_rounds = 3
    engine, _ = engine_for(cfg, tmp_path, {
        "sol": [spec("v1"), objects("Goals", "sol point"), agrees()],
        "opus": [objects("Data model", "No  pagination!"), spec("v2", addresses=["OBJ-1"]), agrees()],
        "haiku": [objects("Data model", "no pagination"), objects("API", "haiku point"), spec("v3")],
    })
    result = engine.run("an app")

    entry = result.state.ledger.get("OBJ-1")
    assert entry.raised_by == ["haiku", "opus"]  # deduplicated across critics
    assert entry.corroborated
    # opus closed it for itself in v2; haiku never did, so it stays open.
    assert entry.closed_by == {"opus": "resolved"}
    assert entry.status == "open"
    assert result.outcome == "DEADLOCK"


def test_early_agree_is_coerced_by_the_floor(tmp_path: Path):
    cfg = roster("sol", "opus")
    cfg.debate.max_rounds = 1
    engine, store = engine_for(cfg, tmp_path, {
        "sol": [spec("v1")],
        "opus": [agrees()],  # round 1 — below the anti-sycophancy floor
    })
    result = engine.run("an app")

    assert result.outcome == "DEADLOCK"
    verdicts = [e for e in store.events() if e.type == "verdict"]
    assert verdicts[0].payload["coerced"] == "agree_before_floor"
    assert verdicts[0].payload["verdict"]["verdict"] == "REVISE"
    assert result.state.votes["opus"][1] == "REVISE"


def test_agree_with_open_objections_is_coerced(tmp_path: Path):
    """An AGREE that leaves the critic's own blocking objection open is not one."""
    cfg = roster("sol", "opus")
    cfg.debate.min_objection_rounds = 0
    cfg.debate.max_rounds = 2
    engine, store = engine_for(cfg, tmp_path, {
        "sol": [spec("v1"), objects("Data model", "sol point", "blocking")],
        "opus": [objects("Architecture", "opus point", "blocking"), spec("v2")],
    })
    result = engine.run("an app")
    # sol objected in r2, so sol's authorship vote is the only AGREE on v2.
    assert result.outcome == "DEADLOCK"
    assert result.state.ledger.get("OBJ-1").status == "open"


def test_reopen_limit_deadlocks(tmp_path: Path):
    cfg = roster("sol", "opus")
    cfg.debate.min_objection_rounds = 0
    cfg.debate.reopen_limit = 2
    same = lambda: objects("Data model", "no pagination")
    engine, _ = engine_for(cfg, tmp_path, {
        "sol": [spec("v1"), same(), spec("v3", addresses=["OBJ-1"]), same(), spec("v5")],
        "opus": [same(), spec("v2", addresses=["OBJ-1"]), same(), spec("v4", addresses=["OBJ-1"]), same()],
    })
    result = engine.run("an app")

    assert result.outcome == "DEADLOCK"
    assert "reopened" in result.detail and "OBJ-1" in result.detail


def test_round_cap_deadlocks(tmp_path: Path):
    cfg = roster("sol", "opus")
    cfg.debate.max_rounds = 2
    engine, _ = engine_for(cfg, tmp_path, {
        "sol": [spec("v1"), objects("A", "sol point")],
        "opus": [objects("B", "opus point"), spec("v2")],
    })
    result = engine.run("an app")
    assert result.outcome == "DEADLOCK"
    assert "round cap 2" in result.detail


def test_budget_stop_is_not_a_crash(tmp_path: Path):
    cfg = roster("sol", "opus")
    cfg.budget.max_usd = 0.0025  # two calls in
    engine, _ = engine_for(cfg, tmp_path, {
        "sol": [spec("v1"), objects("A", "sol point")],
        "opus": [objects("B", "opus point"), spec("v2")],
    })
    result = engine.run("an app")
    assert result.outcome == "BUDGET_STOP"
    assert "budget exceeded" in result.detail


def test_each_participant_is_told_which_objections_only_it_can_close(tmp_path: Path):
    """Observed live: a debate stalled with every open objection blocked on one
    participant, which had declared 39 closures — almost all for objections the
    *other* participant raised. Only the raiser can close, so the orchestrator
    computes that set rather than leaving the model to infer it."""
    cfg = roster("sol", "opus")
    cfg.debate.min_objection_rounds = 0
    cfg.debate.max_rounds = 3
    engine, _ = engine_for(cfg, tmp_path, {
        "sol": [spec("v1"), objects("Data model", "sol point"), spec("v3")],
        "opus": [objects("Architecture", "opus point"), spec("v2"), objects("Goals", "another")],
    })
    engine.run("an app")

    # By round 3 sol has an open objection of its own (OBJ-2, raised in r2).
    sol_prompts = [r.messages[0].content for r in engine.providers["sol"].requests]
    assert "# Your open objections" in sol_prompts[-1]
    assert "sol point" in sol_prompts[-1]
    assert "nobody else can close them" in sol_prompts[-1]
    # And the block lists only sol's own — the other participant's objections
    # still appear in the verbatim critique further down, which is correct.
    block = sol_prompts[-1].split("# Your open objections")[1].split("\n\n---\n\n")[0]
    assert "sol point" in block and "opus point" not in block


def test_no_such_section_before_anyone_has_objected(tmp_path: Path):
    cfg = roster("sol", "opus")
    engine, _ = engine_for(cfg, tmp_path, {
        "sol": [spec("v1")], "opus": [objects("A", "x")],
    })
    cfg.debate.max_rounds = 1
    engine.run("an app")
    assert "# Your open objections" not in engine.providers["sol"].requests[0].messages[0].content


def test_objection_headline_flows_into_the_ledger_and_falls_back(tmp_path: Path):
    """Ten objections at a paragraph each is unreadable; the critic writes a
    one-liner. Entries logged before headlines existed trim the claim instead."""
    from duet.orchestrator.ledger import ObjectionLedger
    from duet.schemas.debate import Objection, Verdict

    ledger = ObjectionLedger()
    ledger.merge_round(1, {"a": Verdict(verdict="OBJECT", objections=[
        Objection(severity="blocking", headline="clock source is unspecified",
                  section="Architecture", claim="a" * 300, evidence="e", proposed_change="p")])})
    entry = ledger.entries[0]
    assert entry.one_line == "clock source is unspecified"
    assert entry.one_line in ledger.summary_lines()[0]
    assert "a" * 300 not in ledger.summary_lines()[0]  # the wall of text stays out of prompts

    entry.headline = ""  # a pre-headline log
    assert entry.one_line.endswith("…") and len(entry.one_line) < 90
