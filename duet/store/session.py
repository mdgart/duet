"""Event-sourced JSONL session store + cost meter (SPECS §10, §7.2 budget)."""

from __future__ import annotations

import json
import textwrap
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

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
        self._listeners: list[Callable[[Event], None]] = []

    def subscribe(self, listener: Callable[[Event], None]) -> None:
        """Called on every appended event. The CLI renders from this; a web
        backend would push the same stream to a browser (PLAN §6)."""
        self._listeners.append(listener)

    @classmethod
    def new(cls, root: Path | None = None, owner: str = "") -> "SessionStore":
        """Create a session, recording who owns it.

        Ownership is written once, at creation, because it cannot be recovered
        later: a session with no recorded owner is anonymous forever, and every
        multi-user feature — listing your own work, resuming it, deciding who
        may answer a gate — is a query against this field. In v1 the owner is
        the local user; in a hosted deployment it is an account id.

        The id is a full uuid4 rather than a truncated one so that it is never
        the weak link if a session id reaches a URL; `resolve` keeps it typable.
        """
        store = cls(uuid.uuid4().hex, root=root)
        store.append("session_start", {"owner": owner, "session_id": store.session_id})
        return store

    @classmethod
    def resolve(cls, prefix: str, root: Path | None = None) -> str:
        """Find a session by unambiguous id prefix, the way git resolves a sha."""
        sessions = cls.list_sessions(root)
        if prefix in sessions:
            return prefix
        matches = [s for s in sessions if s.startswith(prefix)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"no session id starts with {prefix!r}")
        raise ValueError(
            f"{prefix!r} is ambiguous — {len(matches)} sessions match: "
            + ", ".join(m[:12] for m in matches)
        )

    def label(self, width: int = 34) -> str:
        """What this session was about, in one line.

        Prefers the latest draft's title, since the participants name the thing
        they are designing far better than the opening brief does; falls back to
        the brief for sessions that have not produced a draft yet. Without this
        a listing is a wall of uuids and the only way to identify a session is
        to open its files.
        """
        title = brief = ""
        for event in self.events():
            if event.type == "draft":
                first = event.payload.get("markdown", "").lstrip().split("\n", 1)[0]
                if first.startswith("#"):
                    title = first.lstrip("#").strip()
            elif event.type == "state_change" and event.payload.get("brief") and not brief:
                brief = " ".join(event.payload["brief"].split())
        text = title or brief
        return textwrap.shorten(text, width, placeholder="…") if text else ""

    @property
    def owner(self) -> str:
        for event in self.events():
            if event.type == "session_start":
                return event.payload.get("owner", "")
        return ""

    @classmethod
    def list_sessions(cls, root: Path | None = None) -> list[str]:
        """Session ids, most recently active first.

        Sorting by id would be sorting by uuid, which is sorting by nothing —
        and the session you want is almost always the one you just ran.
        """
        base = root or duet_home() / "sessions"
        if not base.is_dir():
            return []
        logs = [p / "log.jsonl" for p in base.iterdir() if (p / "log.jsonl").is_file()]
        return [p.parent.name for p in sorted(logs, key=lambda f: f.stat().st_mtime, reverse=True)]

    # -- event log ---------------------------------------------------------

    def append(self, type: EventType, payload: dict[str, Any]) -> Event:
        self._seq += 1
        event = Event(seq=self._seq, type=type, payload=payload)
        with self.log_path.open("a") as f:
            f.write(event.model_dump_json() + "\n")
        if type == "cost_tick":
            self._cost_usd += float(payload.get("cost_usd", 0.0))
        for listener in self._listeners:
            listener(event)
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
