"""The web UI is a client of the event log (SPECS §6.0): it starts debates,
streams them, and answers gates, all through the same Machine the CLI drives.

Providers are faked and the debate runs on a background thread, so these tests
wait for the run to reach a gate rather than asserting instantly.
"""

import json
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeProvider, agrees, objects, parsed, roster, spec

from duet.web import app as webapp
from duet.web.runner import Runner
from duet.store.session import SessionStore
from duet.orchestrator.machine import GateConflict

CONSENSUS = {
    "sol": [spec("v1"), objects("Data model", "no pagination"), spec("v3", addresses=["OBJ-2"])],
    "opus": [objects("Architecture", "no cache"), spec("v2", addresses=["OBJ-1"]), agrees()],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = roster("sol", "opus")

    # Every background run gets fresh scripted providers.
    def fake_providers(config):
        return {
            "sol": FakeProvider("a", "model-sol", [parsed(o) for o in CONSENSUS["sol"]]),
            "opus": FakeProvider("a", "model-opus", [parsed(o) for o in CONSENSUS["opus"]]),
        }

    monkeypatch.setattr("duet.web.runner.providers_for", fake_providers)
    app = webapp.create_app(config=cfg, root=tmp_path)
    return TestClient(app)


def wait_until(fn, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def open_gate(client):
    sid = client.post("/api/sessions", json={"idea": "a link app"}).json()["id"]
    snap = wait_until(lambda: (s := client.get(f"/api/sessions/{sid}").json())["gate"] and not s["running"] and s)
    return sid, snap


def gate_body(snap, action="approve", **kwargs):
    return {
        "action": action, "draft_hash": snap["gate"]["draft_hash"],
        "gate_seq": snap["gate"]["gate_seq"], **kwargs,
    }


def test_templates_and_empty_session_list(client):
    names = {t["name"] for t in client.get("/api/templates").json()}
    assert {"software", "decision", "design"} <= names
    assert client.get("/api/sessions").json() == []


def test_start_a_debate_and_reach_the_gate(client):
    sid = client.post("/api/sessions", json={"idea": "a link app"}).json()["id"]

    snap = wait_until(lambda: (s := client.get(f"/api/sessions/{sid}").json())["gate"] and s)
    assert snap["state"] == "GATE_1"
    assert snap["gate"]["options"] == ["approve", "edit", "revise", "reject"]
    assert snap["summary"]  # the proposer's In-short reaches the browser
    assert "## Problem statement" in snap["document"]

    listed = client.get("/api/sessions").json()
    assert listed[0]["id"] == sid and listed[0]["state"] == "GATE_1"


def test_the_event_stream_replays_the_whole_debate(client):
    sid = client.post("/api/sessions", json={"idea": "a link app"}).json()["id"]
    wait_until(lambda: client.get(f"/api/sessions/{sid}").json()["gate"])

    types = []
    with client.stream("GET", f"/api/sessions/{sid}/events") as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                if ev.get("type") == "_eof":
                    break
                types.append(ev["type"])
    assert types.count("draft") == 3
    assert types.count("verdict") == 3
    assert "gate_pending" in types


def test_answer_the_gate_from_the_browser(client):
    sid, snap = open_gate(client)

    assert client.post(f"/api/sessions/{sid}/gate", json=gate_body(snap, actor="mauro")).json() == {"ok": True}
    snap = wait_until(lambda: (s := client.get(f"/api/sessions/{sid}").json())["state"] == "DONE" and s)
    assert snap["gate"] is None and not snap["running"]


def test_gate_requires_the_version_reviewed_by_the_browser(client):
    sid, snap = open_gate(client)
    endpoint = f"/api/sessions/{sid}/gate"
    assert client.post(endpoint, json={"action": "approve"}).status_code == 422
    assert client.post(endpoint, json=gate_body(snap, draft_hash="stale")).status_code == 409
    assert client.post(endpoint, json=gate_body(snap, gate_seq=snap["gate"]["gate_seq"] + 1)).status_code == 409
    assert client.get(f"/api/sessions/{sid}").json()["state"] == "GATE_1"


def test_a_stale_browser_cannot_approve_a_reopened_gate(client, tmp_path):
    sid, snap = open_gate(client)
    store = SessionStore(sid, root=tmp_path)
    # A later review may open a different gate even if the text is unchanged.
    payload = {key: value for key, value in snap["gate"].items() if key != "gate_seq"}
    reopened = store.append("gate_pending", payload)
    response = client.post(f"/api/sessions/{sid}/gate", json=gate_body(snap))
    assert response.status_code == 409
    current = client.get(f"/api/sessions/{sid}").json()
    assert current["gate"]["gate_seq"] == reopened.seq
    assert current["gate"]["draft_hash"] == snap["gate"]["draft_hash"]
    assert not any(e.type == "gate_decision" for e in store.events())


def test_duplicate_and_concurrent_gate_submissions_have_one_winner(client, tmp_path):
    sid, snap = open_gate(client)
    barrier = Barrier(2)

    def submit():
        barrier.wait(timeout=5)
        return client.post(f"/api/sessions/{sid}/gate", json=gate_body(snap)).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert sorted(results) == [200, 409]
    wait_until(lambda: client.get(f"/api/sessions/{sid}").json()["state"] == "DONE")
    assert client.post(f"/api/sessions/{sid}/gate", json=gate_body(snap)).status_code == 409
    events = list(SessionStore(sid, root=tmp_path).events())
    assert sum(e.type == "gate_decision" for e in events) == 1
    assert len({e.seq for e in events}) == len(events)


def test_a_running_session_rejects_another_writer(tmp_path, monkeypatch):
    monkeypatch.setattr("duet.web.runner.providers_for", lambda _: {})
    cfg = roster("sol", "opus")
    runner = Runner(config=cfg, root=tmp_path)
    store = SessionStore.new(root=tmp_path)
    entered, release = Event(), Event()

    def drive(machine):
        entered.set()
        assert release.wait(timeout=5)

    runner._run_in_background(store, cfg, drive)
    try:
        assert entered.wait(timeout=5)
        with pytest.raises(GateConflict, match="still running"):
            runner._run_in_background(store, cfg, drive)
        with pytest.raises(GateConflict, match="still running"):
            runner.raise_budget(store.session_id, 20)
        assert not any(event.type == "budget" for event in store.events())
    finally:
        release.set()
        runner._threads[store.session_id].join(timeout=5)


def test_empty_edit_is_rejected_without_approving(client):
    sid, snap = open_gate(client)
    response = client.post(f"/api/sessions/{sid}/gate", json=gate_body(snap, "edit", edited_markdown=" \n"))
    assert response.status_code == 400
    current = client.get(f"/api/sessions/{sid}").json()
    assert current["state"] == "GATE_1" and current["gate"]


def test_edited_document_is_canonical_in_log_artifact_and_snapshot(client, tmp_path):
    sid, snap = open_gate(client)
    edited = "# Human revision\n\n## In short\n\nThe approved edit.\n\n## Requirements\n\nFIXED: work offline."
    response = client.post(f"/api/sessions/{sid}/gate", json=gate_body(snap, "edit", edited_markdown=edited, actor="mauro"))
    assert response.status_code == 200
    current = wait_until(lambda: (s := client.get(f"/api/sessions/{sid}").json())["state"] == "DONE" and not s["running"] and s)
    assert current["document"] == edited
    assert current["summary"] == "The approved edit."
    assert current["draft_hash"] == hashlib.sha256(edited.encode()).hexdigest()[:12]
    assert current["counts"]["fixed"] == 1
    store = SessionStore(sid, root=tmp_path)
    assert (store.dir / "artifacts" / "IMPLEMENTATION.md").read_text() == edited
    revisions = [e.payload for e in store.events() if e.type == "artifact_revision"]
    assert len(revisions) == 1
    assert revisions[0]["gate_seq"] == snap["gate"]["gate_seq"]
    assert revisions[0]["previous_hash"] == snap["draft_hash"]
    assert revisions[0]["actor"] == "mauro"
    assert current["round"] == snap["round"]  # an edit is not another debate round


def test_legacy_edits_are_visible_without_a_revision_event(client, tmp_path):
    sid, snap = open_gate(client)
    store = SessionStore(sid, root=tmp_path)
    edited = "# Legacy human edit\n\nFIXED: preserve this text."
    store.append("gate_decision", {"gate": "gate_1", "action": "edit", "actor": "mauro",
                                  "draft_hash": snap["draft_hash"], "edited_markdown": edited})
    store.save_artifact("IMPLEMENTATION.md", edited)
    store.append("state_change", {"from": "GATE_1", "to": "DONE"})
    current = client.get(f"/api/sessions/{sid}").json()
    assert current["document"] == edited and current["gate"] is None


def test_snapshot_progress_and_roster_are_derived_from_the_session(client, tmp_path):
    sid, snap = open_gate(client)
    assert [p["id"] for p in snap["participants"]] == ["sol", "opus"]
    assert snap["round"] == 3
    assert snap["counts"]["objections_raised"] == 2
    assert snap["counts"]["objections_closed"] == 2
    assert snap["counts"]["objections_open"] == 0
    assert len(snap["objections"]) == 2
    assert all(o["status"] == "resolved" for o in snap["objections"])
    assert snap["active_call"] is None
    events = list(SessionStore(sid, root=tmp_path).events())
    assert snap["created_at"] == events[0].ts
    assert snap["updated_at"] == events[-1].ts
    listed = client.get("/api/sessions").json()[0]
    assert listed["created_at"] == snap["created_at"]
    assert listed["updated_at"] == snap["updated_at"]


def test_active_call_only_reports_unfinished_work(client, tmp_path, monkeypatch):
    sid, _ = open_gate(client)
    store = SessionStore(sid, root=tmp_path)
    call = store.append("model_call", {"participant": "sol", "purpose": "critique", "round": 4,
                                       "model_id": "model-sol", "system": "private prompt"})
    monkeypatch.setattr(client.app.state.runner, "is_running", lambda _: True)
    snap = client.get(f"/api/sessions/{sid}").json()
    assert snap["active_call"] == {"participant": "sol", "purpose": "critique", "round": 4,
                                   "model_id": "model-sol", "seq": call.seq, "started_at": call.ts}
    assert snap["round"] == 4
    store.append("model_response", {"participant": "sol"})
    assert client.get(f"/api/sessions/{sid}").json()["active_call"] is None


def test_a_decision_debate_produces_a_decision_document(client, monkeypatch):
    from duet.schemas.templates import draft_model_for, get_template

    def decision_draft(title, addresses=None):
        m = draft_model_for(get_template("decision"))
        body = {s.id: f"{title} — {s.heading}" for s in get_template("decision").sections}
        return m(title=title, summary=f"{title}.", addresses=addresses or [], **body)

    scripts = {
        "sol": [decision_draft("v1"), objects("Options considered", "no do-nothing"), decision_draft("v3", ["OBJ-2"])],
        "opus": [objects("Rationale", "thin"), decision_draft("v2", ["OBJ-1"]), agrees()],
    }
    monkeypatch.setattr("duet.web.runner.providers_for", lambda c: {
        pid: FakeProvider("a", f"m-{pid}", [parsed(o) for o in sc]) for pid, sc in scripts.items()})

    sid = client.post("/api/sessions", json={"idea": "adopt X?", "template": "decision"}).json()["id"]
    snap = wait_until(lambda: (s := client.get(f"/api/sessions/{sid}").json())["gate"] and s)
    assert snap["template"] == "decision"
    assert "## Recommendation" in snap["document"]


def test_unknown_template_is_rejected(client):
    r = client.post("/api/sessions", json={"idea": "x", "template": "nonsense"})
    assert r.status_code == 400


def test_missing_session_is_404(client):
    assert client.get("/api/sessions/deadbeef").status_code == 404


def test_the_index_page_serves(client):
    r = client.get("/")
    assert r.status_code == 200 and "duet" in r.text.lower()


def test_profiles_endpoint_lists_profiles_and_a_default_budget(client, tmp_path):
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "cheap.toml").write_text("[budget]\nmax_usd = 2.0\n")
    body = client.get("/api/profiles").json()
    assert "cheap" in body["profiles"]
    assert body["default_budget"] > 0


def test_budget_and_profile_are_recorded_on_the_session(client):
    sid = client.post("/api/sessions", json={"idea": "x", "budget": 3.5}).json()["id"]
    wait_until(lambda: client.get(f"/api/sessions/{sid}").json()["gate"])
    assert client.get(f"/api/sessions/{sid}").json()["budget_usd"] == 3.5


def test_a_budget_stop_can_be_raised_from_the_browser(client):
    # A cap of a fraction of a cent stops within the first couple of turns.
    sid = client.post("/api/sessions", json={"idea": "x", "budget": 0.0025}).json()["id"]
    snap = wait_until(lambda: (s := client.get(f"/api/sessions/{sid}").json())["state"] == "BUDGET_STOP" and s)
    assert snap["budget_usd"] == 0.0025

    # Raising the cap records it and lifts the stop — the session leaves
    # BUDGET_STOP and the higher cap persists (readable on a later resume).
    assert client.post(f"/api/sessions/{sid}/budget", json={"budget": 25.0}).json() == {"ok": True}
    resumed = wait_until(lambda: (s := client.get(f"/api/sessions/{sid}").json())["state"] != "BUDGET_STOP" and s)
    assert resumed["budget_usd"] == 25.0


def test_unknown_profile_is_rejected(client, tmp_path):
    r = client.post("/api/sessions", json={"idea": "x", "profile": "ghost"})
    assert r.status_code == 400


# --- project documentation in the UI ----------------------------------------


def test_docs_are_listed_and_readable(client):
    docs = client.get("/api/docs").json()
    ids = {d["id"] for d in docs}
    assert {"readme", "plan", "specs"} <= ids

    body = client.get("/api/docs/plan").json()
    assert body["title"] == "Project plan"
    assert body["markdown"].startswith("# Duet")
    assert "|" in body["markdown"]  # tables, which the renderer must handle


def test_an_unknown_doc_is_404_and_paths_cannot_traverse(client):
    assert client.get("/api/docs/nope").status_code == 404
    # The id maps to a fixed filename, so traversal has nothing to grab.
    assert client.get("/api/docs/..%2F..%2Fetc%2Fpasswd").status_code == 404
