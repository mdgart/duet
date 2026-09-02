"""Output truncation is its own failure, not malformed JSON (SPECS §3).

A truncated structured reply fails deep inside the JSON parser as
"Unterminated string", which reads like a broken model rather than a cap that
needs raising. Both adapters surface it explicitly instead.
"""

from types import SimpleNamespace

import pytest

from duet.providers.anthropic_ import AnthropicProvider
from duet.providers.base import OutputTruncated
from duet.providers.openai_ import OpenAIProvider
from duet.schemas.config import DuetConfig
from duet.schemas.model_io import ModelRequest, Turn

REQ = ModelRequest(system="s", messages=[Turn(role="user", content="u")], max_output_tokens=8192)


class FakeOpenAI:
    def __init__(self, status: str):
        self.responses = SimpleNamespace(create=lambda **kw: SimpleNamespace(
            status=status,
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            output_text='{"partial": "cut off here',
            usage=SimpleNamespace(input_tokens=10, output_tokens=8192, input_tokens_details=None),
        ))


class _Stream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class FakeAnthropic:
    def __init__(self, stop_reason: str):
        message = SimpleNamespace(
            stop_reason=stop_reason,
            content=[SimpleNamespace(type="text", text='{"partial": "cut off here')],
            usage=SimpleNamespace(input_tokens=10, output_tokens=8192, cache_read_input_tokens=0),
        )
        self.messages = SimpleNamespace(stream=lambda **kw: _Stream(message))


def test_openai_incomplete_response_raises_truncated():
    provider = OpenAIProvider("gpt-5.6-sol", DuetConfig(), client=FakeOpenAI("incomplete"))
    with pytest.raises(OutputTruncated, match="max_output_tokens"):
        provider.complete(REQ)


def test_anthropic_max_tokens_stop_raises_truncated():
    provider = AnthropicProvider("claude-opus-5", DuetConfig(), client=FakeAnthropic("max_tokens"))
    with pytest.raises(OutputTruncated, match="max_tokens"):
        provider.complete(REQ)


def test_a_complete_response_is_not_flagged():
    provider = OpenAIProvider("gpt-5.6-sol", DuetConfig(), client=FakeOpenAI("completed"))
    assert provider.complete(REQ).usage.output_tokens == 8192

    provider = AnthropicProvider("claude-opus-5", DuetConfig(), client=FakeAnthropic("end_turn"))
    assert provider.complete(REQ).usage.output_tokens == 8192


def test_the_engine_asks_for_brevity_rather_than_a_rerun(tmp_path):
    """A truncation retry must change the instruction: re-emitting the same
    spec at the same cap just truncates again."""
    from duet.orchestrator.debate import DebateEngine, SchemaRepairFailed
    from duet.schemas.config import Participant
    from duet.store.session import SessionStore

    class AlwaysTruncates:
        name, model_id = "fake", "m"

        def __init__(self):
            self.requests = []

        def complete(self, req):
            self.requests.append(req)
            raise OutputTruncated("m stopped early (max_output_tokens) at max_output_tokens=16384")

    cfg = DuetConfig(participants=[
        Participant(id="a", provider="anthropic", model="m"),
        Participant(id="b", provider="anthropic", model="m"),
    ])
    provider = AlwaysTruncates()
    engine = DebateEngine(cfg, SessionStore.new(root=tmp_path), {"a": provider, "b": provider})
    with pytest.raises(SchemaRepairFailed):
        engine.run("an app")

    assert len(provider.requests) == 2  # one repair round, then a hard error
    retry = provider.requests[1].messages[-1].content
    assert "cut off at the output limit" in retry
    assert "materially shorter" in retry
