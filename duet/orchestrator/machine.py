"""The session state machine (SPECS §2), event-sourced and gate-suspending.

Gates are suspension points, not prompts: reaching one emits `gate_pending`
and returns. Nothing here reads stdin — the CLI, and later a browser or an MCP
client, are all just things that append a `gate_decision` event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from duet.orchestrator.arbitration import disagreement_report
from duet.orchestrator.debate import DebateEngine
from duet.providers.base import Provider, build_provider
from duet.schemas.config import DuetConfig, Participant
from duet.store.session import SessionStore

State = Literal[
    "INIT", "DESIGN_DEBATE", "GATE_1", "ARBITRATION",
    "TASK_PLANNING", "BUILD_LOOP", "GATE_2", "DONE", "BUDGET_STOP", "FAILED",
]

GATE_1_OPTIONS = ["approve", "edit", "revise", "reject"]
ARBITRATION_OPTIONS = ["resolve", "abort"]


@dataclass
class GateRequest:
    gate: str
    options: list[str]
    artifacts: list[str]
    draft_hash: str
    objections: list[dict] = field(default_factory=list)


@dataclass
class RunResult:
    state: State
    suspended: bool = False
    gate: GateRequest | None = None
    detail: str = ""


def providers_for(config: DuetConfig) -> dict[str, Provider]:
    return {p.id: build_provider(p, config) for p in config.participants}


class Machine:
    def __init__(self, config: DuetConfig, store: SessionStore, providers: dict[str, Provider]):
        self.config = config
        self.store = store
        self.providers = providers

    # -- event-sourced state ----------------------------------------------

    def current_state(self) -> State:
        state: State = "INIT"
        for event in self.store.events():
            if event.type == "state_change":
                state = event.payload["to"]
        return state

    def session_roster(self) -> list[Participant]:
        """The participants this session was started with, if recorded."""
        for event in self.store.events():
            if event.type == "state_change" and event.payload.get("roster"):
                return [Participant.model_validate(p) for p in event.payload["roster"]]
        return []

    def context(self) -> str:
        """Reference material for the session, fixed at creation.

        Unlike the brief — which a Gate-1 revision rewrites — context is
        immutable: it describes the situation, and the situation does not
        change because the models disagreed about it. The first value wins.
        """
        for event in self.store.events():
            if event.type == "state_change" and event.payload.get("context"):
                return event.payload["context"]
        return ""

    def brief(self) -> str:
        # The latest wins: a Gate-1 revision rewrites the brief with the human's
        # feedback appended as a FIXED: item, and that is what the debate reads.
        brief = ""
        for event in self.store.events():
            if event.type == "state_change" and "brief" in event.payload:
                brief = event.payload["brief"]
        return brief

    def round_cap_bonus(self) -> int:
        """Arbitration and Gate-1 revisions each buy the debate more rounds."""
        bonus = 0
        for event in self.store.events():
            if event.type == "gate_decision" and event.payload["action"] in ("resolve", "revise"):
                bonus += 2 if event.payload["action"] == "resolve" else 1
        return bonus

    def pending_gate(self) -> tuple[dict | None, dict | None]:
        """The last gate_pending and the decision answering it, if any."""
        pending = decision = None
        for event in self.store.events():
            if event.type == "gate_pending":
                pending, decision = event.payload, None
            elif event.type == "gate_decision" and pending is not None:
                decision = event.payload
        return pending, decision

    def _transition(self, to: State, **payload) -> None:
        self.store.append("state_change", {"from": self.current_state(), "to": to, **payload})

    # -- driving -----------------------------------------------------------

    def start(self, brief: str, context: str = "") -> RunResult:
        DebateEngine.check_context(self.config, context)
        # The roster is pinned to the session, not re-read from config on
        # resume. Only the participant who raised an objection may close it, so
        # swapping the roster mid-debate orphans every open objection: the new
        # participants cannot resolve them and the debate can only deadlock.
        payload = {"brief": brief, "roster": [p.model_dump() for p in self.config.participants]}
        if context.strip():
            # Stored inline for faithful replay and as a file for reading.
            payload["context"] = context
            payload["context_ref"] = self.store.save_artifact("CONTEXT.md", context)
        self._transition("DESIGN_DEBATE", **payload)
        return self.advance()

    def review(self, markdown: str, author: str) -> RunResult:
        """Put a human-edited spec back in front of the models (SPECS §13.1).

        Valid from GATE_1 or DONE: an approved spec is not frozen, it is just
        one nobody is currently arguing about. The review is a normal debate
        round with the human as proposer, so its verdicts, objections and cost
        land in the same log as every other round.
        """
        state = self.current_state()
        if state not in ("GATE_1", "DONE"):
            return RunResult(state, detail=f"cannot review from {state}")

        engine = DebateEngine.replay(
            self.config, self.store, self.providers, context=self.context()
        )
        result = engine.review(self.brief(), engine.state, markdown, author)

        if result.outcome == "BUDGET_STOP":
            self._transition("BUDGET_STOP")
            return RunResult("BUDGET_STOP", detail=result.detail)

        log_ref = self.store.save_artifact("IMPLEMENTATION.md", markdown)
        self._transition("GATE_1", reviewed_by=author)
        return self._open_gate(
            "gate_1", GATE_1_OPTIONS, [log_ref], result.state.draft_hash, result.detail
        )

    def advance(self) -> RunResult:
        """Run until the machine terminates or needs a human decision."""
        while True:
            state = self.current_state()
            if state == "BUDGET_STOP":
                # SPECS §7.2: a budget stop suspends, it does not end. Raise the
                # cap and resume and the debate continues from the log — which is
                # what makes a low cap safe to set rather than destructive.
                if self.store.cost_usd >= self.config.budget.max_usd:
                    return RunResult(
                        state,
                        detail=(
                            f"spent ${self.store.cost_usd:.2f} of the "
                            f"${self.config.budget.max_usd:.2f} cap — raise "
                            f"budget.max_usd and resume to continue"
                        ),
                    )
                self._transition("DESIGN_DEBATE")
                continue
            if state in ("DONE", "FAILED"):
                return RunResult(state, detail=f"session is {state}")
            if state in ("GATE_1", "ARBITRATION"):
                result = self._handle_gate(state)
                if result is not None:
                    return result
                continue
            if state == "DESIGN_DEBATE":
                result = self._run_debate()
                if result is not None:
                    return result
                continue
            return RunResult(state, detail=f"{state} is not implemented until M2")

    def _run_debate(self) -> RunResult | None:
        engine = DebateEngine.replay(
            self.config, self.store, self.providers, context=self.context()
        )
        cap = self.config.debate.max_rounds + self.round_cap_bonus()
        engine.config = self.config.model_copy(deep=True)
        engine.config.debate.max_rounds = cap
        result = engine.run(self.brief(), engine.state)

        if result.outcome == "CONSENSUS":
            log_ref = self.store.save_artifact("IMPLEMENTATION.md", result.state.draft_markdown)
            self._transition("GATE_1")
            return self._open_gate(
                "gate_1", GATE_1_OPTIONS, [log_ref], result.state.draft_hash, result.detail
            )
        if result.outcome == "BUDGET_STOP":
            self._transition("BUDGET_STOP")
            return RunResult("BUDGET_STOP", detail=result.detail)

        report = disagreement_report(
            result.state.ledger,
            self.config.roster,
            result.detail,
            result.state.open_history,
            result.state.churn
        )
        log_ref = self.store.save_artifact(f"disagreement-r{result.state.round}.md", report)
        self._transition("ARBITRATION")
        return self._open_gate(
            "arbitration",
            ARBITRATION_OPTIONS,
            [log_ref],
            result.state.draft_hash,
            result.detail,
            # What is actually unresolved, so a client can ask for a ruling on
            # each rather than for one undifferentiated blob of feedback.
            objections=[
                {
                    "id": e.id,
                    "severity": e.severity,
                    "section": e.section,
                    "claim": e.claim,
                    "held_by": sorted(set(e.raised_by) - set(e.closed_by)),
                }
                for e in result.state.ledger.open_entries()
            ],
        )

    def _open_gate(
        self,
        gate: str,
        options: list[str],
        artifacts: list[str],
        draft_hash: str,
        detail: str,
        objections: list[dict] | None = None,
    ) -> RunResult:
        payload = {"gate": gate, "options": options, "artifacts": artifacts, "draft_hash": draft_hash}
        if objections:
            payload["objections"] = objections
        self.store.append("gate_pending", payload)
        return RunResult(
            self.current_state(), suspended=True, gate=GateRequest(**payload), detail=detail
        )

    def _handle_gate(self, state: State) -> RunResult | None:
        pending, decision = self.pending_gate()
        if pending is None:
            self._transition("FAILED")
            return RunResult("FAILED", detail=f"{state} with no pending gate in the log")
        if decision is None:
            return RunResult(state, suspended=True, gate=GateRequest(**pending),
                             detail="awaiting a gate decision")

        action = decision["action"]
        if action in ("approve", "edit"):
            if action == "edit" and decision.get("edited_markdown"):
                self.store.save_artifact("IMPLEMENTATION.md", decision["edited_markdown"])
            self._transition("DONE", approved_by=decision["actor"])
            return RunResult("DONE", detail=f"spec approved by {decision['actor']}")
        if action in ("reject", "abort"):
            self._transition("FAILED", rejected_by=decision["actor"])
            return RunResult("FAILED", detail=f"rejected by {decision['actor']}")
        if action in ("revise", "resolve"):
            # Human feedback re-enters the debate as a FIXED: item.
            self._transition("DESIGN_DEBATE", brief=self._brief_with(decision.get("feedback", "")))
            return None
        self._transition("FAILED")
        return RunResult("FAILED", detail=f"unknown gate action {action!r}")

    def _brief_with(self, feedback: str) -> str:
        """Fold a human decision back into the brief as FIXED: material.

        Arbitration rulings arrive already marked — one FIXED: line per
        objection — while free-form revision feedback is a single unmarked
        note. Prefixing only when the marker is absent keeps both readable and
        avoids "FIXED: FIXED: ...".
        """
        brief = self.brief()
        text = feedback.strip()
        if not text:
            return brief
        if "FIXED:" not in text:
            text = f"FIXED: {text}"
        return f"{brief}\n\n{text}"


def submit_gate_decision(
    store: SessionStore, *, action: str, actor: str, feedback: str = "", edited_markdown: str = ""
) -> None:
    """Answer the open gate. Any client may call this — that is the point."""
    pending = None
    for event in store.events():
        if event.type == "gate_pending":
            pending = event.payload
        elif event.type == "gate_decision":
            pending = None
    if pending is None:
        raise ValueError("no gate is currently open")
    if action not in pending["options"]:
        raise ValueError(f"action {action!r} not offered; options are {pending['options']}")
    store.append(
        "gate_decision",
        {
            "gate": pending["gate"],
            "action": action,
            "actor": actor,
            "feedback": feedback,
            "edited_markdown": edited_markdown,
            # Pins the decision to the exact text it was made against (SPECS §6.0).
            "draft_hash": pending["draft_hash"],
        },
    )


def pin_roster(config: DuetConfig, store: SessionStore) -> tuple[DuetConfig, str]:
    """Return config bound to the session's original roster.

    Resuming is continuing one debate, not starting a similar one. A different
    roster cannot close the objections already on the ledger, so the session's
    own participants win over whatever config now says — and the caller is told
    when they differ, because silently ignoring a config change is its own trap.
    """
    machine = Machine(config, store, {})
    pinned = machine.session_roster()
    if not pinned:
        return config, ""
    if [p.model_dump() for p in pinned] == [p.model_dump() for p in config.participants]:
        return config, ""
    bound = config.model_copy(deep=True)
    bound.participants = pinned
    return bound, (
        f"session roster is {', '.join(p.id for p in pinned)}; config says "
        f"{', '.join(p.id for p in config.participants)}. Using the session's — "
        f"objections already raised can only be closed by whoever raised them."
    )
