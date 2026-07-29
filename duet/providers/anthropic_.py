"""Anthropic adapter (Messages API). M0: text + structured output; tools in M2."""

from __future__ import annotations

import json

from pydantic import BaseModel

from duet.providers.base import RetryableError, compute_cost, with_retries
from duet.schemas.config import DuetConfig
from duet.schemas.model_io import ModelRequest, ModelResponse, Usage


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model_id: str, config: DuetConfig, client=None):
        self.model_id = model_id
        self.config = config
        if client is None:
            import anthropic  # lazy import

            client = anthropic.Anthropic()
        self.client = client

    def complete(self, req: ModelRequest) -> ModelResponse:
        if req.tools:
            raise NotImplementedError("tool use lands in M2")

        system = req.system
        if req.response_schema is not None:
            # Structured output via instruction + JSON extraction; native
            # schema mode can replace this when available in the SDK.
            schema = json.dumps(req.response_schema.model_json_schema())
            system += (
                "\n\nRespond ONLY with a JSON object valid against this schema, "
                "no prose, no code fences:\n" + schema
            )

        def call():
            try:
                return self.client.messages.create(
                    model=self.model_id,
                    system=system,
                    messages=[
                        {"role": t.role, "content": t.content} for t in req.messages
                    ],
                    max_tokens=req.max_output_tokens,
                )
            except Exception as e:  # noqa: BLE001
                status = getattr(e, "status_code", None)
                if status == 429 or (status is not None and status >= 500):
                    raise RetryableError(str(e)) from e
                raise

        resp = with_retries(call)
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )

        parsed: BaseModel | None = None
        if req.response_schema is not None and text:
            raw = text.strip()
            if raw.startswith("```"):
                raw = raw.strip("`").removeprefix("json").strip()
            parsed = req.response_schema.model_validate(json.loads(raw))

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
                cost_usd=compute_cost(self.config, self.model_id, input_tokens, output_tokens),
            ),
            model_id=self.model_id,
        )
