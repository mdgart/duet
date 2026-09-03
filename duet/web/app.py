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
    profile: str = ""
    budget: float | None = None


class GateDecision(BaseModel):
    action: str
    actor: str = "web"
    feedback: str = ""
    edited_markdown: str = ""


class RaiseBudget(BaseModel):
    budget: float
    actor: str = "web"


# The project's own docs, readable in the UI. A fixed registry rather than a
# path parameter: the id maps to a known filename, so no request can traverse
# out of the docs directory.
DOC_FILES: dict[str, tuple[str, str]] = {
    "readme": ("Getting started", "README.md"),
    "plan": ("Project plan", "PLAN.md"),
    "specs": ("Technical spec", "SPECS.md"),
}


def docs_root() -> Path | None:
    """Where the markdown docs live: shipped beside the package, else the checkout."""
    for candidate in (Path(__file__).parent / "_docs", Path(__file__).resolve().parents[2]):
        if (candidate / "README.md").is_file():
            return candidate
    return None


def available_profiles(root: Path | None) -> list[str]:
    """Profile names under ~/.duet/profiles (or DUET_HOME)."""
    from duet.schemas.config import duet_home

    base = (root / "profiles") if root else duet_home() / "profiles"
    if not base.is_dir():
        return []
    return sorted(p.stem for p in base.glob("*.toml"))


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
        "budget_usd": machine.session_budget() or runner.config_for(store).budget.max_usd,
        "running": runner.is_running(session_id),
        "gate": gate,
        "summary": summary,
        "document": document,
    }


def create_app(
    config: DuetConfig | None = None,
    root: Path | None = None,
    config_path: Path | None = None,
    base_profile: str | None = None,
) -> FastAPI:
    # A fixed config (tests) or per-session profile loading (the real server).
    runner = Runner(
        config=config if config is not None else (None if (config_path or base_profile) else load_config()),
        config_path=config_path,
        base_profile=base_profile,
        root=root,
    )
    app = FastAPI(title="duet")
    app.state.runner = runner

    @app.get("/api/templates")
    def templates() -> list[dict]:
        return [{"name": t.name, "label": t.label, "when": t.when} for t in TEMPLATES.values()]

    @app.get("/api/docs")
    def docs() -> list[dict]:
        base = docs_root()
        if base is None:
            return []
        return [
            {"id": doc_id, "title": title}
            for doc_id, (title, filename) in DOC_FILES.items()
            if (base / filename).is_file()
        ]

    @app.get("/api/docs/{doc_id}")
    def doc(doc_id: str) -> dict:
        base = docs_root()
        if base is None or doc_id not in DOC_FILES:
            raise HTTPException(404, "no such document")
        title, filename = DOC_FILES[doc_id]
        path = base / filename
        if not path.is_file():
            raise HTTPException(404, "no such document")
        return {"id": doc_id, "title": title, "markdown": path.read_text()}

    @app.get("/api/profiles")
    def profiles() -> dict:
        return {
            "profiles": available_profiles(root),
            "default_budget": runner._load(None, None).budget.max_usd,
        }

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
        if body.profile and body.profile not in available_profiles(root):
            raise HTTPException(400, f"unknown profile {body.profile!r}")
        sid = runner.start(
            body.idea, body.template, context=body.context,
            profile=body.profile, budget=body.budget,
        )
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

    @app.post("/api/sessions/{session_id}/budget")
    def raise_budget(session_id: str, body: RaiseBudget) -> dict:
        runner.raise_budget(session_id, body.budget, body.actor)
        return {"ok": True}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/", StaticFiles(directory=STATIC), name="static")
    return app
