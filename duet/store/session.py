"""Event-sourced JSONL session store + cost meter (SPECS §10, §7.2 budget)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Iterator

from duet.schemas.config import duet_home
from duet.schemas.events import Event, EventType


class BudgetExceeded(Exception):
    def __init__(self, spent: float, cap: float):
        super().__init__(f"budget exceeded: ${spent:.2f} of ${cap:.2f} cap")
        self.spent = spent
        self.cap = cap


class SessionStore:
    """One directory per session; append-only log.jsonl is the source of truth."""

    def __init__(self, session_id: str, root: Path | None = None):
        self.session_id = session_id
        self.dir = (root or duet_home() / "sessions") / session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "artifacts").mkdir(exist_ok=True)
        self.log_path = self.dir / "log.jsonl"
        self._seq = self._last_seq()
        self._cost_usd = self._replay_cost()

    @classmethod
    def new(cls, root: Path | None = None) -> "SessionStore":
        return cls(uuid.uuid4().hex[:12], root=root)

    @classmethod
    def list_sessions(cls, root: Path | None = None) -> list[str]:
        base = root or duet_home() / "sessions"
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir() if (p / "log.jsonl").is_file())

    # -- event log ---------------------------------------------------------

    def append(self, type: EventType, payload: dict[str, Any]) -> Event:
        self._seq += 1
        event = Event(seq=self._seq, type=type, payload=payload)
        with self.log_path.open("a") as f:
            f.write(event.model_dump_json() + "\n")
        if type == "cost_tick":
            self._cost_usd += float(payload.get("cost_usd", 0.0))
        return event

    def events(self) -> Iterator[Event]:
        if not self.log_path.is_file():
            return
        with self.log_path.open() as f:
            for line in f:
                if line.strip():
                    yield Event.model_validate(json.loads(line))

    def _last_seq(self) -> int:
        seq = 0
        for e in self.events():
            seq = e.seq
        return seq

    # -- cost meter --------------------------------------------------------

    def _replay_cost(self) -> float:
        return sum(
            float(e.payload.get("cost_usd", 0.0))
            for e in self.events()
            if e.type == "cost_tick"
        )

    @property
    def cost_usd(self) -> float:
        return self._cost_usd

    def add_cost(self, model_id: str, usage: dict[str, Any], cap_usd: float) -> None:
        """Record a cost tick; raise BudgetExceeded once the cap is crossed."""
        self.append(
            "cost_tick",
            {
                "model_id": model_id,
                "cost_usd": usage.get("cost_usd", 0.0),
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_usd": round(
                    self._cost_usd + float(usage.get("cost_usd", 0.0)), 6
                ),
            },
        )
        if self._cost_usd >= cap_usd:
            raise BudgetExceeded(self._cost_usd, cap_usd)

    # -- artifacts ---------------------------------------------------------

    def save_artifact(self, name: str, content: str) -> str:
        path = self.dir / "artifacts" / name
        path.write_text(content)
        return f"artifacts/{name}"
