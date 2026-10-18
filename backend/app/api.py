"""HTTP API (FastAPI). Local-only by default: loopback bind, Host/Origin checks and a per-install session token."""
from __future__ import annotations

import hmac
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__
from . import statemachine as sm
from .config import Config, load_config
from .context import AppContext, build_context
from .locking import InstanceLock
from .memory import KINDS, STATUSES
from .providers.base import AgentRequest
from .security import redact
from .settings_service import PROVIDERS
from .staging import ApplyConflict, ApprovalError, StagingError

ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


class TaskIn(BaseModel):
    request: str = Field(min_length=3, max_length=8000)
    project_id: str | None = None
    routing_mode: str | None = None
    manual_routes: dict[str, str] = Field(default_factory=dict)
    title: str | None = None


class ResumeIn(BaseModel):
    clarification: str = ""


class ApproveIn(BaseModel):
    content_hash: str
    note: str = ""
    acknowledge_issues: bool = False


class RejectIn(BaseModel):
    note: str = ""
    request_revision: bool = True


class EditFileIn(BaseModel):
    path: str
    content: str


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    root_dir: str = ""
    test_command: str = ""
    instructions: str = ""


class ProjectPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    root_dir: str | None = None
    test_command: str | None = None
    instructions: str | None = None


class MemoryIn(BaseModel):
    kind: str = "finding"
    title: str = Field(min_length=1, max_length=200)
    body: str = ""
    status: str = "hypothesis"


class MemoryPatch(BaseModel):
    kind: str | None = None
    status: str | None = None
    title: str | None = None
    body: str | None = None


class ClaimPatch(BaseModel):
    status: str
    note: str = ""


class TestRequestIn(BaseModel):
    deep: bool = False


def _valid_root(path: str) -> str:
    if not path:
        return ""
    p = Path(path).expanduser()
    if not p.is_dir():
        raise HTTPException(400, f"Directory does not exist: {path}")
    r = p.resolve()
    if r == Path(r.anchor) or r == Path.home().resolve():
        raise HTTPException(400, "Refusing to use a drive root or your whole home folder as a project directory; choose a specific project folder.")
    return str(r)


def create_app(ctx: AppContext | None = None, cfg: Config | None = None) -> FastAPI:
    owns_ctx = ctx is None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal ctx
        lock = None
        if owns_ctx:
            c = cfg or load_config()
            lock = InstanceLock(c.data_dir / "instance.lock")
            if not lock.acquire():
                raise RuntimeError("Another AI Command Center instance is already using this data directory.")
            ctx = build_context(c)
            ctx.engine.recover()
        app.state.ctx = ctx
        app.state.token = _load_token(ctx.cfg)
        yield
        await ctx.engine.shutdown()
        await ctx.registry.aclose()
        if owns_ctx:
            ctx.db.close()
            if lock:
                lock.release()

    app = FastAPI(title="AI Command Center", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    if ctx is not None:  # tests: context supplied, lifespan still runs for token/shutdown
        app.state.ctx = ctx
        app.state.token = _load_token(ctx.cfg)

    # ----------------------------------------------------------- security middleware
    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = _hostname(request.headers.get("host") or "")
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "Host not allowed"}, status_code=403)  # blocks DNS-rebinding
        origin = request.headers.get("origin")
        allowed = app.state.ctx.cfg.allowed_origins
        if origin and origin.rstrip("/") not in allowed:
            return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
        path = request.url.path
        if path.startswith("/api") and path not in ("/api/session", "/api/health"):
            sent = request.headers.get("x-acc-token", "")
            if not hmac.compare_digest(sent.encode(), app.state.token.encode()):
                return JSONResponse({"detail": "Missing or invalid session token"}, status_code=401)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    r = APIRouter(prefix="/api")

    def C() -> AppContext:
        return app.state.ctx

    def task_or_404(tid: str) -> dict:
        t = C().store.get_task(tid)
        if not t:
            raise HTTPException(404, "task not found")
        return t

    @r.get("/session")
    def session():
        return {"token": app.state.token, "mode": C().cfg.mode, "version": __version__}

    @r.get("/health")
    def health():
        return {"ok": True, "mode": C().cfg.mode, "version": __version__}

    # ----------------------------------------------------------- dashboard
    @r.get("/dashboard")
    def dashboard():
        c = C()
        tasks = c.store.list_tasks(limit=100)
        active_states = {sm.PENDING, sm.PLANNING, sm.READY, sm.RUNNING, sm.REVIEWING, sm.REPAIRING, sm.APPROVED, sm.APPLYING, sm.AWAITING_APPROVAL}
        current = [_brief(c, t) for t in tasks if t["status"] in active_states]
        done = [_brief(c, t) for t in tasks if t["status"] == sm.COMPLETED][:8]
        failed = [_brief(c, t) for t in tasks if t["status"] == sm.FAILED][:8]
        assignments = c.db.all("SELECT s.id, s.title, s.kind, s.agent, s.status, s.task_id, t.title AS task_title FROM subtasks s JOIN tasks t ON t.id=s.task_id "
                               "WHERE s.status='running' ORDER BY s.started_at")
        return {"mode": c.cfg.mode, "current_tasks": current, "recently_completed": done, "needs_attention": failed,
                "assignments": assignments, "pending_approvals": _pending_approvals(c),
                "recent_errors": c.store.recent_errors(8), "usage": c.budget.summary(),
                "providers": [s.__dict__ for s in c.registry.status()],
                "counts": {"total": len(tasks)}}

    def _brief(c: AppContext, t: dict) -> dict:
        subs = c.store.subtasks(t["id"])
        return {"id": t["id"], "title": t["title"], "status": t["status"], "outcome": t["outcome"], "paused": t["paused"],
                "pause_reason": t["pause_reason"], "created_at": t["created_at"], "updated_at": t["updated_at"],
                "subtasks_done": sum(1 for s in subs if s["status"] == "done"), "subtasks_total": len(subs),
                "agents": sorted({s["agent"] for s in subs}), "error": t["error"], "cost": c.store.task_usage(t["id"])["cost_usd"],
                "demo": c.store.task_usage(t["id"])["demo"],
                "local": (lambda u: u["local_calls"] > 0 and u["billable_calls"] == 0)(c.store.task_usage(t["id"]))}

    def _pending_approvals(c: AppContext) -> list[dict]:
        rows = c.db.all("SELECT p.*, t.title AS task_title FROM proposals p JOIN tasks t ON t.id=p.task_id WHERE p.status='pending' ORDER BY p.created_at")
        for p in rows:
            p["files"] = c.db.all("SELECT * FROM proposal_files WHERE proposal_id=? ORDER BY id", (p["id"],))
        return rows

    # ----------------------------------------------------------- tasks
    @r.post("/tasks", status_code=201)
    async def create_task(body: TaskIn):
        c = C()
        if body.project_id and not c.store.get_project(body.project_id):
            raise HTTPException(404, "project not found")
        mode = body.routing_mode or c.settings.get_key("routing_mode")
        if mode not in ("auto", "manual"):
            raise HTTPException(400, "routing_mode must be auto or manual")
        for kind, prov in body.manual_routes.items():
            if prov not in PROVIDERS:
                raise HTTPException(400, f"unknown provider in manual_routes: {prov}")
        t = c.store.create_task(body.request.strip(), body.project_id, mode, body.manual_routes, body.title)
        c.engine.start(t["id"])
        return t

    @r.get("/tasks")
    def list_tasks(status: str | None = None):
        c = C()
        return [_brief(c, t) for t in c.store.list_tasks(status.split(",") if status else None)]

    @r.get("/tasks/{tid}")
    def task_detail(tid: str):
        c = C()
        t = task_or_404(tid)
        return {"task": t, "subtasks": c.store.subtasks(tid), "logs": c.store.logs(tid), "calls": c.store.calls(task_id=tid),
                "reviews": c.store.reviews(tid), "tests": c.store.test_runs(tid), "proposals": c.store.proposals(tid),
                "approvals": c.store.approvals(tid), "claims": c.store.claims(tid), "sources": c.store.sources(tid),
                "events": c.store.state_events(tid), "usage": c.store.task_usage(tid), "running": c.engine.is_running(tid),
                "limits": c.settings.get_key("limits")}

    @r.post("/tasks/{tid}/pause")
    def pause(tid: str):
        task_or_404(tid)
        C().engine.pause(tid)
        return C().store.get_task(tid)

    @r.post("/tasks/{tid}/resume")
    async def resume(tid: str, body: ResumeIn | None = None):
        t = task_or_404(tid)
        if t["status"] in (sm.COMPLETED, sm.CANCELLED, sm.FAILED, sm.AWAITING_APPROVAL):
            raise HTTPException(409, f"task is {t['status']}; nothing to resume")
        C().engine.resume(tid, (body.clarification if body else ""))
        return C().store.get_task(tid)

    @r.post("/tasks/{tid}/retry")
    async def retry(tid: str):
        task_or_404(tid)
        try:
            C().engine.retry(tid)
        except sm.IllegalTransition as e:
            raise HTTPException(409, str(e))
        return C().store.get_task(tid)

    @r.post("/tasks/{tid}/cancel")
    async def cancel(tid: str):
        task_or_404(tid)
        await C().engine.cancel(tid)
        return C().store.get_task(tid)

    # ----------------------------------------------------------- approvals
    @r.get("/approvals")
    def approvals():
        return _pending_approvals(C())

    @r.post("/proposals/{pid}/approve")
    async def approve(pid: str, body: ApproveIn):
        try:
            return await C().engine.approve(pid, body.content_hash, body.note, body.acknowledge_issues)
        except ApprovalError as e:
            raise HTTPException(409, str(e))

    @r.post("/proposals/{pid}/reject")
    async def reject(pid: str, body: RejectIn):
        try:
            return C().engine.reject(pid, body.note, body.request_revision)
        except ApprovalError as e:
            raise HTTPException(409, str(e))

    @r.put("/proposals/{pid}/files")
    def edit_file(pid: str, body: EditFileIn):
        c = C()
        p = c.store.get_proposal(pid)
        if not p:
            raise HTTPException(404, "proposal not found")
        task = c.store.get_task(p["task_id"])
        try:
            return c.staging.edit_staged_file(pid, body.path, body.content, c.store.get_project(task["project_id"]))
        except StagingError as e:
            raise HTTPException(400, str(e))

    @r.get("/proposals/{pid}/file")
    def read_staged(pid: str, path: str):
        try:
            return {"path": path, "content": C().staging.read_staged(pid, path)}
        except StagingError as e:
            raise HTTPException(404, str(e))

    @r.post("/proposals/{pid}/revert")
    def revert(pid: str):
        c = C()
        p = c.store.get_proposal(pid)
        if not p:
            raise HTTPException(404, "proposal not found")
        task = c.store.get_task(p["task_id"])
        try:
            return {"restored": c.staging.revert(pid, c.store.get_project(task["project_id"]))}
        except (StagingError, ApplyConflict) as e:
            raise HTTPException(409, str(e))

    # ----------------------------------------------------------- claims
    @r.patch("/claims/{cid}")
    def patch_claim(cid: int, body: ClaimPatch):
        if body.status not in ("verified", "unverified", "conflicting", "outdated"):
            raise HTTPException(400, "invalid status")
        C().store.set_claim_status(cid, body.status, ("manual: " + body.note)[:500])
        return {"ok": True}

    # ----------------------------------------------------------- projects and memory
    @r.get("/projects")
    def projects():
        return C().store.list_projects()

    @r.post("/projects", status_code=201)
    def create_project(body: ProjectIn):
        return C().store.create_project(body.name, body.description, _valid_root(body.root_dir), body.test_command, body.instructions)

    @r.patch("/projects/{pid}")
    def patch_project(pid: str, body: ProjectPatch):
        if not C().store.get_project(pid):
            raise HTTPException(404, "project not found")
        fields = body.model_dump(exclude_none=True)
        if "root_dir" in fields:
            fields["root_dir"] = _valid_root(fields["root_dir"])
        return C().store.update_project(pid, **fields)

    @r.delete("/projects/{pid}", status_code=204)
    def delete_project(pid: str):
        try:
            C().store.delete_project(pid)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @r.get("/projects/{pid}/memory")
    def memory(pid: str):
        return C().memory.list(pid)

    @r.post("/projects/{pid}/memory", status_code=201)
    def add_memory(pid: str, body: MemoryIn):
        if body.kind not in KINDS or body.status not in STATUSES:
            raise HTTPException(400, "invalid kind or status")
        return C().memory.add(pid, body.kind, body.title, body.body, body.status)

    @r.patch("/memory/{mid}")
    def patch_memory(mid: str, body: MemoryPatch):
        if not C().memory.get(mid):
            raise HTTPException(404, "memory item not found")
        try:
            return C().memory.update(mid, **body.model_dump(exclude_none=True))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @r.delete("/memory/{mid}", status_code=204)
    def delete_memory(mid: str):
        C().memory.delete(mid)

    @r.get("/projects/{pid}/export")
    def export(pid: str, format: str = "json"):
        if not C().store.get_project(pid):
            raise HTTPException(404, "project not found")
        if format == "markdown":
            return PlainTextResponse(C().memory.export_markdown(pid), media_type="text/markdown")
        return C().memory.export(pid)

    # ----------------------------------------------------------- agents, usage, settings
    @r.get("/agents")
    def agents():
        c = C()
        out = []
        for st in c.registry.status():
            running = c.db.all("SELECT s.id, s.title, s.kind, s.task_id, t.title AS task_title FROM subtasks s JOIN tasks t ON t.id=s.task_id WHERE s.agent=? AND s.status='running'", (st.provider,))
            queued = c.db.all("SELECT s.id, s.title, s.kind, s.task_id, t.title AS task_title FROM subtasks s JOIN tasks t ON t.id=s.task_id WHERE s.agent=? AND s.status='ready' AND t.status IN ('running','ready','repairing') LIMIT 10", (st.provider,))
            calls = c.store.calls(provider=st.provider, limit=30)
            last_err = next((x for x in calls if x["status"] == "error"), None)
            state = "disabled" if not st.enabled else ("not_configured" if not st.configured else ("working" if running else "idle"))
            out.append({**st.__dict__, "state": state, "running": running, "queued": queued,
                        "recent_results": [x for x in calls if x["status"] == "ok"][:5], "last_error": last_err,
                        "usage": c.budget.summary()["providers"][st.provider]})
        return out

    @r.get("/calls")
    def calls(provider: str | None = None, limit: int = 100):
        return C().store.calls(provider=provider, limit=min(limit, 500))

    @r.get("/settings")
    def get_settings():
        c = C()
        s = c.settings.get()
        s["providers_status"] = [x.__dict__ for x in c.registry.status()]
        s["mode"] = c.cfg.mode
        s["models"] = {n: c.registry.get(n).model for n in PROVIDERS if n in c.registry.providers}
        s["credentials"] = {n: ("configured" if c.registry.get(n).is_configured() else "missing") for n in PROVIDERS if n in c.registry.providers}
        s["usage"] = c.budget.summary()
        return s

    @r.put("/settings")
    def put_settings(patch: dict):
        try:
            C().settings.update(patch)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return get_settings()

    @r.post("/providers/{name}/validate")
    async def validate(name: str):
        if name not in PROVIDERS:
            raise HTTPException(404, "unknown provider")
        ok, msg = await C().registry.validate(name)
        return {"provider": name, "ok": ok, "message": redact(msg)}

    @r.post("/providers/{name}/test")
    async def test_request(name: str, body: TestRequestIn):
        """Send one tiny real request (counts toward spend and limits). Requires deep=true as explicit consent."""
        c = C()
        if name not in PROVIDERS:
            raise HTTPException(404, "unknown provider")
        if not body.deep:
            raise HTTPException(400, "pass deep=true to confirm you want to send a (billable) test request")
        try:
            res = await c.gateway.call(name, AgentRequest(purpose="ping", prompt="Reply with the single word: pong", max_output_tokens=16, expect_json=False))
        except Exception as e:
            return {"provider": name, "ok": False, "message": redact(str(e))}
        return {"provider": name, "ok": True, "message": f"Got a response from {res.model}.", "cost_usd": res.usage.cost_usd,
                "billable": c.registry.get(name).billable}

    @r.get("/tools")
    def tools():
        return C().tools.describe()

    app.include_router(r)

    # ----------------------------------------------------------- static frontend
    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        dist = C().cfg.frontend_dir
        if full_path.startswith("api/"):
            raise HTTPException(404)
        target = (dist / full_path).resolve()
        if full_path and target.is_file() and dist.resolve() in target.parents:
            return FileResponse(target)
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse({"detail": "Frontend not built. Run scripts/setup and scripts/build, or use the Vite dev server."}, status_code=404)

    return app


def _hostname(hostport: str) -> str:
    h = hostport.strip().lower()
    if h.startswith("["):
        return h.split("]")[0] + "]"
    return h.rsplit(":", 1)[0] if ":" in h else h


def _load_token(cfg: Config) -> str:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    p = cfg.token_path
    if p.is_file() and p.read_text().strip():
        return p.read_text().strip()
    tok = secrets.token_urlsafe(32)
    p.write_text(tok)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return tok


def app_factory() -> FastAPI:  # uvicorn --factory target
    return create_app()
