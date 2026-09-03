"""The web UI is a client of the event log (SPECS §6.0): it starts debates,
streams them, and answers gates, all through the same Machine the CLI drives.

Providers are faked and the debate runs on a background thread, so these tests
wait for the run to reach a gate rather than asserting instantly.
"""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeProvider, agrees, objects, parsed, roster, spec

from duet.web import app as webapp
from duet.web.runner import Runner

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
    sid = client.post("/api/sessions", json={"idea": "a link app"}).json()["id"]
    wait_until(lambda: client.get(f"/api/sessions/{sid}").json()["gate"])

    assert client.post(f"/api/sessions/{sid}/gate", json={"action": "approve", "actor": "mauro"}).json() == {"ok": True}
    snap = wait_until(lambda: (s := client.get(f"/api/sessions/{sid}").json())["state"] == "DONE" and s)
    assert snap["gate"] is None and not snap["running"]


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
