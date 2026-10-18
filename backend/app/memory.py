"""Inspectable project memory. Nothing a model says becomes confirmed memory unless the user confirms it."""
from __future__ import annotations

import json
import re

from .db import Database, new_id, now
from .store import Store

KINDS = ("instruction", "preference", "decision", "finding", "limitation", "file_ref", "outstanding", "config")
STATUSES = ("confirmed", "hypothesis", "suggestion")
_WORD = re.compile(r"[a-z0-9]{3,}")


def _terms(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


class MemoryService:
    def __init__(self, db: Database, store: Store):
        self.db = db
        self.store = store

    def add(self, project_id: str | None, kind: str, title: str, body: str, status: str = "hypothesis", source: str = "user") -> dict:
        if kind not in KINDS or status not in STATUSES:
            raise ValueError("invalid kind or status")
        mid = new_id("m")
        self.db.execute("INSERT INTO memory_items(id,project_id,kind,status,title,body,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (mid, project_id, kind, status, title.strip()[:200], body.strip()[:8000], source, now(), now()))
        return self.get(mid)

    def get(self, mid: str) -> dict | None:
        return self.db.one("SELECT * FROM memory_items WHERE id=?", (mid,))

    def list(self, project_id: str | None = None, status: str | None = None) -> list[dict]:
        where, params = [], []
        if project_id:
            where.append("project_id=?"); params.append(project_id)
        if status:
            where.append("status=?"); params.append(status)
        return self.db.all("SELECT * FROM memory_items" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY updated_at DESC", params)

    def update(self, mid: str, **fields) -> dict:
        allowed = {k: v for k, v in fields.items() if k in ("kind", "status", "title", "body") and v is not None}
        if "kind" in allowed and allowed["kind"] not in KINDS or "status" in allowed and allowed["status"] not in STATUSES:
            raise ValueError("invalid kind or status")
        if allowed:
            sets = ", ".join(f"{k}=?" for k in allowed)
            self.db.execute(f"UPDATE memory_items SET {sets}, updated_at=? WHERE id=?", [*allowed.values(), now(), mid])
        return self.get(mid)

    def delete(self, mid: str) -> None:
        self.db.execute("DELETE FROM memory_items WHERE id=?", (mid,))

    def export(self, project_id: str | None = None) -> dict:
        project = self.store.get_project(project_id) if project_id else None
        return {"exported_at": now(), "project": project, "memory": self.list(project_id)}

    def export_markdown(self, project_id: str) -> str:
        """A human-readable PROJECT.md: standing instructions plus memory grouped by status. Unconfirmed items are labelled as such."""
        project = self.store.get_project(project_id)
        items = self.list(project_id)
        lines = [f"# PROJECT.md: {project['name']}", ""]
        if project.get("description"):
            lines += [project["description"], ""]
        lines += ["## Standing instructions", "", project.get("instructions") or "_none_", ""]
        for status, heading in (("confirmed", "Confirmed memory"), ("hypothesis", "Unconfirmed: hypotheses (verify before relying)"),
                                ("suggestion", "Unconfirmed: suggestions")):
            group = [i for i in items if i["status"] == status]
            lines += [f"## {heading}", ""]
            lines += [f"- **[{i['kind']}] {i['title']}**: {i['body']}" for i in group] or ["_none_"]
            lines.append("")
        return "\n".join(lines)

    def retrieve(self, project_id: str | None, query: str, k: int = 5, include_unconfirmed: int = 2) -> dict:
        """Targeted retrieval: top-k confirmed items by keyword overlap, plus a few clearly-labelled unconfirmed ones."""
        q = _terms(query)
        items = self.list(project_id) if project_id else []
        scored = []
        for it in items:
            overlap = len(q & _terms(it["title"] + " " + it["body"]))
            boost = 1 if it["kind"] in ("instruction", "preference", "decision") else 0  # standing rules always get a little weight
            if overlap or boost:
                scored.append((overlap * 2 + boost, it))
        scored.sort(key=lambda x: -x[0])
        confirmed = [i for s, i in scored if i["status"] == "confirmed"][:k]
        unconfirmed = [i for s, i in scored if i["status"] != "confirmed" and s >= 2][:include_unconfirmed]
        return {"confirmed": confirmed, "unconfirmed": unconfirmed}

    @staticmethod
    def render_context(project: dict | None, retrieved: dict, max_chars: int = 3000) -> str:
        parts: list[str] = []
        if project and project.get("instructions"):
            parts.append("PROJECT INSTRUCTIONS (user-authored, trusted):\n" + project["instructions"][:1500])
        if retrieved["confirmed"]:
            parts.append("CONFIRMED PROJECT MEMORY (user-confirmed facts and decisions):\n" +
                         "\n".join(f"- [{i['kind']}] {i['title']}: {i['body'][:300]}" for i in retrieved["confirmed"]))
        if retrieved["unconfirmed"]:
            parts.append("UNCONFIRMED NOTES (hypotheses/suggestions: NOT facts, verify before relying on them):\n" +
                         "\n".join(f"- [{i['status']}] {i['title']}: {i['body'][:200]}" for i in retrieved["unconfirmed"]))
        return "\n\n".join(parts)[:max_chars]
