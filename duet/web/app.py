"""FastAPI app: read the session log in a browser, answer gates there.

Every endpoint is a thin view over the event log or a call into the same Machine
the CLI uses. No engine logic lives here — the web app is a client (SPECS §6.0).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from duet.schemas.config import DuetConfig, load_config
from duet.schemas.templates import TEMPLATES
from duet.store.session import SessionStore
from duet.web.runner import Runner

STATIC = Path(__file__).parent / "static"


class NewSession(BaseModel):
    idea: str
    template: str = "software"
    context: str = ""


class GateDecision(BaseModel):
    action: str
    actor: str = "web"
    feedback: str = ""
    edited_markdown: str = ""


def session_snapshot(runner: Runner, session_id: str) -> dict[str, Any]:
    """Everything a browser needs to render one session, from the log alone."""
    store = runner.store(session_id)
    machine = runner.machine(store)
    state = machine.current_state()
    pending, decision = machine.pending_gate()
    gate = pending if (pending and decision is None) else None

    document = ""
    summary = ""
    for event in store.events():
        if event.type == "draft":
            document = event.payload.get("markdown", "")
    if not document:
        impl = store.dir / "artifacts" / "IMPLEMENTATION.md"
        if impl.is_file():
            document = impl.read_text()
    if "\n## In short\n" in document:
        summary = document.split("\n## In short\n", 1)[1].split("\n## ", 1)[0].strip()

    return {
        "id": session_id,
        "label": store.label(60),
        "state": state,
        "template": machine.template(),
        "brief": machine.brief(),
        "cost_usd": round(store.cost_usd, 4),
        "running": runner.is_running(session_id),
        "gate": gate,
        "summary": summary,
        "document": document,
    }


def create_app(config: DuetConfig | None = None, root: Path | None = None) -> FastAPI:
    runner = Runner(config=config or load_config(), root=root)
    app = FastAPI(title="duet")
    app.state.runner = runner

    @app.get("/api/templates")
    def templates() -> list[dict]:
        return [{"name": t.name, "label": t.label, "when": t.when} for t in TEMPLATES.values()]

    @app.get("/api/sessions")
    def sessions() -> list[dict]:
        out = []
        for sid in SessionStore.list_sessions(root=root):
            store = SessionStore(sid, root=root)
            state = "INIT"
            for e in store.events():
                if e.type == "state_change":
                    state = e.payload.get("to", state)
            out.append({
                "id": sid,
                "label": store.label(60) or "(untitled)",
                "state": state,
                "owner": store.owner or "anonymous",
                "cost_usd": round(store.cost_usd, 4),
                "running": runner.is_running(sid),
            })
        return out

    @app.post("/api/sessions")
    def new_session(body: NewSession) -> dict:
        if body.template not in TEMPLATES:
            raise HTTPException(400, f"unknown template {body.template!r}")
        if not body.idea.strip():
            raise HTTPException(400, "idea is empty")
        sid = runner.start(body.idea, body.template, context=body.context)
        return {"id": sid}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict:
        if not (SessionStore(session_id, root=root).log_path).is_file():
            raise HTTPException(404, "no such session")
        return session_snapshot(runner, session_id)

    @app.get("/api/sessions/{session_id}/events")
    def events(session_id: str) -> StreamingResponse:
        return StreamingResponse(
            runner.stream(session_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/sessions/{session_id}/gate")
    def gate(session_id: str, body: GateDecision) -> dict:
        try:
            runner.submit_gate(
                session_id,
                action=body.action,
                actor=body.actor,
                feedback=body.feedback,
                edited_markdown=body.edited_markdown,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/", StaticFiles(directory=STATIC), name="static")
    return app
