"""M0 exit criterion, deterministically: every participant answers through the
same interface; usage and cost land in the session log keyed by participant."""

from pathlib import Path

from tests.conftest import FakeProvider, make_response

from duet.schemas.config import DuetConfig, Participant
from duet.schemas.model_io import ModelRequest, Turn
from duet.store.session import SessionStore


def ping_roster(store: SessionStore, cfg: DuetConfig, providers: dict[str, FakeProvider]) -> None:
    req = ModelRequest(system="ping", messages=[Turn(role="user", content="pong?")])
    for p in cfg.participants:
        resp = providers[p.id].complete(req)
        store.append("model_response", {"participant": p.id, "provider": p.provider, "text": resp.text})
        store.add_cost(resp.model_id, resp.usage.model_dump(), cap_usd=25.0)


def test_ping_default_pair_logged(tmp_path: Path):
    cfg = DuetConfig()
    providers = {
        p.id: FakeProvider(p.provider, p.model, [make_response("pong", p.model)])
        for p in cfg.participants
    }
    store = SessionStore.new(root=tmp_path)
    ping_roster(store, cfg, providers)

    events = [e for e in store.events() if e.type != "session_start"]
    assert [e.type for e in events] == [
        "model_response", "cost_tick", "model_response", "cost_tick",
    ]
    assert store.cost_usd > 0
    tick = events[1].payload
    assert tick["model_id"] == "gpt-5.4-mini"
    assert tick["input_tokens"] == 10


def test_ping_scales_past_two_participants(tmp_path: Path):
    """Three voices, two of them on one provider — the log stays unambiguous
    because events are keyed by participant id, not provider name."""
    cfg = DuetConfig(
        participants=[
            Participant(id="sol", provider="openai", model="gpt-5.6-sol"),
            Participant(id="opus", provider="anthropic", model="claude-opus-5"),
            Participant(id="haiku", provider="anthropic", model="claude-haiku-4-5"),
        ]
    )
    providers = {
        p.id: FakeProvider(p.provider, p.model, [make_response("pong", p.model)])
        for p in cfg.participants
    }
    store = SessionStore.new(root=tmp_path)
    ping_roster(store, cfg, providers)

    responses = [e for e in store.events() if e.type == "model_response"]
    assert [e.payload["participant"] for e in responses] == ["sol", "opus", "haiku"]
    anthropic_ids = [e.payload["participant"] for e in responses if e.payload["provider"] == "anthropic"]
    assert anthropic_ids == ["opus", "haiku"]
    assert len([e for e in store.events() if e.type == "cost_tick"]) == 3
