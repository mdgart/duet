"""What each provider must actually do, verified against the real API.

Every assertion here corresponds to a bug that scripted providers could not
catch and a live call did:

  - OpenAI strict mode rejects the JSON Schema pydantic emits (optional
    properties, `minimum`/`maximum`), failing with a 400.
  - A model hitting its output cap returns truncated JSON, surfacing as an
    "unterminated string" from the parser rather than as a limit to raise.
  - Anthropic structured output needs a forced tool, not a schema pasted into
    the system prompt.
"""

from typing import Literal

import pytest
from pydantic import BaseModel, Field

from duet.providers.base import OutputTruncated
from duet.schemas.config import DebateConfig
from duet.schemas.debate import SpecDraft, Verdict
from duet.schemas.model_io import ModelRequest, Turn


class Ingredient(BaseModel):
    name: str
    grams: int


class Recipe(BaseModel):
    """Deliberately awkward: a nested model, a list, an enum, a field with a
    default, and a bounded float — the last two are what OpenAI strict mode
    rejects unless the schema is rewritten."""

    title: str
    difficulty: Literal["easy", "medium", "hard"]
    ingredients: list[Ingredient]
    notes: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


def ask(content: str, **kw) -> ModelRequest:
    return ModelRequest(
        system="You are terse and precise.",
        messages=[Turn(role="user", content=content)],
        **kw,
    )


def test_plain_text_completion_reports_usage(provider):
    response = provider.complete(ask("Reply with exactly: pong", max_output_tokens=64))
    assert "pong" in response.text.lower()
    assert response.usage.input_tokens > 0 and response.usage.output_tokens > 0
    assert response.model_id == provider.model_id


def test_structured_output_round_trips(provider):
    response = provider.complete(
        ask("Give a recipe for pancakes with two ingredients.",
            response_schema=Recipe, max_output_tokens=2048)
    )
    recipe = response.parsed
    assert isinstance(recipe, Recipe)
    assert recipe.difficulty in ("easy", "medium", "hard")
    assert recipe.ingredients and all(i.grams >= 0 for i in recipe.ingredients)
    assert 0.0 <= recipe.confidence <= 1.0
    assert response.usage.output_tokens > 0


@pytest.mark.parametrize("schema", [SpecDraft, Verdict], ids=["SpecDraft", "Verdict"])
def test_the_real_phase_a_schemas_are_accepted(provider, schema):
    """The regression that matters most: these are the only two schemas Phase A
    ever sends, so a dialect change that breaks them breaks every debate."""
    response = provider.complete(
        ask(f"Emit a minimal but valid {schema.__name__} about a todo CLI. Be brief.",
            response_schema=schema, max_output_tokens=8192)
    )
    assert isinstance(response.parsed, schema)


def test_the_production_output_cap_is_accepted(provider):
    """Exercise the cap the engine actually uses, not a convenient small one.

    Anthropic's SDK refuses a *non-streaming* request whose max_tokens implies
    the call could run past ten minutes — a limit invisible at the 2k and 8k
    caps the other tests use, and hit immediately at the real default. Cheap to
    check: the cap is a ceiling, and this asks for one word.
    """
    cap = DebateConfig().max_output_tokens
    response = provider.complete(ask("Reply with exactly: ok", max_output_tokens=cap))
    assert response.text.strip()
    assert response.usage.output_tokens > 0


def test_truncation_is_reported_as_truncation(provider):
    """Not as unterminated JSON from somewhere deep in the parser."""
    with pytest.raises(OutputTruncated):
        provider.complete(
            ask("Give a recipe with twenty ingredients and long notes.",
                response_schema=Recipe, max_output_tokens=16)
        )


def test_streaming_delivers_text_before_completion(provider):
    """The contract is that text arrives *before* the turn ends, not that it
    arrives in any particular number of pieces. How finely a provider slices
    its deltas is its own business — a short answer legitimately comes in one.
    What must hold is that content precedes the terminal chunk, and that the
    terminal chunk carries the usage the cost meter needs."""
    chunks = list(provider.stream(ask("Count from 1 to 20, space separated.",
                                      max_output_tokens=256)))

    body, final = chunks[:-1], chunks[-1]
    assert body, "no content arrived before the stream closed"
    assert all(not c.done for c in body)
    assert all(c.streamed for c in body), "text turns must not use the fallback path"
    assert final.done and final.usage is not None
    assert final.usage.output_tokens > 0
    assert "20" in "".join(c.text for c in chunks)


def test_structured_streaming_falls_back_to_one_flagged_chunk(provider):
    """SPECS §3 permits the fallback but requires it be visible in the event."""
    chunks = list(provider.stream(ask("A recipe for toast.",
                                      response_schema=Recipe, max_output_tokens=1024)))
    assert len(chunks) == 1
    assert chunks[0].done and chunks[0].streamed is False
