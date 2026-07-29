"""OpenAI adapter (Responses API). M0: text + structured output; tools in M2."""

from __future__ import annotations

import json

from pydantic import BaseModel

from duet.providers.base import RetryableError, compute_cost, with_retries
from duet.schemas.config import DuetConfig
from duet.schemas.model_io import ModelRequest, ModelResponse, Usage


class OpenAIProvider:
    name = "openai"

    def __init__(self, model_id: str, config: DuetConfig, client=None):
        self.model_id = model_id
        self.config = config
        if client is None:
            from openai import OpenAI  # lazy import

            client = OpenAI()
        self.client = client

    def complete(self, req: ModelRequest) -> ModelResponse:
        if req.tools:
            raise NotImplementedError("tool use lands in M2")

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
                    "schema": req.response_schema.model_json_schema(),
                    "strict": True,
                }
            }

        def call():
            try:
                return self.client.responses.create(**kwargs)
            except Exception as e:  # noqa: BLE001
                status = getattr(e, "status_code", None)
                if status == 429 or (status is not None and status >= 500):
                    raise RetryableError(str(e)) from e
                raise

        resp = with_retries(call)
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
                cost_usd=compute_cost(self.config, self.model_id, input_tokens, output_tokens),
            ),
            model_id=self.model_id,
        )
