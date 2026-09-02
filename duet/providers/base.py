"""Provider protocol, retry wrapper, and adapter registry (SPECS §3)."""

from __future__ import annotations

import random
import time
from typing import Callable, Iterator, Protocol, runtime_checkable

from duet.schemas.config import DuetConfig, ModelSpec
from duet.schemas.model_io import Chunk, ModelRequest, ModelResponse

MAX_ATTEMPTS = 5


@runtime_checkable
class Provider(Protocol):
    name: str
    model_id: str

    def complete(self, req: ModelRequest) -> ModelResponse: ...
    def stream(self, req: ModelRequest) -> Iterator[Chunk]: ...


ProviderFactory = Callable[[ModelSpec, DuetConfig], Provider]

_FACTORIES: dict[str, ProviderFactory] = {}


class RetryableError(Exception):
    """Raised by adapters on 429/5xx-class failures."""


class OutputTruncated(Exception):
    """The model hit its output cap mid-answer.

    Worth its own type: truncated structured output fails as a JSON decode
    error deep inside the parser, which reads like a malformed reply rather
    than a limit that needs raising.
    """


def with_retries(fn, *, max_attempts: int = MAX_ATTEMPTS, base_delay: float = 1.0):
    """Call fn() with exponential backoff on RetryableError."""
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except RetryableError:
            if attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1)) * (0.5 + random.random())
            time.sleep(delay)
    raise AssertionError("unreachable")


def compute_cost(
    config: DuetConfig,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> float:
    """Cost of one turn. `input_tokens` includes cached ones, as providers report
    them, so the cached share is split out before pricing rather than added."""
    pricing = config.pricing_for(model_id)
    if pricing is None:
        return 0.0
    cached = min(cached_input_tokens, input_tokens) if pricing.cached_input_per_mtok else 0
    fresh = input_tokens - cached
    return (
        fresh * pricing.input_per_mtok
        + cached * (pricing.cached_input_per_mtok or 0.0)
        + output_tokens * pricing.output_per_mtok
    ) / 1_000_000


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Add an adapter to the registry. Adding a lab is this call plus a module."""
    _FACTORIES[name] = factory


def known_providers() -> list[str]:
    return sorted(_FACTORIES)


def _openai_factory(spec: ModelSpec, config: DuetConfig) -> Provider:
    from duet.providers.openai_ import OpenAIProvider  # lazy SDK import

    return OpenAIProvider(model_id=spec.model, config=config)


def _anthropic_factory(spec: ModelSpec, config: DuetConfig) -> Provider:
    from duet.providers.anthropic_ import AnthropicProvider  # lazy SDK import

    return AnthropicProvider(model_id=spec.model, config=config)


register_provider("openai", _openai_factory)
register_provider("anthropic", _anthropic_factory)


def build_provider(spec: ModelSpec, config: DuetConfig) -> Provider:
    """Instantiate the adapter for any model spec — participant or utility."""
    factory = _FACTORIES.get(spec.provider)
    if factory is None:
        raise ValueError(
            f"unknown provider {spec.provider!r}; registered: {', '.join(known_providers())}"
        )
    return factory(spec, config)


def provider_for(config: DuetConfig, participant_id: str) -> Provider:
    """Adapter for one roster participant, by id."""
    return build_provider(config.roster.get(participant_id), config)


def single_chunk(resp: ModelResponse) -> Iterator[Chunk]:
    """The documented fallback for requests an adapter cannot stream — notably
    structured-output modes, which is every Phase A call (SPECS §3)."""
    yield Chunk(text=resp.text, done=True, usage=resp.usage, streamed=False)
