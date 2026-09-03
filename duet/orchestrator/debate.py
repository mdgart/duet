"""Phase A — the design debate engine (SPECS §4).

All nondeterminism lives in the model calls. Everything else here — rotation,
ledger merging, consensus counting, deadlock detection — is deterministic and
reconstructible from the event log, which is what makes a session resumable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Literal

from duet.orchestrator.ledger import ObjectionLedger, unresolved_for
from duet.prompts.phase_a import (
    AGREE_ALLOWED,
    AGREE_FORBIDDEN,
    CRITIC_SYSTEM,
    PROMPT_VERSION,
    PROPOSER_SYSTEM,
    critic_messages,
    proposer_messages,
)
from duet.providers.base import OutputTruncated, Provider
from duet.schemas.config import DuetConfig, Participant
from duet.schemas.debate import Verdict
from duet.schemas.templates import Template, draft_model_for, get_template
from duet.schemas.model_io import ModelRequest, Turn
from duet.store.session import BudgetExceeded, SessionStore

Outcome = Literal["CONSENSUS", "DEADLOCK", "BUDGET_STOP"]


class SchemaRepairFailed(Exception):
    """Model output failed validation twice — the hard error of SPECS §3."""


class ContextTooLarge(Exception):
    """Reference material that would crowd out the debate itself.

    Context is re-sent on every turn, so it multiplies across the run. Refused
    up front rather than silently truncated: a half-read constraint is worse
    than none, because the models act confidently on the half they got.
    """


@dataclass
class DebateState:
    round: int = 0
    draft_markdown: str = ""
    draft_hash: str = ""
    ledger: ObjectionLedger = field(default_factory=ObjectionLedger)
    votes: dict[str, tuple[str, str]] = field(default_factory=dict)
    history: dict[int, list[str]] = field(default_factory=dict)
    critiqued: dict[int, set] = field(default_factory=dict)
    """Who has already critiqued each round. An interruption can land between a
    draft and the critiques of it; without this the resumed run would start a
    fresh round and abandon a draft that was paid for and never judged."""
    churn: list[tuple] = field(default_factory=list)
    """(raised, closed) per round. The net open count cannot distinguish a
    round that closed three objections and opened three from one that did
    nothing at all — and that is precisely the question about whether a debate
    is retiring complexity or manufacturing it."""
    open_history: list[int] = field(default_factory=list)
    """Open blocking/major objections after each round — the convergence
    trace. Consensus needs this at zero, so its trend is the only aggregate
    evidence of whether a debate is getting anywhere."""

    def agreements_on(self, draft_hash: str, among: list[str] | None = None) -> list[str]:
        """Participants agreeing on this exact draft.

        `among` restricts the count to the roster: a human-authored draft
        records the author's assent like any other, but consensus is a claim
        about the models, so a human vote must never help reach it.
        """
        agreed = (p for p, (h, v) in self.votes.items() if h == draft_hash and v == "AGREE")
        return sorted(p for p in agreed if among is None or p in among)


@dataclass
class DebateResult:
    outcome: Outcome
    state: DebateState
    detail: str = ""


class DebateEngine:
    # Context may occupy at most this share of the per-turn budget; the rest
    # belongs to the draft, the ledger and the recent critique.
    CONTEXT_BUDGET_SHARE = 0.5

    def __init__(
        self,
        config: DuetConfig,
        store: SessionStore,
        providers: dict[str, Provider],
        context: str = "",
        template: Template | str = "software",
    ):
        self.config = config
        self.store = store
        self.providers = providers
        self.roster = config.roster
        self.context = context
        self.template = template if isinstance(template, Template) else get_template(template)
        self.draft_model = draft_model_for(self.template)
        self.check_context(config, context)

    @classmethod
    def check_context(cls, config: DuetConfig, context: str) -> None:
        budget_chars = int(config.build.context_budget_tokens * 4 * cls.CONTEXT_BUDGET_SHARE)
        if len(context) > budget_chars:
            raise ContextTooLarge(
                f"context is ~{len(context) // 4} tokens; the per-turn budget allows "
                f"~{budget_chars // 4}. It is re-sent every turn, so trim it or raise "
                f"build.context_budget_tokens."
            )

    # -- reconstruction ----------------------------------------------------

    @classmethod
    def replay(
        cls,
        config: DuetConfig,
        store: SessionStore,
        providers: dict[str, Provider],
        context: str = "",
        template: Template | str = "software",
    ):
        """Rebuild debate state from the log. Resume is replay plus continue.

        Rounds are rebuilt in order and, within a round, in protocol order —
        the proposer's closures land before that round's critiques, exactly as
        they did live, so a resumed ledger is identical to the original.
        """
        engine = cls(config, store, providers, context=context, template=template)
        state = DebateState()
        rounds: dict[int, dict] = {}
        for event in store.events():
            payload = event.payload
            if event.type == "draft":
                slot = rounds.setdefault(payload["round"], {"verdicts": {}})
                slot["draft"] = payload
            elif event.type == "verdict":
                slot = rounds.setdefault(payload["round"], {"verdicts": {}})
                slot["verdicts"][payload["participant"]] = Verdict.model_validate(
                    payload["verdict"]
                )

        for round_number in sorted(rounds):
            slot = rounds[round_number]
            before = _snapshot(state.ledger)
            draft = slot.get("draft")
            if draft is not None:
                state.round = draft["round"]
                state.draft_markdown = draft["markdown"]
                state.draft_hash = draft["hash"]
                state.ledger.close_for(draft["participant"], draft.get("addressed", []))
                state.votes[draft["participant"]] = (draft["hash"], "AGREE")
            if slot["verdicts"]:
                state.critiqued[round_number] = set(slot["verdicts"])
                state.ledger.merge_round(round_number, slot["verdicts"])
                state.churn.append(_churn(state.ledger, *before))
                state.history[round_number] = [
                    _render_critique(pid, v) for pid, v in sorted(slot["verdicts"].items())
                ]
                state.open_history.append(len(state.ledger.open_blocking()))
                for pid, verdict in slot["verdicts"].items():
                    state.votes[pid] = (state.draft_hash, verdict.verdict)
        engine.state = state
        return engine

    # -- the loop ----------------------------------------------------------

    def run(self, brief: str, state: DebateState | None = None) -> DebateResult:
        state = state or getattr(self, "state", None) or DebateState()
        cap = self.config.debate.max_rounds
        try:
            while state.round < cap:
                # Snapshot before the proposer turn, not after: _propose closes
                # the author's own objections via `addresses`, and a snapshot
                # taken later counts those as though they never happened —
                # reporting "0 closed" for a round that closed four.
                before = _snapshot(state.ledger)
                proposer = self.roster.proposer_for_round(max(state.round, 1))
                unfinished = self._unfinished_critics(state, proposer)
                if unfinished is None:
                    state.round += 1
                    proposer = self.roster.proposer_for_round(state.round)
                    draft = self._propose(state, brief, proposer)
                    state.draft_markdown = draft.to_markdown()
                    state.draft_hash = draft.content_hash()
                    # Authoring is assent: the proposer votes for their own draft,
                    # so a rotating debate never needs a separate confirmation turn.
                    state.votes[proposer.id] = (state.draft_hash, "AGREE")
                    unfinished = self.roster.others(proposer.id)

                verdicts: dict[str, Verdict] = {}
                for critic in unfinished:
                    verdict = self._critique(state, brief, critic, proposer.badge)
                    verdicts[critic.id] = verdict
                    state.votes[critic.id] = (state.draft_hash, verdict.verdict)
                    state.critiqued.setdefault(state.round, set()).add(critic.id)
                state.ledger.merge_round(state.round, verdicts)
                state.churn.append(_churn(state.ledger, *before))
                state.history[state.round] = [
                    _render_critique(cid, v) for cid, v in sorted(verdicts.items())
                ]
                state.open_history.append(len(state.ledger.open_blocking()))

                stalled = self._stalled(state)
                if stalled:
                    return DebateResult("DEADLOCK", state, stalled)

                thrashing = state.ledger.thrashing(self.config.debate.reopen_limit)
                if thrashing:
                    return DebateResult(
                        "DEADLOCK",
                        state,
                        f"objection reopened {self.config.debate.reopen_limit}×: "
                        + ", ".join(e.id for e in thrashing),
                    )

                needed = self.config.debate.required_agreements(len(self.roster))
                agreed = state.agreements_on(state.draft_hash, self.roster.ids)
                if len(agreed) >= needed:
                    return DebateResult(
                        "CONSENSUS", state, f"{len(agreed)}/{len(self.roster)}: {', '.join(agreed)}"
                    )
        except BudgetExceeded as e:
            return DebateResult("BUDGET_STOP", state, str(e))
        return DebateResult("DEADLOCK", state, f"round cap {cap} reached without consensus")

    def review(self, brief: str, state: DebateState, markdown: str, author: str) -> DebateResult:
        """One critique round over a human-supplied draft (SPECS §13.1).

        The human takes the proposer turn: they wrote the revision, so the
        models only critique it. That reuses the debate protocol rather than
        bolting on a second review mechanism, and it means an edited spec faces
        the same scrutiny a model-authored one does.
        """
        state.round += 1
        state.draft_markdown = markdown
        state.draft_hash = _hash_markdown(markdown)
        state.votes[author] = (state.draft_hash, "AGREE")
        self.store.save_artifact(f"draft-r{state.round}-{state.draft_hash}.md", markdown)
        self.store.append(
            "draft",
            {
                "round": state.round,
                "participant": author,
                "human": True,
                "hash": state.draft_hash,
                "addressed": [],
                "markdown": markdown,
                "log_ref": f"artifacts/draft-r{state.round}-{state.draft_hash}.md",
            },
        )

        try:
            verdicts: dict[str, Verdict] = {}
            for critic in self.roster:
                verdicts[critic.id] = self._critique(state, brief, critic, author)
                state.votes[critic.id] = (state.draft_hash, verdicts[critic.id].verdict)
        except BudgetExceeded as e:
            return DebateResult("BUDGET_STOP", state, str(e))

        state.ledger.merge_round(state.round, verdicts)
        state.history[state.round] = [
            _render_critique(cid, v) for cid, v in sorted(verdicts.items())
        ]
        agreed = state.agreements_on(state.draft_hash, self.roster.ids)
        needed = self.config.debate.required_agreements(len(self.roster))
        if len(agreed) >= needed:
            return DebateResult("CONSENSUS", state, f"{len(agreed)}/{len(self.roster)} agreed")
        objecting = sorted(set(self.roster.ids) - set(agreed))
        return DebateResult("DEADLOCK", state, f"unresolved objections from {', '.join(objecting)}")

    def _stalled(self, state: DebateState) -> str:
        """Aggregate non-convergence, which the reopen limit cannot see.

        The reopen limit catches a debate stuck on one objection. It is blind
        to the other failure: every round closes real objections and raises
        equally many new ones, so nothing repeats and the ledger grows forever.
        Progress is therefore defined as reaching a new low in open
        blocking/major objections; a debate that cannot manage that for
        `stall_rounds` rounds is not converging, whatever it looks like
        round to round.
        """
        window = self.config.debate.stall_rounds
        history = state.open_history
        if len(history) <= window:
            return ""
        best_before = min(history[:-window])
        if min(history[-window:]) < best_before:
            return ""
        best_round = history.index(best_before) + 1
        return (
            f"no progress in {window} rounds: open blocking/major objections have not "
            f"fallen below {best_before} since round {best_round} (trend: "
            + " → ".join(str(n) for n in history)
            + ")"
        )

    def _unfinished_critics(self, state: DebateState, proposer: Participant):
        """Critics still owed a turn on the current draft, or None if the round
        is complete and the debate should move on."""
        if not state.draft_hash or state.round < 1:
            return None
        done = state.critiqued.get(state.round, set())
        pending = [c for c in self.roster.others(proposer.id) if c.id not in done]
        return pending or None

    # -- turns -------------------------------------------------------------

    def _propose(self, state: DebateState, brief: str, proposer: Participant) -> SpecDraft:
        system = PROPOSER_SYSTEM.format(
            badge=proposer.badge, n=len(self.roster), noun=self.template.noun
        )
        user = proposer_messages(
            brief=brief,
            draft_markdown=state.draft_markdown or None,
            ledger_lines=state.ledger.summary_lines(),
            history=self._recent_history(state),
            context=self.context,
            your_open=_own_open_lines(state, proposer.id),
        )
        draft = self._call(proposer, system, user, self.draft_model, "propose", state.round)
        closed = state.ledger.close_for(proposer.id, draft.addresses)
        markdown = draft.to_markdown()
        log_ref = self.store.save_artifact(
            f"draft-r{state.round}-{draft.content_hash()}.md", markdown
        )
        self.store.append(
            "draft",
            {
                "round": state.round,
                "participant": proposer.id,
                "hash": draft.content_hash(),
                "addressed": closed,
                "markdown": markdown,
                "log_ref": log_ref,
            },
        )
        return draft

    def _critique(
        self, state: DebateState, brief: str, critic: Participant, proposer_badge: str
    ) -> Verdict:
        floor = state.round <= self.config.debate.min_objection_rounds
        system = CRITIC_SYSTEM.format(
            badge=critic.badge,
            proposer=proposer_badge,
            n=len(self.roster),
            noun=self.template.noun,
            agree_rule=AGREE_FORBIDDEN if floor else AGREE_ALLOWED,
        )
        user = critic_messages(
            brief=brief,
            draft_markdown=state.draft_markdown,
            ledger_lines=state.ledger.summary_lines(),
            history=self._recent_history(state),
            context=self.context,
            your_open=_own_open_lines(state, critic.id),
        )
        verdict = self._call(critic, system, user, Verdict, "critique", state.round)
        verdict, coerced = self._enforce(state, critic, verdict, floor)
        self.store.append(
            "verdict",
            {
                "round": state.round,
                "participant": critic.id,
                "draft_hash": state.draft_hash,
                "verdict": verdict.model_dump(),
                "coerced": coerced,
            },
        )
        return verdict

    def _enforce(
        self, state: DebateState, critic: Participant, verdict: Verdict, floor: bool
    ) -> tuple[Verdict, str]:
        """Orchestrator-side verdict rules (SPECS §4.3), applied to the parsed
        object rather than trusted to the prompt."""
        if verdict.verdict != "AGREE":
            return verdict, ""
        if floor and not verdict.objections:
            # The anti-sycophancy floor: an empty AGREE this early is politeness.
            return verdict.model_copy(update={"verdict": "REVISE"}), "agree_before_floor"
        if floor:
            return verdict.model_copy(update={"verdict": "OBJECT"}), "agree_before_floor"
        # SPECS §4.3 asks whether the closures appear *in this verdict*, so the
        # ids it declares count even though the ledger has not merged them yet.
        declared = set(verdict.resolved_objections) | {w.id for w in verdict.waived_objections}
        outstanding = [e for e in unresolved_for(state.ledger, critic.id) if e.id not in declared]
        if outstanding:
            return (
                verdict.model_copy(update={"verdict": "REVISE"}),
                "agree_with_open_" + ",".join(e.id for e in outstanding),
            )
        return verdict, ""

    # -- context -----------------------------------------------------------

    def _recent_history(self, state: DebateState) -> list[str]:
        """Last two rounds verbatim, degrading to one round and then to nothing
        as the roster grows and the window stops fitting (SPECS §4.4). The
        ledger is never dropped — it is the deterministic record."""
        budget_chars = self.config.build.context_budget_tokens * 4
        for window in (2, 1, 0):
            rounds = sorted(state.history)[-window:] if window else []
            lines = [line for r in rounds for line in state.history[r]]
            if sum(len(l) for l in lines) <= budget_chars:
                return lines
        return []

    # -- model plumbing ----------------------------------------------------

    def _call(self, participant: Participant, system: str, user: str, schema, purpose: str, round_number: int):
        provider = self.providers[participant.id]
        # Checked before spending, not after. add_cost raises once the cap is
        # crossed, which lands between the model response and the draft/verdict
        # event that records it — aborting there pays for a turn and then throws
        # it away, and the resumed run buys it again.
        if self.store.cost_usd >= self.config.budget.max_usd:
            raise BudgetExceeded(self.store.cost_usd, self.config.budget.max_usd)
        messages = [Turn(role="user", content=user)]
        for attempt in (1, 2):
            self.store.append(
                "model_call",
                {
                    "participant": participant.id,
                    "provider": participant.provider,
                    "model_id": participant.model,
                    "purpose": purpose,
                    "round": round_number,
                    "round_cap": self.config.debate.max_rounds,
                    "prompt_version": PROMPT_VERSION,
                    "system": system,
                    "messages": [m.model_dump() for m in messages],
                    "repair": attempt > 1,
                },
            )
            try:
                response = provider.complete(
                    ModelRequest(
                        system=system,
                        messages=messages,
                        response_schema=schema,
                        max_output_tokens=self.config.debate.max_output_tokens,
                    )
                )
            except Exception as e:  # noqa: BLE001 — one repair round, then hard error
                if attempt == 2:
                    self.store.append(
                        "error",
                        {"participant": participant.id, "purpose": purpose, "error": str(e)},
                    )
                    hint = (
                        " — raise debate.max_output_tokens"
                        if isinstance(e, OutputTruncated)
                        else ""
                    )
                    raise SchemaRepairFailed(
                        f"{participant.id} failed twice on {purpose}: {e}{hint}"
                    ) from e
                # A truncated turn needs a shorter answer, not a corrected one:
                # telling it to "re-emit" the same spec just truncates again.
                if isinstance(e, OutputTruncated):
                    instruction = (
                        f"Your previous answer was cut off at the output limit ({e}). "
                        "Re-emit the whole thing, materially shorter: keep every section "
                        "and every decision, cut elaboration and repetition."
                    )
                else:
                    instruction = f"Your output failed validation: {e}. Re-emit, valid this time."
                messages = messages + [Turn(role="user", content=instruction)]
                continue

            self.store.append(
                "model_response",
                {
                    "participant": participant.id,
                    "purpose": purpose,
                    "round": round_number,
                    "text": response.text,
                    "usage": response.usage.model_dump(),
                },
            )
            try:
                self.store.add_cost(
                    response.model_id, response.usage.model_dump(), self.config.budget.max_usd
                )
            except BudgetExceeded:
                # Recorded; let this turn finish and be written down. The next
                # turn's pre-check stops the run on a clean boundary.
                pass
            if response.parsed is None:
                raise SchemaRepairFailed(f"{participant.id} returned no parsed {schema.__name__}")
            return response.parsed
        raise AssertionError("unreachable")


def _own_open_lines(state: DebateState, participant_id: str) -> list[str]:
    return [
        f"- {e.id} [{e.severity}] {e.section}: {e.claim}"
        for e in state.ledger.open_for(participant_id)
    ]


def _snapshot(ledger) -> tuple:
    """Ledger size and open set, taken at the very start of a round."""
    return (len(ledger.entries), {e.id for e in ledger.open_entries()})


def _churn(ledger, before_total: int, before_open: set) -> tuple:
    """How many objections this round created, and how many it retired.

    Both halves of a round count: the proposer closing its own via `addresses`
    and the critics closing theirs in a verdict.
    """
    still_open = {e.id for e in ledger.open_entries()}
    return (len(ledger.entries) - before_total, len(before_open - still_open))


def _hash_markdown(markdown: str) -> str:
    """Pin a human-supplied draft the same way SpecDraft.content_hash pins one."""
    return hashlib.sha256(markdown.encode()).hexdigest()[:12]


def _render_critique(participant_id: str, verdict: Verdict) -> str:
    lines = [f"[{participant_id}] {verdict.verdict} (agreement {verdict.agreement_score:.2f})"]
    for o in verdict.objections:
        lines.append(f"  - ({o.severity}) {o.section}: {o.claim} — evidence: {o.evidence}")
    for w in verdict.waived_objections:
        lines.append(f"  - waived {w.id}: {w.reason}")
    if verdict.resolved_objections:
        lines.append(f"  - resolved: {', '.join(verdict.resolved_objections)}")
    return "\n".join(lines)
