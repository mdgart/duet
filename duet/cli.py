"""Duet CLI — session driver and the first gate client (SPECS §8)."""

from __future__ import annotations

import getpass
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Column, Table

from duet import __version__
from duet.orchestrator.debate import ContextTooLarge
from duet.orchestrator.machine import Machine, pin_roster, providers_for, submit_gate_decision
from duet.providers.base import build_provider
from duet.schemas.config import DuetConfig, Participant, load_config
from duet.schemas.events import Event
from duet.schemas.model_io import ModelRequest, Turn
from duet.store.session import SessionStore

app = typer.Typer(help=f"Duet v{__version__} — cross-model agent loop")
console = Console()


def badge(p: Participant) -> str:
    return f"[bold {p.color}]\\[{p.badge}][/]"


@app.command()
def ping(
    profile: Optional[str] = typer.Option(None, help="Config profile name"),
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to duet.toml"),
):
    """M0 exit criterion: every participant answers through one interface; cost logged."""
    cfg = load_config(config_path, profile)
    store = SessionStore.new(owner=getpass.getuser())
    console.print(f"session [bold]{store.session_id}[/] · {len(cfg.participants)} participants")

    req = ModelRequest(
        system="You are a connectivity check. Reply with exactly one word.",
        messages=[Turn(role="user", content="Reply with the single word: pong")],
        max_output_tokens=16,
    )
    failures: list[str] = []
    for p in cfg.participants:
        stage = "connect"  # distinguishes a bad adapter/key from a bad call
        try:
            provider = build_provider(p, cfg)
            stage = "call"
            store.append(
                "model_call",
                {"participant": p.id, "provider": p.provider, "model_id": p.model, "purpose": "ping"},
            )
            resp = provider.complete(req)
        except Exception as e:  # noqa: BLE001
            store.append(
                "error",
                {"participant": p.id, "provider": p.provider, "stage": stage, "error": str(e)},
            )
            console.print(f"{badge(p)} [red]FAILED[/] ({stage}): {e}")
            failures.append(p.id)
            continue
        store.append(
            "model_response",
            {"participant": p.id, "text": resp.text, "usage": resp.usage.model_dump()},
        )
        store.add_cost(resp.model_id, resp.usage.model_dump(), cfg.budget.max_usd)
        console.print(
            f"{badge(p)} {resp.text.strip()!r}  "
            f"[dim]{resp.usage.input_tokens}in/{resp.usage.output_tokens}out · ${resp.usage.cost_usd:.4f}[/]"
        )

    console.print(f"total ${store.cost_usd:.4f} · log: {store.log_path}")
    if failures:
        console.print(f"[red]{len(failures)} of {len(cfg.participants)} failed[/]: {', '.join(failures)}")
        raise typer.Exit(code=1)
    console.print("[green]ok[/]")


class _Ticking:
    """Status text with a live elapsed-seconds counter."""

    def __init__(self, text: str, started: float):
        self.text = text
        self.started = started

    def __rich__(self) -> str:
        return f"{self.text} [dim]· {time.monotonic() - self.started:.0f}s[/]"


DEFAULT_CONTEXT = Path("CONTEXT.md")


def _read_brief(idea: str) -> str:
    """The idea itself, or `@path` to read it from a file.

    `~` is expanded here rather than left to the shell: it survives quoting,
    which the shell's own expansion does not, and "@~/notes/brief.md" is what
    people type.
    """
    if not idea.startswith("@"):
        return idea
    path = Path(idea[1:]).expanduser()
    if not path.is_file():
        console.print(f"[red]brief file not found: {path}[/]")
        raise typer.Exit(code=1)
    return path.read_text()


def _load_context(paths: list[Path]) -> str:
    """Reference material for every model at every stage.

    Reported either way — silently including a file, or silently missing one,
    both produce a debate the human cannot account for. Files are read as
    literal text: no URL in them is ever fetched, which would put network in
    the orchestrator and make every link an injection vector.
    """
    chosen = [p.expanduser() for p in paths] or (
        [DEFAULT_CONTEXT] if DEFAULT_CONTEXT.is_file() else []
    )
    if not chosen:
        console.print("[dim]no context file (looked for ./CONTEXT.md)[/]")
        return ""
    blocks = []
    for path in chosen:
        if not path.is_file():
            console.print(f"[red]context file not found: {path}[/]")
            raise typer.Exit(code=1)
        text = path.read_text()
        console.print(f"[dim]context: {path} ({len(text) // 4} tokens approx)[/]")
        blocks.append(f"## {path.name}\n\n{text.strip()}")
    return "\n\n".join(blocks)


def _budgeted(cfg: DuetConfig, budget: Optional[float]) -> DuetConfig:
    """Apply a --budget override.

    A budget stop suspends the session, and lifting it means raising the cap —
    so the cap has to be adjustable at the moment you are blocked, not only by
    editing a config file first.
    """
    if budget is not None:
        cfg.budget.max_usd = budget
    return cfg


def _warn_unpriced(cfg: DuetConfig) -> None:
    """Say so before spending, not after."""
    unpriced = cfg.unpriced_participants()
    if not unpriced:
        return
    names = ", ".join(f"{p.id} ({p.model})" for p in unpriced)
    console.print(
        f"[yellow]warning[/]: no price configured for {names} — their spend counts as "
        f"$0.00, so the ${cfg.budget.max_usd:.0f} budget cap will not stop this run. "
        # Escaped: rich would read the bracket as a style tag and swallow it.
        f"Add a \\[pricing.\"<model>\"] entry to fix the accounting."
    )


def _live_view(cfg: DuetConfig, store: SessionStore) -> None:
    """Render the debate as it happens. The CLI is a gate client and a viewer;
    it drives nothing (SPECS §6.0).

    Model calls block for tens of seconds, so a turn in flight shows a live
    spinner with who is working, on what, and how far through the round cap —
    otherwise a working session is indistinguishable from a hung one.
    """
    badges = {p.id: badge(p) for p in cfg.participants}
    live: dict = {"status": None, "started": 0.0, "elapsed": 0.0, "usage": {}}
    verbs = {"propose": "drafting the spec", "critique": "reviewing the draft"}

    def stop() -> None:
        if live["status"] is not None:
            live["status"].stop()
            live["status"] = None

    def spin(text: str) -> None:
        stop()
        # A renderable rather than a string, so the seconds tick up: the
        # spinner proves the process is alive, the counter shows how long this
        # particular turn has been running.
        live["status"] = console.status(_Ticking(text, time.monotonic()), spinner="dots")
        live["status"].start()

    def cost_bar() -> str:
        return f"${store.cost_usd:.2f}/${cfg.budget.max_usd:.0f}"

    def took() -> str:
        tokens = live["usage"].get("output_tokens", 0)
        return f"[dim]{live['elapsed']:.0f}s · {tokens} tok · {cost_bar()}[/]"

    def render(event: Event) -> None:
        p = event.payload
        who = badges.get(p.get("participant"), "")
        if event.type == "model_call":
            live["started"] = time.monotonic()
            verb = verbs.get(p.get("purpose", ""), p.get("purpose", "working"))
            repair = " [yellow](output was invalid — retrying)[/]" if p.get("repair") else ""
            # The cap rides on the event: a Gate-1 revision raises it mid-session.
            rounds = f"round {p.get('round', '?')}/{p.get('round_cap', '?')}"
            spin(f"{who} {verb} · {rounds} · {cost_bar()}{repair}")
        elif event.type == "model_response":
            live["elapsed"] = time.monotonic() - live["started"]
            live["usage"] = p.get("usage", {})
        elif event.type == "draft":
            stop()
            addressed = f" · addresses {', '.join(p['addressed'])}" if p.get("addressed") else ""
            console.print(
                f"{who} round {p['round']} drafted [bold]{p['hash']}[/]{addressed}  {took()}"
            )
        elif event.type == "verdict":
            stop()
            v = p["verdict"]
            colour = {"AGREE": "green", "REVISE": "yellow", "OBJECT": "red"}.get(v["verdict"], "white")
            note = f" [dim](coerced: {p['coerced']})[/]" if p.get("coerced") else ""
            console.print(f"{who} [bold {colour}]{v['verdict']}[/]{note}  {took()}")
            for o in v["objections"]:
                console.print(f"    [dim]{o['severity']}[/] {o['section']}: {o['claim']}")
            if v.get("resolved_objections"):
                console.print(f"    [dim green]resolved {', '.join(v['resolved_objections'])}[/]")
        elif event.type == "state_change":
            stop()
            console.print(f"[dim]— {p['from']} → {p['to']}[/]")
        elif event.type == "gate_pending":
            stop()
        elif event.type == "error":
            stop()
            console.print(f"{who} [red]error[/]: {p.get('error', '')}")

    store.subscribe(render)


def _edit_text(initial: str, suffix: str = ".md") -> str:
    """Open $EDITOR on some text and return what was saved (SPECS §6.1).

    Written out rather than borrowed from a library: typer vendors click rather
    than depending on it, so click.edit is not importable here.
    """
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as handle:
        handle.write(initial)
        path = Path(handle.name)
    try:
        exit_code = subprocess.call(shlex.split(editor) + [str(path)])
        if exit_code != 0:
            raise RuntimeError(f"{editor!r} exited {exit_code} — edit abandoned")
        return path.read_text()
    finally:
        path.unlink(missing_ok=True)


def _show_artifact(store: SessionStore, ref: str) -> None:
    """Render a gate artifact. Markdown, not raw text: rich would otherwise
    read square brackets in the spec as its own markup and eat or reject them."""
    body = Markdown((store.dir / ref).read_text())
    if console.is_terminal:
        with console.pager(styles=True):
            console.print(body)
    else:
        console.print(body)


RULING_HELP = """\
# Rulings. Write your decision under each objection you want to settle; each
# becomes a FIXED: item in the brief, which the models must treat as settled and
# may not object to. Leave one blank to leave it open — an objection you do not
# rule on comes back.
#
# Lines starting with # are ignored. Save and close when done.
"""


def _collect_rulings(gate) -> str:
    """Ask for a ruling per open objection, in an editor.

    One prompt line cannot hold a ruling: real ones run to paragraphs, and the
    arbitration report promises a decision *per objection* rather than a single
    undifferentiated note. The template carries each objection's id, severity
    and claim so the decision is written next to what it settles.
    """
    if not gate.objections:
        return typer.prompt("what should change")

    blocks = [RULING_HELP]
    for o in gate.objections:
        held = ", ".join(o.get("held_by") or []) or "unresolved"
        claim = "\n".join(f"#   {line}" for line in _wrap(o["claim"], 88))
        blocks.append(
            f"## {o['id']} — {o['severity']} (held by {held})\n"
            f"# section: {o['section']}\n{claim}\n\n"
        )
    edited = _edit_text("\n".join(blocks))

    rulings = []
    current = None
    for line in edited.splitlines():
        if line.startswith("## "):
            current = line[3:].split("—")[0].strip()
            continue
        if line.startswith("#") or not line.strip():
            continue
        if current:
            rulings.append((current, line.strip()))
    merged: dict[str, list[str]] = {}
    for oid, text in rulings:
        merged.setdefault(oid, []).append(text)
    return "\n".join(f"FIXED: {oid} — {' '.join(lines)}" for oid, lines in merged.items())


def _wrap(text: str, width: int) -> list[str]:
    import textwrap as _tw

    return _tw.wrap(text, width) or [""]


def _spec_summary(markdown: str) -> str:
    """The `## In short` section, if the proposer wrote one."""
    marker = "\n## In short\n"
    if marker not in markdown:
        return ""
    body = markdown.split(marker, 1)[1]
    return body.split("\n## ", 1)[0].strip()


def _show_gate(store: SessionStore, gate, detail: str) -> None:
    """What the human needs to decide, not what the builder needs to build.

    The spec runs to tens of thousands of characters because it is written for
    whoever implements it. Paging all of that at a gate buries the decision, so
    the default view is the proposer's own summary plus what the debate cost and
    settled; `view` shows the document itself.
    """
    console.rule(f"[bold]{gate.gate}[/] · {detail}")
    path = store.dir / gate.artifacts[0] if gate.artifacts else None
    markdown = path.read_text() if path and path.is_file() else ""

    summary = _spec_summary(markdown)
    if summary:
        console.print(Markdown(f"## In short\n\n{summary}"))
    elif markdown:
        console.print("[dim](the proposer wrote no summary — use `view` for the spec)[/]")

    f = gate.facts
    if f:
        console.print(
            f"[dim]{f['rounds']} rounds · ${f['cost_usd']:.4f} · "
            f"{f['objections_closed']}/{f['objections_raised']} objections settled"
            + (f", {f['objections_open']} still open" if f.get("objections_open") else "")
            + f" · {f['assumptions']} assumptions made for you"
            + (f", {f['fixed']} decisions of yours kept" if f.get("fixed") else "")
            + "[/]"
        )
    if gate.objections:
        console.print(f"[dim]unresolved:[/] " + ", ".join(
            f"{o['id']} ({o['severity']}, held by {', '.join(o.get('held_by') or []) or '?'})"
            for o in gate.objections
        ))
    if path:
        console.print(f"[dim]full document: {path} ({len(markdown):,} chars)[/]")


def _drive(machine: Machine, store: SessionStore, cfg: DuetConfig, actor: str, result) -> None:
    """Loop between the engine and the human until the session terminates."""
    while result.suspended:
        gate = result.gate
        _show_gate(store, gate, result.detail)

        # `view` is not a decision — it pages the document and asks again.
        choices = gate.options + ["view"]
        action = typer.prompt(f"action ({'/'.join(choices)})")
        while action not in choices:
            action = typer.prompt(f"choose one of {', '.join(choices)}")
        while action == "view":
            for ref in gate.artifacts:
                _show_artifact(store, ref)
            action = typer.prompt(f"action ({'/'.join(choices)})")
            while action not in choices:
                action = typer.prompt(f"choose one of {', '.join(choices)}")
        feedback = ""
        if action == "resolve":
            feedback = _collect_rulings(gate)
            if not feedback.strip():
                console.print("[yellow]no rulings written[/] — nothing would change; pick again")
                continue
            console.print(f"[dim]{feedback.count('FIXED:')} ruling(s) recorded[/]")
        elif action == "revise":
            feedback = typer.prompt("what should change")
        edited = ""
        if action == "edit":
            edited = _edit_text((store.dir / gate.artifacts[0]).read_text())
        submit_gate_decision(
            store, action=action, actor=actor, feedback=feedback, edited_markdown=edited
        )
        result = machine.advance()

    style = {"DONE": "green", "FAILED": "red", "BUDGET_STOP": "yellow"}.get(result.state, "white")
    console.print(f"[bold {style}]{result.state}[/] · {result.detail}")
    console.print(f"session [bold]{store.session_id}[/] · total ${store.cost_usd:.4f}")
    if result.state == "DONE":
        console.print(f"spec: {store.dir / 'artifacts' / 'IMPLEMENTATION.md'}")


@app.command()
def new(
    idea: str = typer.Argument(..., help="Idea text, or @path/to/a/file.md"),
    context_paths: list[Path] = typer.Option(
        [], "--context", help="Reference material; repeatable. Defaults to ./CONTEXT.md"
    ),
    profile: Optional[str] = typer.Option(None),
    config_path: Optional[Path] = typer.Option(None, "--config"),
    actor: str = typer.Option(getpass.getuser(), help="Who is answering the gates"),
    budget: Optional[float] = typer.Option(None, "--budget", help="Override budget.max_usd"),
):
    """Start a session: debate an idea into an approved spec (Phase A)."""
    cfg = _budgeted(load_config(config_path, profile), budget)
    brief = _read_brief(idea)
    context = _load_context(context_paths)
    store = SessionStore.new(owner=actor)
    console.print(f"session [bold]{store.session_id}[/] · {len(cfg.participants)} participants")
    _warn_unpriced(cfg)
    _live_view(cfg, store)
    machine = Machine(cfg, store, providers_for(cfg))
    try:
        result = machine.start(brief, context=context)
    except ContextTooLarge as e:
        console.print(f"[red]context too large[/]: {e}")
        raise typer.Exit(code=1)
    _drive(machine, store, cfg, actor, result)


@app.command()
def resume(
    session_id: str,
    profile: Optional[str] = typer.Option(None),
    config_path: Optional[Path] = typer.Option(None, "--config"),
    actor: str = typer.Option(getpass.getuser(), help="Who is answering the gates"),
    budget: Optional[float] = typer.Option(
        None, "--budget", help="Override budget.max_usd — how you lift a budget stop"
    ),
):
    """Continue a suspended session — including one started by another process."""
    cfg = _budgeted(load_config(config_path, profile), budget)
    try:
        session_id = SessionStore.resolve(session_id)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(code=1)
    store = SessionStore(session_id)
    cfg, notice = pin_roster(cfg, store)
    if notice:
        console.print(f"[yellow]note[/]: {notice}")
    _live_view(cfg, store)
    machine = Machine(cfg, store, providers_for(cfg))
    label = store.label()
    console.print(
        f"resuming [bold]{session_id[:12]}[/] from {machine.current_state()}"
        + (f" · {label}" if label else "")
    )
    _drive(machine, store, cfg, actor, machine.advance())


@app.command()
def review(
    session_id: str = typer.Argument(..., help="Session id, or any unambiguous prefix"),
    file: Optional[Path] = typer.Option(None, "--file", help="Spec to review; default: edit the session's own"),
    profile: Optional[str] = typer.Option(None),
    config_path: Optional[Path] = typer.Option(None, "--config"),
    actor: str = typer.Option(getpass.getuser(), help="Who is proposing the revision"),
    budget: Optional[float] = typer.Option(None, "--budget", help="Override budget.max_usd"),
):
    """Put an edited spec back in front of the models for one critique round.

    You take the proposer turn: you supply the revision, they critique it. One
    call per participant, no revision cycle — cheap enough to use on every edit.
    """
    cfg = _budgeted(load_config(config_path, profile), budget)
    try:
        session_id = SessionStore.resolve(session_id)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(code=1)

    store = SessionStore(session_id)
    cfg, notice = pin_roster(cfg, store)
    if notice:
        console.print(f"[yellow]note[/]: {notice}")
    current = (store.dir / "artifacts" / "IMPLEMENTATION.md")
    if file is not None:
        markdown = file.read_text()
    elif not current.is_file():
        console.print(f"[red]no IMPLEMENTATION.md in {session_id[:12]} — pass --file[/]")
        raise typer.Exit(code=1)
    else:
        markdown = _edit_text(current.read_text())
        if markdown == current.read_text():
            console.print("[dim]spec unchanged — reviewing it as-is[/]")

    _live_view(cfg, store)
    machine = Machine(cfg, store, providers_for(cfg))
    result = machine.review(markdown, actor)
    if not result.suspended and result.state not in ("GATE_1",):
        console.print(f"[red]{result.state}[/] · {result.detail}")
        raise typer.Exit(code=1)
    _drive(machine, store, cfg, actor, result)


@app.command()
def roster(
    profile: Optional[str] = typer.Option(None),
    config_path: Optional[Path] = typer.Option(None, "--config"),
):
    """Show the configured participants and the roles they take on task 1."""
    cfg = load_config(config_path, profile)
    coder, reviewers = cfg.roles_for_task(0)
    reviewer_ids = {r.id for r in reviewers}
    table = Table("participant", "provider", "model", "task-1 role", "$/Mtok in-out")
    for p in cfg.participants:
        role = "coder" if p.id == coder.id else ("reviewer" if p.id in reviewer_ids else "-")
        price = cfg.pricing_for(p.model)
        cost = f"{price.input_per_mtok:g} / {price.output_per_mtok:g}" if price else "[dim]unpriced[/]"
        table.add_row(f"{badge(p)} {p.id}", p.provider, p.model, role, cost)
    console.print(table)
    console.print(
        f"[dim]consensus: {cfg.debate.consensus} — "
        f"{cfg.debate.required_agreements(len(cfg.participants))} of {len(cfg.participants)} must AGREE[/]"
    )


@app.command("config")
def show_config(
    profile: Optional[str] = typer.Option(None),
    config_path: Optional[Path] = typer.Option(None, "--config"),
):
    """Print the resolved configuration."""
    cfg = load_config(config_path, profile)
    console.print_json(cfg.model_dump_json())


@app.command()
def sessions():
    """List sessions with state and cost."""
    # One row per session: a wrapped label turns a listing you scan into one you
    # have to read.
    table = Table(
        "session",
        Column("about", no_wrap=True, overflow="ellipsis"),
        "owner",
        "state",
        "cost",
    )
    for sid in SessionStore.list_sessions():
        store = SessionStore(sid)
        last_state = "-"
        for e in store.events():
            if e.type == "state_change":
                last_state = e.payload.get("to", "-")
        style = {"DONE": "green", "FAILED": "red", "BUDGET_STOP": "yellow"}.get(last_state, "")
        # Short ids are enough to read and enough to resume: `duet resume`
        # accepts any unambiguous prefix.
        table.add_row(
            sid[:12],
            store.label() or "[dim]—[/]",
            store.owner or "[dim]anonymous[/]",
            f"[{style}]{last_state}[/]" if style else last_state,
            f"${store.cost_usd:.4f}",
        )
    console.print(table)


if __name__ == "__main__":
    app()
