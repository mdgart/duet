"""The objection ledger — maintained deterministically from verdict JSON.

SPECS §4.4 is explicit that this is never produced by model summarization: it
is the one record the orchestrator can trust when older rounds fall out of
verbatim context.
"""

from __future__ import annotations

from duet.schemas.debate import (
    SEVERITY_RANK,
    LedgerEntry,
    Verdict,
    normalize_claim,
)


class ObjectionLedger:
    def __init__(self) -> None:
        self.entries: list[LedgerEntry] = []
        self._by_key: dict[str, LedgerEntry] = {}
        self._by_id: dict[str, LedgerEntry] = {}

    # -- queries -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, objection_id: str) -> LedgerEntry | None:
        return self._by_id.get(objection_id)

    def open_entries(self) -> list[LedgerEntry]:
        return [e for e in self.entries if e.status == "open"]

    def open_blocking(self) -> list[LedgerEntry]:
        return [e for e in self.open_entries() if e.blocking_or_major]

    def corroborated(self) -> list[LedgerEntry]:
        return [e for e in self.entries if e.corroborated]

    def open_for(self, participant_id: str) -> list[LedgerEntry]:
        """Entries this participant raised and has not closed.

        Only the raiser may close an objection, so this is the exact set that
        stands between them and a valid AGREE. Computed here rather than left
        for the model to infer: a debate stalled with every open objection
        blocked on one participant that had declared 39 closures, almost all of
        them for objections the *other* participant had raised.
        """
        return [
            e
            for e in self.entries
            if participant_id in e.raised_by and participant_id not in e.closed_by
        ]

    def thrashing(self, reopen_limit: int) -> list[LedgerEntry]:
        """Entries reopened enough times to count as deadlock (SPECS §4.3)."""
        return [e for e in self.entries if e.reopen_count >= reopen_limit]

    def summary_lines(self) -> list[str]:
        """The compressed form injected once verbatim history is evicted.

        Deliberately free of evaluative labels. This text goes into the prompts,
        and marking an entry "corroborated" there tells a model that a point is
        strong when all that happened is two readers thought of it — the exact
        amplification that let a wrong physical constant travel unchecked
        between participants. The raised-by list states the same fact without
        the endorsement: who raised it is evidence, "corroborated" is a verdict.
        """
        lines = []
        for e in self.entries:
            who = "+".join(e.raised_by)
            tail = f" — {e.resolution}" if e.resolution else ""
            lines.append(
                f"{e.id} [{e.severity}/{e.status}] (raised by {who}) {e.section}: {e.claim}{tail}"
            )
        return lines

    # -- mutation ----------------------------------------------------------

    def close_for(self, participant_id: str, objection_ids: list[str]) -> list[str]:
        """A proposer marking its own prior objections as addressed by a revision.

        Only closes entries this participant actually raised: a draft claiming
        to have fixed another participant's objection settles nothing, since
        that participant can still re-raise it on the next critique.
        """
        closed = []
        for objection_id in objection_ids:
            entry = self._by_id.get(objection_id)
            if entry is not None and participant_id in entry.raised_by:
                entry.closed_by[participant_id] = "resolved"
                closed.append(objection_id)
        return closed

    def merge_round(self, round_number: int, verdicts: dict[str, Verdict]) -> None:
        """Fold one round's critiques in, in a fixed order so that replaying
        the same events always rebuilds the same ledger."""
        for participant_id in sorted(verdicts):
            self._apply_closures(participant_id, verdicts[participant_id])
        for participant_id in sorted(verdicts):
            self._apply_objections(round_number, participant_id, verdicts[participant_id])

    def _apply_closures(self, participant_id: str, verdict: Verdict) -> None:
        # Closures are applied before new objections, so that re-raising a
        # claim in the same round reopens it rather than being swallowed.
        for objection_id in verdict.resolved_objections:
            entry = self._by_id.get(objection_id)
            if entry is not None and participant_id in entry.raised_by:
                entry.closed_by[participant_id] = "resolved"
        for waived in verdict.waived_objections:
            entry = self._by_id.get(waived.id)
            if entry is not None and participant_id in entry.raised_by:
                entry.closed_by[participant_id] = "waived"
                entry.resolution = waived.reason

    def _apply_objections(self, round_number: int, participant_id: str, verdict: Verdict) -> None:
        for objection in verdict.objections:
            key = normalize_claim(objection.section, objection.claim)
            # A critic that says which entry it is restating is believed about
            # *identity* only — the orchestrator still checks the id exists and
            # does the counting itself. Wording-based matching cannot do this:
            # the same concern comes back in genuinely different words.
            entry = self._by_id.get(objection.reopens) if objection.reopens else None
            if entry is None:
                entry = self._by_key.get(key)
            if entry is None:
                entry = LedgerEntry(
                    id=f"OBJ-{len(self.entries) + 1}",
                    severity=objection.severity,
                    section=objection.section,
                    claim=objection.claim,
                    evidence=objection.evidence,
                    proposed_change=objection.proposed_change,
                    raised_by=[participant_id],
                    first_round=round_number,
                )
                self.entries.append(entry)
                self._by_key[key] = entry
                self._by_id[entry.id] = entry
                continue

            was_closed = entry.status != "open"
            if participant_id not in entry.raised_by:
                entry.raised_by.append(participant_id)
            # Re-raising reopens: drop this participant's prior closure, and
            # count the reopen once per transition rather than per raiser.
            entry.closed_by.pop(participant_id, None)
            if was_closed and entry.status == "open":
                entry.reopen_count += 1
            if SEVERITY_RANK[objection.severity] > SEVERITY_RANK[entry.severity]:
                entry.severity = objection.severity


def unresolved_for(ledger: ObjectionLedger, participant_id: str) -> list[LedgerEntry]:
    """Blocking/major objections this participant raised and has not closed.

    SPECS §4.3: an AGREE is only valid once this list is empty.
    """
    return [
        e
        for e in ledger.entries
        if e.blocking_or_major
        and participant_id in e.raised_by
        and participant_id not in e.closed_by
    ]
