"""Anthropic structured output goes through a forced tool, not prose JSON.

SPECS §3 calls for each API's native schema mode. Asking for raw JSON in the
system prompt echoes the whole schema into the request, invites code fences and
preamble, and burns output tokens the model needs for the answer.
"""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from duet.providers.anthropic_ import STRUCTURED_TOOL, AnthropicProvider
from duet.schemas.config import DuetConfig
from duet.schemas.model_io import ModelRequest, Turn


class Shape(BaseModel):
    name: str
    sides: int


def request(**kw) -> ModelRequest:
    return ModelRequest(system="be useful", messages=[Turn(role="user", content="go")], **kw)


class _StreamContext:
    """Mimics the SDK's streaming helper: a context manager that reassembles
    the message. complete() goes through this, not through create()."""

    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class RecordingClient:
    def __init__(self, content, stop_reason="end_turn"):
        self.calls: list[dict] = []
        outer = self

        def stream(**kwargs):
            outer.calls.append(kwargs)
            return _StreamContext(
                SimpleNamespace(
                    content=content,
                    stop_reason=stop_reason,
                    usage=SimpleNamespace(
                        input_tokens=7, output_tokens=11, cache_read_input_tokens=0
                    ),
                )
            )

        self.messages = SimpleNamespace(stream=stream)


def tool_block(payload: dict):
    return SimpleNamespace(type="tool_use", name=STRUCTURED_TOOL, input=payload)


def test_schema_request_forces_a_tool_and_parses_its_input():
    client = RecordingClient([tool_block({"name": "square", "sides": 4})], stop_reason="tool_use")
    provider = AnthropicProvider("claude-opus-5", DuetConfig(), client=client)

    response = provider.complete(request(response_schema=Shape))

    sent = client.calls[0]
    assert sent["tool_choice"] == {"type": "tool", "name": STRUCTURED_TOOL}
    assert sent["tools"][0]["input_schema"]["properties"].keys() == {"name", "sides"}
    assert response.parsed == Shape(name="square", sides=4)
    assert response.usage.output_tokens == 11


def test_the_schema_no_longer_pollutes_the_system_prompt():
    client = RecordingClient([tool_block({"name": "tri", "sides": 3})], stop_reason="tool_use")
    AnthropicProvider("claude-opus-5", DuetConfig(), client=client).complete(
        request(response_schema=Shape)
    )
    assert client.calls[0]["system"] == "be useful"


def test_plain_text_requests_send_no_tools():
    client = RecordingClient([SimpleNamespace(type="text", text="hello")])
    provider = AnthropicProvider("claude-opus-5", DuetConfig(), client=client)

    response = provider.complete(request())

    assert "tools" not in client.calls[0] and "tool_choice" not in client.calls[0]
    assert response.text == "hello" and response.parsed is None


def test_a_missing_tool_block_is_an_explicit_error():
    """Better than returning None and failing later with no context."""
    client = RecordingClient([SimpleNamespace(type="text", text="I would rather chat")])
    provider = AnthropicProvider("claude-opus-5", DuetConfig(), client=client)

    with pytest.raises(ValueError, match=f"no {STRUCTURED_TOOL} block"):
        provider.complete(request(response_schema=Shape))
