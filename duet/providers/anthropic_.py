"""Anthropic adapter (Messages API). M0: text + structured output; agent tools in M2."""

from __future__ import annotations

import json
from typing import Iterator

from pydantic import BaseModel

from duet.providers.base import (
    OutputTruncated,
    RetryableError,
    compute_cost,
    single_chunk,
    with_retries,
)
from duet.schemas.config import DuetConfig
from duet.schemas.model_io import Chunk, ModelRequest, ModelResponse, Usage

STRUCTURED_TOOL = "emit_result"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model_id: str, config: DuetConfig, client=None):
        self.model_id = model_id
        self.config = config
        if client is None:
            import anthropic  # lazy import

            client = anthropic.Anthropic()
        self.client = client

    def _kwargs(self, req: ModelRequest) -> dict:
        kwargs: dict = {
            "model": self.model_id,
            "system": req.system,
            "messages": [{"role": t.role, "content": t.content} for t in req.messages],
            "max_tokens": req.max_output_tokens,
        }
        if req.response_schema is not None:
            # Native structured output is a single forced tool whose input_schema
            # is the target schema. The model fills a tool input rather than
            # writing JSON as prose, which removes three failure modes at once:
            # no code fences or preamble to strip, no schema echoed into the
            # system prompt, and no chance of a chatty wrapper around the object.
            kwargs["tools"] = [
                {
                    "name": STRUCTURED_TOOL,
                    "description": f"Return the {req.response_schema.__name__} result.",
                    "input_schema": req.response_schema.model_json_schema(),
                }
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": STRUCTURED_TOOL}
        return kwargs

    def complete(self, req: ModelRequest) -> ModelResponse:
        if req.tools:
            raise NotImplementedError("agent tool use lands in M2")

        kwargs = self._kwargs(req)

        def call():
            try:
                # Always over the streaming transport, even though this method
                # returns a completed message: the SDK refuses a non-streaming
                # request whose max_tokens implies it could run past ten
                # minutes, and Phase A's cap is well above that line. The
                # helper reassembles the message, so nothing else changes.
                with self.client.messages.stream(**kwargs) as stream:
                    return stream.get_final_message()
            except Exception as e:  # noqa: BLE001
                status = getattr(e, "status_code", None)
                if status == 429 or (status is not None and status >= 500):
                    raise RetryableError(str(e)) from e
                raise

        resp = with_retries(call)
        if getattr(resp, "stop_reason", None) == "max_tokens":
            raise OutputTruncated(
                f"{self.model_id} stopped early (max_tokens) at "
                f"max_output_tokens={req.max_output_tokens}"
            )

        parsed: BaseModel | None = None
        if req.response_schema is not None:
            block = next(
                (
                    b
                    for b in resp.content
                    if getattr(b, "type", "") == "tool_use"
                    and getattr(b, "name", "") == STRUCTURED_TOOL
                ),
                None,
            )
            if block is None:
                raise ValueError(
                    f"{self.model_id} returned no {STRUCTURED_TOOL} block "
                    f"(stop_reason={getattr(resp, 'stop_reason', None)!r})"
                )
            parsed = req.response_schema.model_validate(block.input)
            text = json.dumps(block.input)
        else:
            text = "".join(
                block.text for block in resp.content if getattr(block, "type", "") == "text"
            )

        u = resp.usage
        input_tokens = getattr(u, "input_tokens", 0) or 0
        output_tokens = getattr(u, "output_tokens", 0) or 0
        cached = getattr(u, "cache_read_input_tokens", 0) or 0

        return ModelResponse(
            text=text,
            parsed=parsed,
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached,
                cost_usd=compute_cost(
                    self.config, self.model_id, input_tokens, output_tokens, cached
                ),
            ),
            model_id=self.model_id,
        )

    def stream(self, req: ModelRequest) -> Iterator[Chunk]:
        """Incremental text where the API allows it. Structured-output requests
        fall back to one terminal chunk, flagged as such (SPECS §3)."""
        if req.tools or req.response_schema is not None:
            yield from single_chunk(self.complete(req))
            return

        input_tokens = output_tokens = 0
        for event in self.client.messages.create(**self._kwargs(req), stream=True):
            kind = getattr(event, "type", "")
            if kind == "content_block_delta":
                delta = getattr(getattr(event, "delta", None), "text", "") or ""
                if delta:
                    yield Chunk(text=delta)
            elif kind == "message_start":
                u = getattr(getattr(event, "message", None), "usage", None)
                input_tokens = getattr(u, "input_tokens", 0) or 0
            elif kind == "message_delta":
                u = getattr(event, "usage", None)
                output_tokens = getattr(u, "output_tokens", 0) or output_tokens
        yield Chunk(
            done=True,
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=compute_cost(self.config, self.model_id, input_tokens, output_tokens),
            ),
        )
