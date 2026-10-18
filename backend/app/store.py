"""Data-access helpers. Plain functions over the Database; JSON columns are decoded here."""
from __future__ import annotations

import json
from typing import Any

from . import statemachine as sm
from .db import Database, jloads, new_id, now
from .security import redact


class Store:
    def __init__(self, db: Database):
        self.db = db

    # ---- projects ------------------------------------------------------
    def create_project(self, name: str, description: str = "", root_dir: str = "", test_command: str = "",
                       instructions: str = "") -> dict:
        pid = new_id("p")
        self.db.execute("INSERT INTO projects(id,name,description,root_dir,test_command,instructions,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (pid, name, description, root_dir, test_command, instructions, now(), now()))
        return self.get_project(pid)

    def get_project(self, pid: str | None) -> dict | None:
        return self.db.one("SELECT * FROM projects WHERE id=?", (pid,)) if pid else None

    def list_projects(self) -> list[dict]:
        return self.db.all("SELECT * FROM projects ORDER BY created_at")

    def update_project(self, pid: str, **fields: Any) -> dict:
        allowed = {"name", "description", "root_dir", "test_command", "instructions"}
        fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE projects SET {sets}, updated_at=? WHERE id=?", [*fields.values(), now(), pid])
        return self.get_project(pid)

    def delete_project(self, pid: str) -> None:
        if self.db.one("SELECT 1 x FROM tasks WHERE project_id=?", (pid,)):
            raise ValueError("project has tasks; delete or keep it (tasks keep their history)")
        self.db.execute("DELETE FROM memory_items WHERE project_id=?", (pid,))
        self.db.execute("DELETE FROM projects WHERE id=?", (pid,))

    # ---- tasks -----------------------------------------------------------
    def create_task(self, request: str, project_id: str | None, routing_mode: str, manual_routes: dict, title: str | None = None) -> dict:
        tid = new_id("t")
        title = (title or request.strip().split("\n")[0])[:120]
        with self.db.tx() as c:
            c.execute("INSERT INTO tasks(id,project_id,title,request,status,routing_mode,manual_routes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (tid, project_id, title, request, sm.PENDING, routing_mode, json.dumps(manual_routes), now(), now()))
            c.execute("INSERT INTO state_events(task_id,from_state,to_state,reason,at) VALUES(?,?,?,?,?)", (tid, None, sm.PENDING, "created", now()))
        return self.get_task(tid)

    @staticmethod
    def _decode_task(t: dict) -> dict:
        t["plan"] = jloads(t["plan"], {})
        t["report"] = jloads(t["report"], {})
        t["manual_routes"] = jloads(t["manual_routes"], {})
        t["paused"] = bool(t["paused"])
        t["cancel_requested"] = bool(t["cancel_requested"])
        return t

    def get_task(self, tid: str) -> dict | None:
        t = self.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
        return self._decode_task(t) if t else None

    def list_tasks(self, status: list[str] | None = None, limit: int = 200) -> list[dict]:
        if status:
            q = ",".join("?" * len(status))
            rows = self.db.all(f"SELECT * FROM tasks WHERE status IN ({q}) ORDER BY created_at DESC LIMIT ?", [*status, limit])
        else:
            rows = self.db.all("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._decode_task(r) for r in rows]

    def set_task(self, tid: str, **fields: Any) -> None:
        """Update non-status columns. Status only changes through statemachine.transition."""
        if "status" in fields:
            raise ValueError("use statemachine.transition for status")
        enc = {k: (json.dumps(v) if k in ("plan", "report", "manual_routes") else (int(v) if isinstance(v, bool) else v)) for k, v in fields.items()}
        sets = ", ".join(f"{k}=?" for k in enc)
        self.db.execute(f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?", [*enc.values(), now(), tid])

    def state_events(self, tid: str) -> list[dict]:
        return self.db.all("SELECT * FROM state_events WHERE task_id=? ORDER BY id", (tid,))

    # ---- subtasks --------------------------------------------------------
    @staticmethod
    def _decode_sub(s: dict) -> dict:
        s["acceptance_criteria"] = jloads(s["acceptance_criteria"], [])
        s["depends_on"] = jloads(s["depends_on"], [])
        s["output"] = jloads(s["output"], {})
        return s

    def add_subtask(self, task_id: str, idx: int, title: str, kind: str, objective: str, criteria: list[str],
                    depends_on: list[str], agent: str, routing_note: str = "", status: str = "ready") -> dict:
        sid = new_id("s")
        self.db.execute("INSERT INTO subtasks(id,task_id,idx,title,kind,objective,acceptance_criteria,depends_on,agent,routing_note,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, task_id, idx, title, kind, objective, json.dumps(criteria), json.dumps(depends_on), agent, routing_note, status))
        return self.get_subtask(sid)

    def get_subtask(self, sid: str) -> dict | None:
        s = self.db.one("SELECT * FROM subtasks WHERE id=?", (sid,))
        return self._decode_sub(s) if s else None

    def subtasks(self, task_id: str) -> list[dict]:
        return [self._decode_sub(s) for s in self.db.all("SELECT * FROM subtasks WHERE task_id=? ORDER BY idx", (task_id,))]

    def claim_subtask(self, sid: str, token: str) -> bool:
        """Atomically claim a subtask for execution. Only one caller can win; this prevents duplicate runs."""
        cur = self.db.execute("UPDATE subtasks SET status='running', claim_token=?, attempts=attempts+1, started_at=?, error='' WHERE id=? AND status='ready'",
                              (token, now(), sid))
        return cur.rowcount == 1

    def finish_subtask(self, sid: str, token: str, output: dict) -> bool:
        cur = self.db.execute("UPDATE subtasks SET status='done', output=?, finished_at=?, error='' WHERE id=? AND claim_token=? AND status='running'",
                              (json.dumps(output), now(), sid, token))
        return cur.rowcount == 1

    def fail_subtask(self, sid: str, token: str, error: str, output: dict | None = None) -> bool:
        cur = self.db.execute("UPDATE subtasks SET status='failed', error=?, output=COALESCE(?, output), finished_at=? WHERE id=? AND claim_token=? AND status='running'",
                              (redact(error), json.dumps(output) if output is not None else None, now(), sid, token))
        return cur.rowcount == 1

    def set_subtask(self, sid: str, **fields: Any) -> None:
        enc = {k: (json.dumps(v) if k in ("output", "depends_on", "acceptance_criteria") else v) for k, v in fields.items()}
        sets = ", ".join(f"{k}=?" for k in enc)
        self.db.execute(f"UPDATE subtasks SET {sets} WHERE id=?", [*enc.values(), sid])

    # ---- logs ------------------------------------------------------------
    def log(self, task_id: str | None, level: str, message: str, agent: str = "", subtask_id: str | None = None, data: dict | None = None) -> None:
        self.db.execute("INSERT INTO logs(task_id,subtask_id,level,agent,message,data,at) VALUES(?,?,?,?,?,?,?)",
                        (task_id, subtask_id, level, agent, redact(message)[:4000], redact(json.dumps(data or {}, default=str))[:8000], now()))

    def logs(self, task_id: str, limit: int = 500) -> list[dict]:
        rows = self.db.all("SELECT * FROM logs WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit))
        for r in rows:
            r["data"] = jloads(r["data"], {})
        return list(reversed(rows))

    def recent_errors(self, limit: int = 20) -> list[dict]:
        rows = self.db.all("SELECT * FROM logs WHERE level='error' ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["data"] = jloads(r["data"], {})
        return rows

    # ---- calls -----------------------------------------------------------
    def record_call(self, *, task_id: str | None, subtask_id: str | None, provider: str, model: str, purpose: str, status: str,
                    error_kind: str = "", error: str = "", input_tokens: int | None = None, output_tokens: int | None = None,
                    cost_usd: float | None = None, cost_basis: str = "none", billable: bool = False, latency_ms: int | None = None,
                    meta: dict | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO calls(task_id,subtask_id,provider,model,purpose,status,error_kind,error,input_tokens,output_tokens,cost_usd,cost_basis,billable,latency_ms,meta,at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, subtask_id, provider, model, purpose, status, error_kind, redact(error)[:1000], input_tokens, output_tokens, cost_usd,
             cost_basis, int(billable), latency_ms, json.dumps(meta or {}, default=str), now()))
        return cur.lastrowid

    def calls(self, task_id: str | None = None, provider: str | None = None, limit: int = 200) -> list[dict]:
        where, params = [], []
        if task_id:
            where.append("task_id=?"); params.append(task_id)
        if provider:
            where.append("provider=?"); params.append(provider)
        q = "SELECT * FROM calls" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        rows = self.db.all(q, [*params, limit])
        for r in rows:
            r["meta"] = jloads(r["meta"], {})
            r["billable"] = bool(r["billable"])
        return rows

    def task_usage(self, task_id: str) -> dict:
        r = self.db.one("SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, COALESCE(SUM(CASE WHEN billable=1 THEN cost_usd END),0) cost, COALESCE(SUM(billable),0) billable_calls, COALESCE(SUM(cost_basis='local'),0) local_calls FROM calls WHERE task_id=? AND purpose!='ping'", (task_id,))
        by = self.db.all("SELECT provider, COUNT(*) calls, COALESCE(SUM(CASE WHEN billable=1 THEN cost_usd END),0) cost FROM calls WHERE task_id=? AND purpose!='ping' GROUP BY provider", (task_id,))
        return {"calls": r["calls"], "input_tokens": r["i"], "output_tokens": r["o"], "cost_usd": round(r["cost"], 6),
                "billable_calls": r["billable_calls"], "local_calls": r["local_calls"],
                "demo": r["calls"] > 0 and r["billable_calls"] == 0 and r["local_calls"] == 0, "by_provider": by}

    # ---- reviews / tests / sources / claims -----------------------------
    def add_review(self, task_id: str, subtask_id: str | None, reviewer: str, author: str, round_: int, verdict: str, issues: list, summary: str) -> None:
        self.db.execute("INSERT INTO reviews(task_id,subtask_id,reviewer,author,round,verdict,issues,summary,at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (task_id, subtask_id, reviewer, author, round_, verdict, json.dumps(issues), summary, now()))

    def reviews(self, task_id: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM reviews WHERE task_id=? ORDER BY id", (task_id,))
        for r in rows:
            r["issues"] = jloads(r["issues"], [])
        return rows

    def add_test_run(self, task_id: str, proposal_id: str | None, command: str, status: str, exit_code: int | None, output: str, duration_ms: int | None) -> None:
        self.db.execute("INSERT INTO test_runs(task_id,proposal_id,command,status,exit_code,output,duration_ms,at) VALUES(?,?,?,?,?,?,?,?)",
                        (task_id, proposal_id, command, status, exit_code, redact(output)[-8000:], duration_ms, now()))

    def test_runs(self, task_id: str) -> list[dict]:
        return self.db.all("SELECT * FROM test_runs WHERE task_id=? ORDER BY id", (task_id,))

    def add_source(self, task_id: str, subtask_id: str | None, question: str, s: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO sources(task_id,subtask_id,question,title,url,domain,published,last_updated,retrieved_at,excerpt,origin,check_status,check_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, subtask_id, question, s.get("title", ""), s["url"], s.get("domain", ""), s.get("published"), s.get("last_updated"),
             s.get("retrieved_at") or now(), s.get("excerpt", "")[:1500], s.get("origin", "provider"), s.get("check_status", "unchecked"), s.get("check_note", "")))
        return cur.lastrowid

    def sources(self, task_id: str) -> list[dict]:
        return self.db.all("SELECT * FROM sources WHERE task_id=? ORDER BY id", (task_id,))

    def add_claim(self, task_id: str, subtask_id: str | None, text: str, status: str, support: str, urls: list[str], note: str = "") -> int:
        cur = self.db.execute("INSERT INTO claims(task_id,subtask_id,text,status,support,source_urls,note,checked_at) VALUES(?,?,?,?,?,?,?,?)",
                              (task_id, subtask_id, text, status, support, json.dumps(urls), note, now()))
        return cur.lastrowid

    def claims(self, task_id: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM claims WHERE task_id=? ORDER BY id", (task_id,))
        for r in rows:
            r["source_urls"] = jloads(r["source_urls"], [])
        return rows

    def set_claim_status(self, claim_id: int, status: str, note: str = "") -> None:
        self.db.execute("UPDATE claims SET status=?, note=?, checked_at=? WHERE id=?", (status, note, now(), claim_id))

    # ---- proposals ---------------------------------------------------------
    def proposals(self, task_id: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM proposals WHERE task_id=? ORDER BY version", (task_id,))
        for r in rows:
            r["files"] = self.db.all("SELECT * FROM proposal_files WHERE proposal_id=? ORDER BY id", (r["id"],))
        return rows

    def get_proposal(self, pid: str) -> dict | None:
        r = self.db.one("SELECT * FROM proposals WHERE id=?", (pid,))
        if r:
            r["files"] = self.db.all("SELECT * FROM proposal_files WHERE proposal_id=? ORDER BY id", (pid,))
        return r

    def approvals(self, task_id: str) -> list[dict]:
        return self.db.all("SELECT * FROM approvals WHERE task_id=? ORDER BY id", (task_id,))
