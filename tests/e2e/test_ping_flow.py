"""M0 exit criterion, deterministically: two providers answer through the
same interface; usage and cost land in the session log."""

from pathlib import Path

from tests.conftest import FakeProvider, make_response

from duet.schemas.model_io import ModelRequest, Turn
from duet.store.session import SessionStore


def test_ping_both_providers_logged(tmp_path: Path):
    providers = [
        FakeProvider("openai", "gpt-5.6-sol", [make_response("pong", "gpt-5.6-sol")]),
        FakeProvider("anthropic", "claude-opus-5", [make_response("pong", "claude-opus-5")]),
    ]
    store = SessionStore.new(root=tmp_path)
    req = ModelRequest(system="ping", messages=[Turn(role="user", content="pong?")])

    for p in providers:
        resp = p.complete(req)
        store.append("model_response", {"provider": p.name, "text": resp.text})
        store.add_cost(resp.model_id, resp.usage.model_dump(), cap_usd=25.0)

    events = list(store.events())
    assert [e.type for e in events] == [
        "model_response", "cost_tick", "model_response", "cost_tick",
    ]
    assert store.cost_usd > 0
    tick = events[1].payload
    assert tick["model_id"] == "gpt-5.6-sol"
    assert tick["input_tokens"] == 10
