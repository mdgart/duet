"""OpenAI adapter (Responses API). M0: text + structured output; tools in M2."""

from __future__ import annotations

import copy
import json

from pydantic import BaseModel

from typing import Iterator

from duet.providers.base import (
    OutputTruncated,
    RetryableError,
    compute_cost,
    single_chunk,
    with_retries,
)
from duet.schemas.config import DuetConfig
from duet.schemas.model_io import Chunk, ModelRequest, ModelResponse, Usage


# Strict mode accepts a narrow JSON Schema dialect: every object must forbid
# extra keys and mark every property required, and the numeric/string
# constraint keywords are rejected outright. Pydantic emits none of that, so
# the schema is rewritten on the way out. Dropping the constraints costs
# nothing — pydantic still enforces them when it validates the reply.
_UNSUPPORTED = (
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems",
    "default", "format",
)


def strict_json_schema(schema: dict) -> dict:
    """Rewrite a pydantic JSON schema into OpenAI's strict dialect."""
    schema = copy.deepcopy(schema)

    def walk(node) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        for key in _UNSUPPORTED:
            node.pop(key, None)
        properties = node.get("properties")
        if properties is not None:
            node["additionalProperties"] = False
            # Strict mode has no notion of an optional field: everything is
            # required, and a model with nothing to say emits an empty value.
            node["required"] = list(properties)
            for subschema in properties.values():
                walk(subschema)
        for key in ("items", "additionalItems", "not"):
            if key in node:
                walk(node[key])
        for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
            if key in node:
                walk(node[key])
        if "$defs" in node:
            for subschema in node["$defs"].values():
                walk(subschema)

    walk(schema)
    return schema


class OpenAIProvider:
    name = "openai"

    def __init__(self, model_id: str, config: DuetConfig, client=None):
        self.model_id = model_id
        self.config = config
        if client is None:
            from openai import OpenAI  # lazy import

            client = OpenAI()
        self.client = client

    def _kwargs(self, req: ModelRequest) -> dict:
        kwargs: dict = {
            "model": self.model_id,
            "instructions": req.system,
            "input": [{"role": t.role, "content": t.content} for t in req.messages],
            "max_output_tokens": req.max_output_tokens,
        }
        if req.response_schema is not None:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": req.response_schema.__name__,
                    "schema": strict_json_schema(req.response_schema.model_json_schema()),
                    "strict": True,
                }
            }
        return kwargs

    def complete(self, req: ModelRequest) -> ModelResponse:
        if req.tools:
            raise NotImplementedError("tool use lands in M2")
        kwargs = self._kwargs(req)

        def call():
            try:
                return self.client.responses.create(**kwargs)
            except Exception as e:  # noqa: BLE001
                status = getattr(e, "status_code", None)
                if status == 429 or (status is not None and status >= 500):
                    raise RetryableError(str(e)) from e
                raise

        resp = with_retries(call)
        if getattr(resp, "status", None) == "incomplete":
            reason = getattr(getattr(resp, "incomplete_details", None), "reason", "unknown")
            raise OutputTruncated(
                f"{self.model_id} stopped early ({reason}) at "
                f"max_output_tokens={req.max_output_tokens}"
            )
        text = getattr(resp, "output_text", "") or ""

        parsed: BaseModel | None = None
        if req.response_schema is not None and text:
            parsed = req.response_schema.model_validate(json.loads(text))

        u = getattr(resp, "usage", None)
        input_tokens = getattr(u, "input_tokens", 0) or 0
        output_tokens = getattr(u, "output_tokens", 0) or 0
        cached = 0
        details = getattr(u, "input_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0

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

        kwargs = self._kwargs(req) | {"stream": True}
        text_parts: list[str] = []
        usage = Usage()
        for event in self.client.responses.create(**kwargs):
            kind = getattr(event, "type", "")
            if kind == "response.output_text.delta":
                delta = getattr(event, "delta", "") or ""
                text_parts.append(delta)
                yield Chunk(text=delta)
            elif kind == "response.completed":
                u = getattr(getattr(event, "response", None), "usage", None)
                input_tokens = getattr(u, "input_tokens", 0) or 0
                output_tokens = getattr(u, "output_tokens", 0) or 0
                usage = Usage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=compute_cost(self.config, self.model_id, input_tokens, output_tokens),
                )
        yield Chunk(done=True, usage=usage)
