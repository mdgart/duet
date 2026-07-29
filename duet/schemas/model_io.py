"""Provider-neutral model I/O types (SPECS §3)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ToolDef(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float = 0.0


class ModelRequest(BaseModel):
    system: str
    messages: list[Turn]
    tools: list[ToolDef] | None = None
    response_schema: type[BaseModel] | None = None
    max_output_tokens: int = 4096

    model_config = {"arbitrary_types_allowed": True}


class ModelResponse(BaseModel):
    text: str
    parsed: BaseModel | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model_id: str = ""

    model_config = {"arbitrary_types_allowed": True}
