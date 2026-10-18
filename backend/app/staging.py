"""Staging area, diffs, versioned proposals, approvals and guarded apply.

Safety properties enforced here (and covered by tests):
  * Agents never write to the project; they only produce staged copies under data/staging.
  * An approval is bound to one proposal's content hash. Any change creates a new version that needs approval again.
  * Applying re-verifies the staged bytes, the approval, and that every target file is unchanged since staging.
  * Originals are backed up before overwrite and nothing is written if any conflict is found.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from . import statemachine as sm
from .config import Config
from .db import Database, new_id, now
from .schemas import ProposedFile
from .security import PathError, safe_join, validate_relative
from .store import Store

MAX_FILES = 30
MAX_FILE_BYTES = 500_000


class StagingError(Exception):
    pass


class ApprovalError(Exception):
    """Raised for stale / invalid approval attempts (HTTP 409)."""


class ApplyConflict(Exception):
    def __init__(self, conflicts: list[str]):
        super().__init__("target files changed since the proposal was staged: " + ", ".join(conflicts))
        self.conflicts = conflicts


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def proposal_hash(files: list[dict]) -> str:
    canon = sorted([(f["path"], f["action"], f.get("base_hash") or "", f["new_hash"]) for f in files])
    return hashlib.sha256(json.dumps(canon).encode()).hexdigest()


def make_diff(path: str, old: str | None, new: str) -> str:
    a = (old or "").splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile="/dev/null" if old is None else f"a/{path}", tofile=f"b/{path}", n=3))


class StagingService:
    def __init__(self, cfg: Config, db: Database, store: Store):
        self.cfg = cfg
        self.db = db
        self.store = store

    def _root(self, project: dict | None) -> Path | None:
        if project and project.get("root_dir"):
            r = Path(project["root_dir"]).expanduser()
            return r.resolve() if r.is_dir() else None
        return None

    # ---- create -----------------------------------------------------------
    def create_proposal(self, task_id: str, project: dict | None, files: list[ProposedFile], title: str, summary: str,
                        author: str, unresolved_issues: int = 0) -> dict:
        if not files:
            raise StagingError("a proposal needs at least one file")
        if len(files) > MAX_FILES:
            raise StagingError(f"too many files in one proposal (max {MAX_FILES})")
        root = self._root(project)
        seen: set[str] = set()
        prepared: list[dict] = []
        for f in files:
            try:
                rel = validate_relative(f.path).as_posix()
            except PathError as e:
                raise StagingError(f"rejected path {f.path!r}: {e}") from e
            if rel.lower() in seen:
                raise StagingError(f"duplicate path in proposal: {rel}")
            seen.add(rel.lower())
            data = f.content.encode("utf-8")
            if len(data) > MAX_FILE_BYTES:
                raise StagingError(f"{rel} exceeds {MAX_FILE_BYTES} bytes")
            base_hash, old_text, action = None, None, "create"
            if root is not None:
                target = safe_join(root, rel)
                if target.exists():
                    if not target.is_file():
                        raise StagingError(f"{rel} exists and is not a regular file")
                    raw = target.read_bytes()
                    base_hash = sha256_bytes(raw)
                    old_text = raw.decode("utf-8", errors="replace")
                    action = "modify"
            prepared.append({"path": rel, "action": action, "base_hash": base_hash, "new_hash": sha256_bytes(data),
                             "data": data, "diff": make_diff(rel, old_text, f.content)})
        chash = proposal_hash(prepared)
        prev = self.db.one("SELECT MAX(version) v FROM proposals WHERE task_id=?", (task_id,))
        version = (prev["v"] or 0) + 1
        pid = new_id("pr")
        with self.db.tx() as c:
            c.execute("UPDATE proposals SET status='superseded' WHERE task_id=? AND status IN ('pending','approved')", (task_id,))
            c.execute("INSERT INTO proposals(id,task_id,version,title,summary,author_agent,status,content_hash,unresolved_issues,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (pid, task_id, version, title, summary, author, "pending", chash, unresolved_issues, now()))
            for f in prepared:
                staged = self.cfg.staging_dir / task_id / pid / f["path"]
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(f["data"])
                c.execute("INSERT INTO proposal_files(proposal_id,path,action,base_hash,new_hash,staged_path,diff) VALUES(?,?,?,?,?,?,?)",
                          (pid, f["path"], f["action"], f["base_hash"], f["new_hash"], str(staged), f["diff"]))
        self.store.log(task_id, "info", f"Staged proposal v{version} with {len(prepared)} file(s)", agent=author)
        return self.store.get_proposal(pid)

    def read_staged(self, proposal_id: str, path: str) -> str:
        p = self.store.get_proposal(proposal_id)
        if not p:
            raise StagingError("unknown proposal")
        for f in p["files"]:
            if f["path"] == path:
                return Path(f["staged_path"]).read_text(encoding="utf-8")
        raise StagingError("file not in proposal")

    def edit_staged_file(self, proposal_id: str, path: str, content: str, project: dict | None) -> dict:
        """A human edits a staged file. This creates a NEW proposal version; earlier approvals no longer apply."""
        p = self.store.get_proposal(proposal_id)
        if not p:
            raise StagingError("unknown proposal")
        if p["status"] not in ("pending", "approved"):
            raise StagingError(f"proposal is {p['status']} and cannot be edited")
        files = []
        for f in p["files"]:
            text = content if f["path"] == path else Path(f["staged_path"]).read_text(encoding="utf-8")
            files.append(ProposedFile(path=f["path"], action=f["action"], content=text))
        if path not in {f["path"] for f in p["files"]}:
            files.append(ProposedFile(path=path, action="create", content=content))
        new = self.create_proposal(p["task_id"], project, files, p["title"], p["summary"], "user", p["unresolved_issues"])
        # a task waiting on the old version goes back to awaiting approval
        t = self.store.get_task(p["task_id"])
        if t and t["status"] == sm.APPROVED:
            sm.transition(self.db, t["id"], sm.AWAITING_APPROVAL, "proposal changed after approval; new approval required")
        return new

    # ---- decisions ----------------------------------------------------------
    def approve(self, proposal_id: str, content_hash: str, note: str = "", acknowledge_issues: bool = False) -> dict:
        p = self.store.get_proposal(proposal_id)
        if not p:
            raise ApprovalError("unknown proposal")
        if p["status"] != "pending":
            raise ApprovalError(f"proposal v{p['version']} is {p['status']}; only the current pending version can be approved")
        if content_hash != p["content_hash"]:
            raise ApprovalError("the proposal changed since you reviewed it; reload and review the new version")
        if p["unresolved_issues"] and not acknowledge_issues:
            raise ApprovalError("this proposal has unresolved review issues or failing checks; approve again with acknowledge_issues=true")
        task = self.store.get_task(p["task_id"])
        if not task or task["status"] != sm.AWAITING_APPROVAL:
            raise ApprovalError(f"task is {task['status'] if task else 'missing'}, not awaiting approval")
        self._verify_staged(p)
        with self.db.tx() as c:
            cur = c.execute("UPDATE proposals SET status='approved', decided_at=?, decision_note=? WHERE id=? AND status='pending'", (now(), note, proposal_id))
            if cur.rowcount != 1:
                raise ApprovalError("proposal was modified concurrently")
            c.execute("INSERT INTO approvals(task_id,proposal_id,decision,content_hash,acknowledged_issues,note,at) VALUES(?,?,?,?,?,?,?)",
                      (p["task_id"], proposal_id, "approved", p["content_hash"], int(acknowledge_issues), note, now()))
        sm.transition(self.db, p["task_id"], sm.APPROVED, f"proposal v{p['version']} approved")
        self.store.log(p["task_id"], "info", f"User approved proposal v{p['version']}")
        return self.store.get_proposal(proposal_id)

    def reject(self, proposal_id: str, note: str = "") -> dict:
        p = self.store.get_proposal(proposal_id)
        if not p:
            raise ApprovalError("unknown proposal")
        if p["status"] != "pending":
            raise ApprovalError(f"proposal is {p['status']}; only a pending proposal can be rejected")
        with self.db.tx() as c:
            c.execute("UPDATE proposals SET status='rejected', decided_at=?, decision_note=? WHERE id=?", (now(), note, proposal_id))
            c.execute("INSERT INTO approvals(task_id,proposal_id,decision,content_hash,note,at) VALUES(?,?,?,?,?,?)",
                      (p["task_id"], proposal_id, "rejected", p["content_hash"], note, now()))
        self.store.log(p["task_id"], "info", f"User rejected proposal v{p['version']}: {note[:200]}")
        return self.store.get_proposal(proposal_id)

    # ---- apply ---------------------------------------------------------------
    def _verify_staged(self, p: dict) -> None:
        files = []
        for f in p["files"]:
            sp = Path(f["staged_path"])
            if not sp.is_file() or sha256_bytes(sp.read_bytes()) != f["new_hash"]:
                raise ApprovalError(f"staged file {f['path']} is missing or was modified outside the app")
            files.append(f)
        if proposal_hash(files) != p["content_hash"]:
            raise ApprovalError("staged content does not match the proposal hash")

    def apply(self, proposal_id: str, project: dict | None) -> list[str]:
        p = self.store.get_proposal(proposal_id)
        if not p:
            raise StagingError("unknown proposal")
        if p["status"] != "approved":
            raise ApprovalError(f"proposal is {p['status']}, not approved")
        approval = self.db.one("SELECT * FROM approvals WHERE proposal_id=? AND decision='approved' ORDER BY id DESC LIMIT 1", (proposal_id,))
        if not approval or approval["content_hash"] != p["content_hash"]:
            raise ApprovalError("no approval matches this exact proposal version")
        self._verify_staged(p)
        root = self._root(project)
        if root is None:
            raise StagingError("the project has no permitted directory, so there is nowhere to apply changes (export the proposal instead)")
        # 1) conflict check for ALL files before writing anything
        conflicts = []
        for f in p["files"]:
            target = safe_join(root, f["path"])
            if f["action"] == "modify":
                if not target.is_file() or sha256_bytes(target.read_bytes()) != f["base_hash"]:
                    conflicts.append(f["path"])
            elif target.exists():
                conflicts.append(f["path"])
        if conflicts:
            raise ApplyConflict(conflicts)
        # 2) backup originals, 3) atomic writes, rollback on failure
        backup_root = self.cfg.backups_dir / p["task_id"] / proposal_id
        written: list[tuple[Path, Path | None]] = []
        try:
            for f in p["files"]:
                target = safe_join(root, f["path"])
                backup = None
                if target.exists():
                    backup = backup_root / f["path"]
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".acc-tmp-")
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(Path(f["staged_path"]).read_bytes())
                    os.replace(tmp, target)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                written.append((target, backup))
        except Exception:
            for target, backup in reversed(written):
                if backup is not None:
                    shutil.copy2(backup, target)
                elif target.exists():
                    target.unlink()
            raise
        self.db.execute("UPDATE proposals SET status='applied', applied_at=? WHERE id=?", (now(), proposal_id))
        self.store.log(p["task_id"], "info", f"Applied proposal v{p['version']} to {root} ({len(p['files'])} file(s)); backups in {backup_root}")
        return [f["path"] for f in p["files"]]

    def revert(self, proposal_id: str, project: dict | None) -> list[str]:
        """Restore originals from backups, but only for files still identical to what we applied."""
        p = self.store.get_proposal(proposal_id)
        root = self._root(project)
        if not p or p["status"] != "applied" or root is None:
            raise StagingError("only an applied proposal in a project with a directory can be reverted")
        backup_root = self.cfg.backups_dir / p["task_id"] / proposal_id
        restored = []
        for f in p["files"]:
            target = safe_join(root, f["path"])
            if not target.is_file() or sha256_bytes(target.read_bytes()) != f["new_hash"]:
                raise ApplyConflict([f["path"]])
        for f in p["files"]:
            target = safe_join(root, f["path"])
            b = backup_root / f["path"]
            if f["action"] == "modify" and b.is_file():
                shutil.copy2(b, target)
            elif f["action"] == "create":
                target.unlink()
            restored.append(f["path"])
        self.db.execute("UPDATE proposals SET status='superseded' WHERE id=?", (proposal_id,))
        self.store.log(p["task_id"], "info", f"Reverted proposal v{p['version']}")
        return restored
