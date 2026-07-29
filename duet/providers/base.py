"""Provider protocol, retry wrapper, and registry (SPECS §3)."""

from __future__ import annotations

import random
import time
from typing import Protocol, runtime_checkable

from duet.schemas.config import DuetConfig
from duet.schemas.model_io import ModelRequest, ModelResponse

MAX_ATTEMPTS = 5


@runtime_checkable
class Provider(Protocol):
    name: str
    model_id: str

    def complete(self, req: ModelRequest) -> ModelResponse: ...


class RetryableError(Exception):
    """Raised by adapters on 429/5xx-class failures."""


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


def compute_cost(config: DuetConfig, model_id: str, input_tokens: int, output_tokens: int) -> float:
    pricing = config.pricing_for(model_id)
    if pricing is None:
        return 0.0
    return (
        input_tokens * pricing.input_per_mtok
        + output_tokens * pricing.output_per_mtok
    ) / 1_000_000


def get_provider(name: str, config: DuetConfig) -> Provider:
    """Registry: provider name -> configured adapter. Lazy SDK imports."""
    if name == "openai":
        from duet.providers.openai_ import OpenAIProvider

        return OpenAIProvider(model_id=config.models.openai, config=config)
    if name == "anthropic":
        from duet.providers.anthropic_ import AnthropicProvider

        return AnthropicProvider(model_id=config.models.anthropic, config=config)
    raise ValueError(f"unknown provider: {name!r}")
