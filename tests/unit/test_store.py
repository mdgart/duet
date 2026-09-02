from pathlib import Path

import pytest

from duet.store.session import BudgetExceeded, SessionStore


def test_append_and_replay(tmp_path: Path):
    store = SessionStore.new(root=tmp_path, owner="mauro")
    store.append("state_change", {"from": None, "to": "INIT"})
    store.append("model_call", {"provider": "openai"})

    events = list(store.events())
    # seq 1 is session_start, written by new().
    assert [e.seq for e in events] == [1, 2, 3]
    assert [e.type for e in events][:2] == ["session_start", "state_change"]

    # Resume: a fresh instance continues the sequence from disk.
    resumed = SessionStore(store.session_id, root=tmp_path)
    resumed.append("error", {"msg": "x"})
    assert list(resumed.events())[-1].seq == 4


def test_owner_is_recorded_once_at_creation(tmp_path: Path):
    """Ownership cannot be backfilled, so it is written when the session is."""
    store = SessionStore.new(root=tmp_path, owner="mauro")
    assert store.owner == "mauro"
    # Survives a reopen, and later events do not overwrite it.
    store.append("gate_decision", {"actor": "someone-else"})
    assert SessionStore(store.session_id, root=tmp_path).owner == "mauro"


def test_sessions_without_an_owner_are_anonymous_not_broken(tmp_path: Path):
    assert SessionStore.new(root=tmp_path).owner == ""


def test_resolve_finds_a_session_by_prefix(tmp_path: Path):
    store = SessionStore.new(root=tmp_path)
    assert len(store.session_id) == 32  # full uuid4, never the weak link in a URL
    assert SessionStore.resolve(store.session_id[:8], root=tmp_path) == store.session_id
    assert SessionStore.resolve(store.session_id, root=tmp_path) == store.session_id


def test_resolve_reports_no_match_and_ambiguity(tmp_path: Path):
    SessionStore.new(root=tmp_path)
    with pytest.raises(ValueError, match="no session id starts with"):
        SessionStore.resolve("zzzz", root=tmp_path)

    # Two sessions sharing a prefix must not silently resolve to one of them.
    for sid in ("abcd1111", "abcd2222"):
        SessionStore(sid, root=tmp_path).append("session_start", {"owner": ""})
    with pytest.raises(ValueError, match="ambiguous"):
        SessionStore.resolve("abcd", root=tmp_path)


def test_cost_accumulation_and_budget_cap(tmp_path: Path):
    store = SessionStore.new(root=tmp_path)
    store.add_cost("m", {"cost_usd": 0.4}, cap_usd=1.0)
    assert store.cost_usd == pytest.approx(0.4)
    with pytest.raises(BudgetExceeded):
        store.add_cost("m", {"cost_usd": 0.7}, cap_usd=1.0)
    # Cost survives resume (replayed from log).
    resumed = SessionStore(store.session_id, root=tmp_path)
    assert resumed.cost_usd == pytest.approx(1.1)


def test_list_sessions_and_artifacts(tmp_path: Path):
    store = SessionStore.new(root=tmp_path)
    store.append("state_change", {"to": "INIT"})
    ref = store.save_artifact("draft.md", "# spec")
    assert ref == "artifacts/draft.md"
    assert (store.dir / "artifacts" / "draft.md").read_text() == "# spec"
    assert store.session_id in SessionStore.list_sessions(root=tmp_path)


def test_label_prefers_the_draft_title_over_the_brief(tmp_path: Path):
    """Participants name the thing they are designing far better than the
    opening brief does — and a listing of bare uuids is unreadable."""
    store = SessionStore.new(root=tmp_path)
    store.append("state_change", {"to": "DESIGN_DEBATE", "brief": "something to track my reading"})
    assert store.label() == "something to track my reading"

    store.append("draft", {"round": 1, "markdown": "# CLI Reading Tracker\n\n## Problem"})
    assert store.label() == "CLI Reading Tracker"

    # The latest draft wins: titles change as the spec is revised.
    store.append("draft", {"round": 2, "markdown": "# Readlog\n\n## Problem"})
    assert store.label() == "Readlog"


def test_label_is_shortened_and_survives_odd_input(tmp_path: Path):
    store = SessionStore.new(root=tmp_path)
    store.append("state_change", {"to": "DESIGN_DEBATE", "brief": "word " * 40})
    assert len(store.label()) <= 34 and store.label().endswith("…")

    bare = SessionStore.new(root=tmp_path)
    assert bare.label() == ""  # nothing to say yet, rather than a crash

    # A draft whose markdown has no heading falls back rather than mislabelling.
    bare.append("state_change", {"to": "DESIGN_DEBATE", "brief": "a brief"})
    bare.append("draft", {"round": 1, "markdown": "no heading here"})
    assert bare.label() == "a brief"


def test_sessions_are_listed_most_recent_first(tmp_path: Path):
    import os
    import time

    older = SessionStore.new(root=tmp_path)
    older.append("state_change", {"to": "DONE"})
    time.sleep(0.01)
    newer = SessionStore.new(root=tmp_path)
    newer.append("state_change", {"to": "DONE"})

    listed = SessionStore.list_sessions(root=tmp_path)
    assert listed[0] == newer.session_id
    assert listed.index(newer.session_id) < listed.index(older.session_id)
