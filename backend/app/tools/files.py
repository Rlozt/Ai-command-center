"""Read-only project file tools. The ONLY directory they can touch is the project's configured root."""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from pydantic import BaseModel, Field

from ..security import PathError, safe_join, validate_relative
from .base import Tool, ToolContext, ToolError
from .documents import MAX_READ_BYTES, extract_text

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".acc-backups", "dist", "build", ".idea", ".vscode"}


def project_root(ctx: ToolContext) -> Path:
    if not ctx.project or not ctx.project.get("root_dir"):
        raise ToolError("no_root", "This project has no permitted directory configured, so file access is disabled.")
    root = Path(ctx.project["root_dir"]).expanduser()
    if not root.is_dir():
        raise ToolError("root_missing", f"Project directory does not exist: {root}")
    return root.resolve()


class ReadIn(BaseModel):
    path: str


class ReadOut(BaseModel):
    path: str
    format: str
    text: str
    truncated: bool
    size: int


async def read_file(ctx: ToolContext, a: ReadIn) -> ReadOut:
    root = project_root(ctx)
    p = safe_join(root, a.path)
    if not p.is_file():
        raise ToolError("not_found", f"not a file: {a.path}")
    text, fmt = extract_text(p)
    return ReadOut(path=a.path, format=fmt, text=text, truncated=len(text) >= MAX_READ_BYTES, size=p.stat().st_size)


class ListIn(BaseModel):
    subdir: str = ""
    max_entries: int = Field(default=200, le=1000)


class ListOut(BaseModel):
    files: list[str]
    truncated: bool


def walk_files(root: Path, subdir: str = ""):
    base = safe_join(root, subdir) if subdir else root
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            full = Path(dirpath) / f
            if full.is_symlink():
                continue  # never follow links out of the permitted directory
            rel = full.relative_to(root).as_posix()
            try:
                validate_relative(rel)
            except PathError:
                continue  # protected files (.env, keys) are invisible to agents
            yield rel, full


async def list_files(ctx: ToolContext, a: ListIn) -> ListOut:
    root = project_root(ctx)
    out: list[str] = []
    for rel, _ in walk_files(root, a.subdir):
        out.append(rel)
        if len(out) >= a.max_entries:
            return ListOut(files=out, truncated=True)
    return ListOut(files=out, truncated=False)


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    glob: str = "*"
    regex: bool = False
    max_results: int = Field(default=50, le=200)


class Hit(BaseModel):
    path: str
    line: int
    text: str


class SearchOut(BaseModel):
    hits: list[Hit]
    truncated: bool


async def search_files(ctx: ToolContext, a: SearchIn) -> SearchOut:
    root = project_root(ctx)
    try:
        pat = re.compile(a.query if a.regex else re.escape(a.query), re.IGNORECASE)
    except re.error as e:
        raise ToolError("bad_regex", str(e)) from e
    hits: list[Hit] = []
    for rel, full in walk_files(root):
        if not fnmatch.fnmatch(rel, a.glob) and not fnmatch.fnmatch(Path(rel).name, a.glob):
            continue
        try:
            if full.stat().st_size > MAX_READ_BYTES:
                continue
            data = full.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:2048]:
            continue
        for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if pat.search(line):
                hits.append(Hit(path=rel, line=n, text=line.strip()[:240]))
                if len(hits) >= a.max_results:
                    return SearchOut(hits=hits, truncated=True)
    return SearchOut(hits=hits, truncated=False)


def register(reg) -> None:
    reg.register(Tool("read_file", "Read a text/document file inside the project's permitted directory.", "read", ReadIn, ReadOut, read_file,
                      "Only the project's configured root directory; symlinks, traversal, secrets and key files are refused."))
    reg.register(Tool("list_files", "List files inside the project's permitted directory.", "read", ListIn, ListOut, list_files,
                      "Only the project's configured root directory."))
    reg.register(Tool("search_files", "Search text inside the project's permitted directory.", "read", SearchIn, SearchOut, search_files,
                      "Only the project's configured root directory; binary and oversized files are skipped."))
