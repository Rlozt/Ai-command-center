"""Tools backed by app services: staging, diffing, memory, reports and source checks."""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..reports import build_report
from ..schemas import ProposedFile
from ..staging import StagingError, make_diff
from .base import Tool, ToolContext, ToolError
from .webcheck import excerpt_in_text


def _svc(ctx: ToolContext, name: str):
    svc = (ctx.services or {}).get(name)
    if svc is None:
        raise ToolError("unavailable", f"service '{name}' is not available")
    return svc


class StageIn(BaseModel):
    title: str = "Staged files"
    summary: str = ""
    files: list[ProposedFile] = Field(min_length=1)


class StageOut(BaseModel):
    proposal_id: str
    version: int
    content_hash: str
    files: list[str]


async def stage_files(ctx: ToolContext, a: StageIn) -> StageOut:
    if not ctx.task_id:
        raise ToolError("no_task", "stage_files needs a task")
    try:
        p = _svc(ctx, "staging").create_proposal(ctx.task_id, ctx.project, a.files, a.title, a.summary, "tool")
    except StagingError as e:
        raise ToolError("staging_rejected", str(e)) from e
    return StageOut(proposal_id=p["id"], version=p["version"], content_hash=p["content_hash"], files=[f["path"] for f in p["files"]])


class DiffIn(BaseModel):
    path: str
    old: str | None = None
    new: str


class DiffOut(BaseModel):
    diff: str


async def diff_text(ctx: ToolContext, a: DiffIn) -> DiffOut:
    return DiffOut(diff=make_diff(a.path, a.old, a.new))


class MemSearchIn(BaseModel):
    query: str
    k: int = Field(default=5, le=20)


class MemSearchOut(BaseModel):
    confirmed: list[dict]
    unconfirmed: list[dict]


async def memory_search(ctx: ToolContext, a: MemSearchIn) -> MemSearchOut:
    r = _svc(ctx, "memory").retrieve((ctx.project or {}).get("id"), a.query, a.k)
    return MemSearchOut(**r)


class MemAddIn(BaseModel):
    kind: str = "finding"
    title: str
    body: str
    status: str = "hypothesis"  # tools can never write confirmed memory


class MemAddOut(BaseModel):
    id: str
    status: str


async def memory_add(ctx: ToolContext, a: MemAddIn) -> MemAddOut:
    if a.status == "confirmed":
        raise ToolError("not_permitted", "only the user can mark memory as confirmed")
    try:
        m = _svc(ctx, "memory").add((ctx.project or {}).get("id"), a.kind, a.title, a.body, a.status, source="tool")
    except ValueError as e:
        raise ToolError("invalid", str(e)) from e
    return MemAddOut(id=m["id"], status=m["status"])


class InstrOut(BaseModel):
    instructions: str


class Empty(BaseModel):
    pass


async def project_instructions(ctx: ToolContext, a: Empty) -> InstrOut:
    return InstrOut(instructions=(ctx.project or {}).get("instructions", ""))


class ReportIn(BaseModel):
    task_id: str


class ReportOut(BaseModel):
    report: dict


async def task_report(ctx: ToolContext, a: ReportIn) -> ReportOut:
    if not ctx.store.get_task(a.task_id):
        raise ToolError("not_found", "unknown task")
    return ReportOut(report=build_report(ctx.store, a.task_id))


class VerifyIn(BaseModel):
    url: str
    excerpt: str = Field(min_length=20)


class VerifyOut(BaseModel):
    opened: bool
    excerpt_found: bool
    detail: str


async def web_verify(ctx: ToolContext, a: VerifyIn) -> VerifyOut:
    fetcher = (ctx.services or {}).get("fetcher")
    if fetcher is None or not ctx.settings.get("allow_web_verification", True):
        raise ToolError("not_configured", "web verification is disabled")
    res = await fetcher.fetch(a.url)
    if not res.ok:
        return VerifyOut(opened=False, excerpt_found=False, detail=res.error)
    found = excerpt_in_text(a.excerpt, res.text)
    return VerifyOut(opened=True, excerpt_found=found, detail="excerpt found" if found else "page opened; excerpt not found")


def register(reg) -> None:
    reg.register(Tool("stage_files", "Stage proposed files for review (never writes to the project).", "stage", StageIn, StageOut, stage_files,
                      "Writes only under the app's staging directory; paths are validated; protected names refused."))
    reg.register(Tool("diff_text", "Produce a unified diff between two texts.", "read", DiffIn, DiffOut, diff_text, "Pure function; no I/O."))
    reg.register(Tool("memory_search", "Targeted retrieval from project memory.", "memory", MemSearchIn, MemSearchOut, memory_search,
                      "Reads memory items of the current project only."))
    reg.register(Tool("memory_add", "Add an UNCONFIRMED note to project memory.", "memory", MemAddIn, MemAddOut, memory_add,
                      "Cannot create confirmed memory; only the user can confirm."))
    reg.register(Tool("project_instructions", "Return the project's PROJECT.md instructions.", "read", Empty, InstrOut, project_instructions,
                      "Current project only."))
    reg.register(Tool("task_report", "Build the structured report for a task.", "read", ReportIn, ReportOut, task_report, "Reads stored task facts only."))
    reg.register(Tool("web_verify", "Open a public URL and check whether an excerpt appears on it.", "network", VerifyIn, VerifyOut, web_verify,
                      "Public http(s) hosts only (SSRF-guarded, redirects re-checked); content is compared in code and never sent to a model."))
