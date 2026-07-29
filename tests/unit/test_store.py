from pathlib import Path

import pytest

from duet.store.session import BudgetExceeded, SessionStore


def test_append_and_replay(tmp_path: Path):
    store = SessionStore.new(root=tmp_path)
    store.append("state_change", {"from": None, "to": "INIT"})
    store.append("model_call", {"provider": "openai"})

    events = list(store.events())
    assert [e.seq for e in events] == [1, 2]
    assert events[0].type == "state_change"

    # Resume: a fresh instance continues the sequence from disk.
    resumed = SessionStore(store.session_id, root=tmp_path)
    resumed.append("error", {"msg": "x"})
    assert list(resumed.events())[-1].seq == 3


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
