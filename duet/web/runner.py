"""Run debates in the background and fan their events out to browsers.

The CLI drives a debate inline and blocks; a browser cannot. So a session runs
on its own thread — provider calls are blocking sync HTTP, which must stay off
the event loop — and every event it appends is published to whatever SSE clients
are watching. A gate suspends the thread exactly as it suspends the CLI; a POST
of a decision resumes it.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator

from duet.orchestrator.machine import Machine, RunResult, pin_roster, providers_for, submit_gate_decision
from duet.schemas.config import DuetConfig, load_config, duet_home
from duet.schemas.events import Event
from duet.store.session import SessionStore

_SENTINEL = object()  # closes an SSE stream


class Broker:
    """Fan-out of a session's events to any number of SSE subscribers.

    Session-keyed, in-memory, single-process — enough for a local tool. The
    hosted path replaces this with the durable log tail in PLAN §6.3.6.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[queue.Queue]] = {}
        self._lock = threading.Lock()

    def publish(self, session_id: str, item: Any) -> None:
        with self._lock:
            for q in self._subscribers.get(session_id, []):
                q.put(item)

    def subscribe(self, session_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.setdefault(session_id, []).append(q)
        return q

    def unsubscribe(self, session_id: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(session_id, [])
            if q in subs:
                subs.remove(q)

    def close(self, session_id: str) -> None:
        """Tell live streams the run is over so they can end cleanly."""
        self.publish(session_id, _SENTINEL)


@dataclass
class Runner:
    """Owns the background threads and the broker for the process."""

    config: DuetConfig | None = None      # base config (tests, or no profile chosen)
    config_path: Any = None               # a duet.toml, honoured by profile loads
    base_profile: str | None = None       # profile when the form leaves it default
    root: Any = None                      # SessionStore root override, for tests
    broker: Broker = field(default_factory=Broker)
    _threads: dict[str, threading.Thread] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- config resolution ------------------------------------------------

    def _load(self, profile: str | None, budget: float | None) -> DuetConfig:
        """A config for one session, from a chosen profile and budget."""
        if profile:
            cfg = load_config(self.config_path, profile)
        elif self.config is not None:
            cfg = self.config.model_copy(deep=True)
        else:
            cfg = load_config(self.config_path, self.base_profile)
        if budget is not None:
            cfg.budget.max_usd = budget
        return cfg

    def config_for(self, store: SessionStore) -> DuetConfig:
        """Rebuild a started session's config from its own log — the profile and
        budget it recorded, then its pinned roster — so a resume is faithful."""
        machine = Machine(self.config or DuetConfig(), store, {})
        cfg = self._load(machine.session_profile() or None, machine.session_budget())
        cfg, _ = pin_roster(cfg, store)
        return cfg

    # -- reads (any session, live or not) ---------------------------------

    def store(self, session_id: str) -> SessionStore:
        return SessionStore(session_id, root=self.root)

    def machine(self, store: SessionStore) -> Machine:
        return Machine(self.config_for(store), store, {})  # no providers: reads only

    # -- driving ----------------------------------------------------------

    def start(
        self,
        idea: str,
        template: str,
        context: str = "",
        profile: str = "",
        budget: float | None = None,
        owner: str = "web",
    ) -> str:
        store = SessionStore.new(root=self.root, owner=owner)
        cfg = self._load(profile or None, budget)
        self._run_in_background(
            store, cfg,
            lambda m: m.start(idea, context=context, template=template,
                              profile=profile, budget_usd=budget),
        )
        return store.session_id

    def submit_gate(self, session_id: str, action: str, actor: str, **kw) -> None:
        store = self.store(session_id)
        submit_gate_decision(store, action=action, actor=actor, **kw)
        # Resume the machine so the next state (another gate, or DONE) is reached.
        self._run_in_background(store, self.config_for(store), lambda m: m.advance())

    def raise_budget(self, session_id: str, new_usd: float, actor: str = "web") -> None:
        """Lift a budget stop from the browser and resume (§7.2)."""
        store = self.store(session_id)
        Machine(self.config_for(store), store, {}).raise_budget(new_usd, actor)
        self._run_in_background(store, self.config_for(store), lambda m: m.advance())

    def _run_in_background(self, store: SessionStore, cfg: DuetConfig, drive) -> None:
        machine = Machine(cfg, store, providers_for(cfg))
        store.subscribe(lambda event: self.broker.publish(store.session_id, event))

        def target() -> None:
            try:
                drive(machine)
            except Exception as e:  # noqa: BLE001 — surface, do not crash the server
                store.append("error", {"scope": "runner", "error": str(e)})
            finally:
                self.broker.close(store.session_id)

        thread = threading.Thread(target=target, daemon=True, name=f"duet-{store.session_id[:8]}")
        with self._lock:
            self._threads[store.session_id] = thread
        thread.start()

    def is_running(self, session_id: str) -> bool:
        with self._lock:
            thread = self._threads.get(session_id)
        return bool(thread and thread.is_alive())

    # -- SSE --------------------------------------------------------------

    def stream(self, session_id: str) -> Iterator[str]:
        """Replay the log, then follow live events until the run ends.

        History first so a browser that connects mid-debate — or to a finished
        session — sees the whole thing, then live events with no gap, because
        the subscriber is registered before the backlog is read.
        """
        q = self.broker.subscribe(session_id)
        try:
            for event in self.store(session_id).events():
                yield _sse(event.model_dump())
            if not self.is_running(session_id):
                yield _sse({"type": "_eof"})
                return
            while True:
                item = q.get()
                if item is _SENTINEL:
                    yield _sse({"type": "_eof"})
                    return
                yield _sse(item.model_dump() if isinstance(item, Event) else item)
        finally:
            self.broker.unsubscribe(session_id, q)


def _sse(payload: dict) -> str:
    import json

    return f"data: {json.dumps(payload)}\n\n"
