#!/usr/bin/env python3
"""Cross-platform helper: setup | test | start | stop | status.

  python scripts/acc.py setup    create backend/.venv, install Python + npm dependencies, build the UI
  python scripts/acc.py test     run backend (pytest) and frontend (vitest + typecheck) tests; never calls paid APIs
  python scripts/acc.py start    build the UI if needed and run the server in the foreground (Ctrl+C stops it)
  python scripts/acc.py stop     stop a server started with `start` (from another terminal)
  python scripts/acc.py status   show whether the server answers on its port

Uses only the standard library so it runs before the project's dependencies are installed.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
IS_WIN = os.name == "nt"
VENV_PY = BACKEND / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")
DATA = Path(os.environ.get("ACC_DATA_DIR") or ROOT / "data")
PIDFILE = DATA / "server.pid"
PORT = int(os.environ.get("ACC_PORT", "8787") or "8787")


def run(cmd: list[str], cwd: Path, env: dict | None = None) -> int:
    print(f"\n> {' '.join(cmd)}   (in {cwd})", flush=True)
    return subprocess.call(cmd, cwd=cwd, env=env)


def npm() -> str:
    exe = shutil.which("npm.cmd" if IS_WIN else "npm") or shutil.which("npm")
    if not exe:
        sys.exit("npm was not found. Install Node.js 20 or newer from https://nodejs.org and open a new terminal.")
    return exe


def need_venv() -> None:
    if not VENV_PY.exists():
        sys.exit("Backend environment not found. Run:  python scripts/acc.py setup")


def setup() -> int:
    if sys.version_info < (3, 11):
        sys.exit(f"Python 3.11 or newer is required (this is {sys.version.split()[0]}).")
    if not VENV_PY.exists():
        if run([sys.executable, "-m", "venv", str(BACKEND / ".venv")], ROOT):
            return 1
    for cmd, cwd in (([str(VENV_PY), "-m", "pip", "install", "--upgrade", "pip"], BACKEND),
                     ([str(VENV_PY), "-m", "pip", "install", "-r", "requirements-dev.txt"], BACKEND),
                     ([npm(), "install"], FRONTEND),
                     ([npm(), "run", "build"], FRONTEND)):
        if run(cmd, cwd):
            print("\nSetup step failed. See the README troubleshooting section.")
            return 1
    env = ROOT / ".env"
    if not env.exists():
        shutil.copyfile(ROOT / ".env.example", env)
        print("\nCreated .env from .env.example (demo mode, no keys needed).")
    print("\nSetup complete. Next:  python scripts/acc.py test   then   python scripts/acc.py start")
    return 0


def test() -> int:
    need_venv()
    # tests never read .env and construct their own demo/fake providers; these blanks are belt and braces
    env = {**os.environ, "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "PERPLEXITY_API_KEY": "", "ACC_MODE": "demo"}
    rc = run([str(VENV_PY), "-m", "pytest", "-q"], BACKEND, env)
    rc2 = run([npm(), "run", "typecheck"], FRONTEND)
    rc3 = run([npm(), "test"], FRONTEND)
    print("\nBackend:", "PASSED" if rc == 0 else "FAILED", "| Frontend typecheck:", "PASSED" if rc2 == 0 else "FAILED",
          "| Frontend tests:", "PASSED" if rc3 == 0 else "FAILED")
    return rc or rc2 or rc3


def start() -> int:
    need_venv()
    if not (FRONTEND / "dist" / "index.html").exists():
        if run([npm(), "run", "build"], FRONTEND):
            return 1
    if PIDFILE.exists():
        print(f"{PIDFILE} exists; if the server is not running, delete that file. Checking port first.")
        if status(quiet=True):
            print(f"Already running at http://localhost:{PORT}")
            return 0
    DATA.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([str(VENV_PY), "-m", "app"], cwd=BACKEND)
    PIDFILE.write_text(str(proc.pid))
    print(f"Server pid {proc.pid}. Open http://localhost:{PORT}  (Ctrl+C here, or `python scripts/acc.py stop`, to stop)")
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 0
    finally:
        PIDFILE.unlink(missing_ok=True)


def stop() -> int:
    if not PIDFILE.exists():
        print("No server.pid found: nothing started by `start` is running. (If it was started another way, press Ctrl+C in its terminal.)")
        return 0
    pid = int(PIDFILE.read_text().strip())
    try:
        if IS_WIN:
            subprocess.call(["taskkill", "/PID", str(pid), "/T", "/F"])
        else:
            os.kill(pid, signal.SIGTERM)
        print(f"Stopped server process {pid}.")
    except ProcessLookupError:
        print(f"Process {pid} was not running.")
    PIDFILE.unlink(missing_ok=True)
    return 0


def status(quiet: bool = False) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2) as r:
            info = json.load(r)
        if not quiet:
            print(f"Running: mode={info.get('mode')} version={info.get('version')} on http://localhost:{PORT}")
        return True
    except Exception:
        if not quiet:
            print(f"Nothing is answering on port {PORT}.")
        return False


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    table = {"setup": setup, "test": test, "start": start, "stop": stop, "status": lambda: 0 if status() else 1}
    if cmd not in table:
        print(__doc__)
        return 2
    return table[cmd]()


if __name__ == "__main__":
    sys.exit(main())
