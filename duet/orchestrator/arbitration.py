"""Deadlock arbitration — the disagreement report (SPECS §6.3)."""

from __future__ import annotations

from duet.orchestrator.ledger import ObjectionLedger
from duet.schemas.config import Roster


def convergence_trend(open_history: list[int], churn: list[tuple] | None = None) -> list[str]:
    """Was the debate getting anywhere? The question arbitration turns on.

    A falling trend means the round cap was simply too low for a hard idea and
    more rounds would likely finish it. A flat or rising one means the models
    are generating objections as fast as they close them, and more rounds buy
    nothing — the idea or the brief needs to change.
    """
    if not open_history:
        return []
    trend = " → ".join(str(n) for n in open_history)
    best, latest = min(open_history), open_history[-1]
    if latest <= best:
        verdict = "still improving — more rounds are likely to close it"
    elif latest > open_history[0]:
        verdict = "getting worse — each revision is opening more than it settles"
    else:
        verdict = "stalled — closing objections at the rate new ones arrive"
    lines = [
        "## Was it converging?",
        "",
        f"Open blocking/major objections by round: {trend}",
        f"Best was {best}, latest is {latest} — {verdict}.",
        "",
    ]
    if churn:
        raised = sum(r for r, _ in churn)
        closed = sum(c for _, c in churn)
        per_round = "  ".join(f"r{i}: +{r}/-{c}" for i, (r, c) in enumerate(churn, 1))
        lines += [
            f"Raised vs closed, per round: {per_round}",
            f"Across the debate: {raised} raised, {closed} closed.",
            "",
        ]
        # The net count hides this: a round can close three and open three.
        if raised > closed * 1.5 and raised > 4:
            lines += [
                "The debate is opening objections faster than it retires them. That is "
                "usually one subsystem generating coupled complaints, not many "
                "independent problems — look for the smallest deletion that closes "
                "several at once, rather than ruling on each in turn.",
                "",
            ]
        elif closed >= raised:
            lines += [
                "Objections are being retired at least as fast as they arrive, so the "
                "round cap — not the design — is what stopped this. More rounds are "
                "likely to finish it.",
                "",
            ]
    return lines


def disagreement_report(
    ledger: ObjectionLedger,
    roster: Roster,
    detail: str,
    open_history: list[int] | None = None,
    churn: list[tuple] | None = None,
) -> str:
    """One section per unresolved objection, one column per participant.

    Widens with the roster rather than changing shape, so the same renderer
    serves a pair and a panel.
    """
    lines = ["# Disagreement report", "", f"Debate ended: {detail}", ""]
    open_entries_early = ledger.open_entries()
    if open_entries_early:
        holders = sorted({p for e in open_entries_early for p in set(e.raised_by) - set(e.closed_by)})
        sections = sorted({e.section for e in open_entries_early})
        blocking = sum(1 for e in open_entries_early if e.severity == "blocking")
        lines += [
            "## In short",
            "",
            f"{len(open_entries_early)} objection(s) still open"
            + (f", {blocking} of them blocking" if blocking else "")
            + f" — held by {', '.join(holders)}.",
            "",
            "They sit in: " + "; ".join(s[:70] for s in sections[:4])
            + (" …" if len(sections) > 4 else "") + ".",
            "",
            "Objections clustered in one area are usually one decision wearing several "
            "hats. Read them together before ruling on each: a single ruling that "
            "removes or settles the shared cause often closes all of them.",
            "",
        ]
    lines += convergence_trend(open_history or [], churn)
    open_entries = ledger.open_entries()
    if not open_entries:
        lines += ["No objections remain open; the debate ran out of rounds rather", "than out of agreement.", ""]
    for entry in open_entries:
        # Stated as what it is, for a human who must check rather than tally.
        raisers = (
            f" · raised independently by {', '.join(entry.raised_by)}"
            if entry.corroborated
            else ""
        )
        lines += [
            f"## {entry.id} — {entry.severity}{raisers}",
            "",
            f"*{entry.one_line}*" if entry.headline else "",
            f"**Section:** {entry.section}",
            f"**Claim:** {entry.claim}",
            f"**Evidence:** {entry.evidence}",
            f"**Proposed change:** {entry.proposed_change}",
            "",
            "| participant | position |",
            "|---|---|",
        ]
        for participant in roster:
            if participant.id in entry.closed_by:
                position = entry.closed_by[participant.id]
            elif participant.id in entry.raised_by:
                position = "upholds"
            else:
                position = "did not raise"
            lines.append(f"| {participant.id} | {position} |")
        upholding = [p for p in entry.raised_by if p not in entry.closed_by]
        lines += ["", f"Tally: {len(upholding)} uphold / {len(entry.closed_by)} closed", ""]
    if any(e.corroborated for e in open_entries):
        lines += [
            "> An objection raised independently by several participants is more "
            "*salient*, not more likely to be true: they concurred, they did not "
            "check each other. Verify the claim on its merits — a shared mistake "
            "arrives looking exactly like a shared insight.",
            "",
        ]
    lines += [
        "## Your options",
        "",
        "- **resolve** — an editor opens with each objection above and space for your",
        "  decision. Anything you write becomes a `FIXED:` item in the brief, which the",
        "  models must treat as settled and may not object to; objections you leave blank",
        "  come back. The debate then resumes with the round cap raised by 2.",
        "- **abort** — end the session. The log and every draft are retained, and the",
        "  brief can be re-run as a new session.",
        "",
    ]
    return "\n".join(lines)
