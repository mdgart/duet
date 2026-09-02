"""Event-sourced session log schema (SPECS §10)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    "session_start",
    "state_change",
    "model_call",
    "model_response",
    "model_chunk",
    "draft",
    "verdict",
    "tool_call",
    "tool_result",
    "test_run",
    "gate_pending",
    "gate_decision",
    "commit",
    "cost_tick",
    "error",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Event(BaseModel):
    seq: int
    ts: str = Field(default_factory=_now)
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
