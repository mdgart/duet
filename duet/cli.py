"""Duet CLI (M0: ping, config, sessions)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from duet import __version__
from duet.providers.base import get_provider
from duet.schemas.config import load_config
from duet.schemas.model_io import ModelRequest, Turn
from duet.store.session import SessionStore

app = typer.Typer(help=f"Duet v{__version__} — cross-model agent loop")
console = Console()

BADGE = {"openai": "[bold cyan]\\[SOL][/]", "anthropic": "[bold dark_orange]\\[OPUS][/]"}


@app.command()
def ping(
    profile: Optional[str] = typer.Option(None, help="Config profile name"),
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to duet.toml"),
):
    """M0 exit criterion: both models answer through one interface; cost logged."""
    cfg = load_config(config_path, profile)
    store = SessionStore.new()
    console.print(f"session [bold]{store.session_id}[/]")

    req = ModelRequest(
        system="You are a connectivity check. Reply with exactly one word.",
        messages=[Turn(role="user", content="Reply with the single word: pong")],
        max_output_tokens=16,
    )
    for name in ("openai", "anthropic"):
        try:
            provider = get_provider(name, cfg)
            store.append("model_call", {"provider": name, "model_id": provider.model_id, "purpose": "ping"})
            resp = provider.complete(req)
        except Exception as e:  # noqa: BLE001
            store.append("error", {"provider": name, "error": str(e)})
            console.print(f"{BADGE[name]} [red]FAILED[/]: {e}")
            raise typer.Exit(code=1)
        store.append("model_response", {"provider": name, "text": resp.text, "usage": resp.usage.model_dump()})
        store.add_cost(resp.model_id, resp.usage.model_dump(), cfg.budget.max_usd)
        console.print(
            f"{BADGE[name]} {resp.text.strip()!r}  "
            f"[dim]{resp.usage.input_tokens}in/{resp.usage.output_tokens}out · ${resp.usage.cost_usd:.4f}[/]"
        )
    console.print(f"[green]ok[/] · total ${store.cost_usd:.4f} · log: {store.log_path}")


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
    table = Table("session", "events", "last state", "cost")
    for sid in SessionStore.list_sessions():
        store = SessionStore(sid)
        last_state, count = "-", 0
        for e in store.events():
            count += 1
            if e.type == "state_change":
                last_state = e.payload.get("to", "-")
        table.add_row(sid, str(count), last_state, f"${store.cost_usd:.4f}")
    console.print(table)


if __name__ == "__main__":
    app()
