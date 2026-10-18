"""Run the project's user-configured test command in an isolated copy of the project with a proposal overlaid.

Only the command stored in the project settings by the user can run; model output can never supply a command.
This is a working-directory and environment isolation, NOT a security sandbox: the process still runs as the
current OS user. Documented in the README.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import sys
import time
from pathlib import Path

from ..config import Config
from ..security import redact
from .files import SKIP_DIRS
from .base import ToolError

MAX_COPY_BYTES = 50_000_000
MAX_COPY_FILES = 5000
OUTPUT_TAIL = 8000


def parse_command(command: str, allowed: list[str]) -> list[str]:
    try:
        argv = shlex.split(command, posix=(os.name != "nt"))
    except ValueError as e:
        raise ToolError("bad_command", f"cannot parse test command: {e}") from e
    if not argv:
        raise ToolError("bad_command", "empty test command")
    exe = Path(argv[0]).name.lower()
    for suf in (".exe", ".cmd", ".bat"):
        if exe.endswith(suf):
            exe = exe[: -len(suf)]
    if Path(argv[0]).name != argv[0]:
        raise ToolError("command_not_allowed", "the test executable must be a bare name on PATH (no directories)")
    if exe not in allowed:
        raise ToolError("command_not_allowed", f"'{exe}' is not in the allowed test executables: {', '.join(allowed)}")
    if exe in ("python", "python3", "py"):
        argv[0] = sys.executable  # same interpreter as the app: works on Windows where `python` may not be on PATH
    else:
        resolved = shutil.which(argv[0])
        if not resolved:
            raise ToolError("command_not_found", f"'{argv[0]}' was not found on PATH")
        argv[0] = resolved
    return argv


def build_sandbox(cfg: Config, task_id: str, run_id: str, project_root: Path | None, staged_files: list[tuple[str, Path]]) -> Path:
    sandbox = cfg.sandbox_dir / task_id / run_id
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True)
    if project_root is not None:
        total = count = 0
        for dirpath, dirnames, filenames in os.walk(project_root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in filenames:
                src = Path(dirpath) / f
                if src.is_symlink() or f.lower().startswith(".env"):
                    continue
                total += src.stat().st_size
                count += 1
                if total > MAX_COPY_BYTES or count > MAX_COPY_FILES:
                    raise ToolError("project_too_large", "project is too large to copy into the test sandbox (50 MB / 5000 files)")
                dst = sandbox / src.relative_to(project_root)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    for rel, staged in staged_files:
        dst = sandbox / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged, dst)
    return sandbox


def scrubbed_env(sandbox: Path) -> dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL", "WINDIR", "USERPROFILE_NOT_COPIED")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["HOME"] = str(sandbox)
    env["USERPROFILE"] = str(sandbox)
    env["CI"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env  # no API keys or other secrets are passed through


async def run_command(argv: list[str], cwd: Path, timeout_s: float) -> tuple[str, int | None, str, int]:
    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(*argv, cwd=str(cwd), env=scrubbed_env(cwd), stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        status = "passed" if proc.returncode == 0 else "failed"
        code = proc.returncode
    except asyncio.TimeoutError:
        proc.kill()
        out, _ = await proc.communicate()
        status, code = "timeout", None
    except asyncio.CancelledError:
        proc.kill()
        raise
    text = redact(out.decode("utf-8", errors="replace"))
    return status, code, text[-OUTPUT_TAIL:], int((time.monotonic() - t0) * 1000)
