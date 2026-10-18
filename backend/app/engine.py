"""Coordinator + execution engine.

The coordinator is deterministic code (routing, bookkeeping, limits, state transitions). Models are asked only
for plans, research, analysis, implementation and review; their claims of success are never trusted on their own:
files are staged and diffed, tests are run by code, and reviews must cite evidence.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from . import statemachine as sm
from .budget import BudgetExceeded
from .config import Config
from .db import Database
from .gateway import Gateway
from .memory import MemoryService
from .planning import heuristic_plan, is_high_impact
from .prompts import SYSTEM, assignment, compact, untrusted
from .providers.base import AgentRequest, ProviderError, ResearchOptions, Source
from .registry import ProviderRegistry
from .reports import build_report
from .research import ResearchService
from .router import RoutingError, route, route_review
from .schemas import (AnalysisOutput, EvidenceEvaluation, ImplementationOutput, Plan, ProposedFile, ResearchReport,
                      ReviewOutput, SynthesisOutput)
from .settings_service import SettingsService
from .staging import ApplyConflict, ApprovalError, StagingError, StagingService
from .store import Store
from .tools.base import ToolContext, ToolRegistry
from .tools.files import project_root
from .tools.testrunner import build_sandbox, parse_command, run_command

REVIEWABLE = ("implementation", "document", "synthesis")


class TaskAbort(Exception):
    """Stop driving the task for now (paused / finished). Not an error."""


class Engine:
    def __init__(self, cfg: Config, db: Database, store: Store, settings: SettingsService, registry: ProviderRegistry,
                 gateway: Gateway, staging: StagingService, memory: MemoryService, research: ResearchService, tools: ToolRegistry):
        self.cfg, self.db, self.store, self.settings = cfg, db, store, settings
        self.registry, self.gateway, self.staging, self.memory = registry, gateway, staging, memory
        self.research, self.tools = research, tools
        self.instance_id = uuid.uuid4().hex[:10]
        self._runners: dict[str, asyncio.Task] = {}
        self._apply_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ lifecycle
    def is_running(self, task_id: str) -> bool:
        t = self._runners.get(task_id)
        return t is not None and not t.done()

    def start(self, task_id: str) -> bool:
        """Start (or continue) driving a task. Returns False if a runner already exists: no duplicate execution."""
        if self.is_running(task_id):
            return False
        self._runners[task_id] = asyncio.create_task(self._drive(task_id), name=f"task-{task_id}")
        return True

    async def wait(self, task_id: str) -> None:
        t = self._runners.get(task_id)
        if t:
            await asyncio.shield(asyncio.gather(t, return_exceptions=True))

    async def shutdown(self) -> None:
        """Stop runners without touching task state: interrupted tasks are recovered on the next start."""
        for t in list(self._runners.values()):
            t.cancel()
        if self._runners:
            await asyncio.gather(*self._runners.values(), return_exceptions=True)
        self._runners.clear()

    def recover(self) -> list[str]:
        """Run once at startup. Re-queues work that was interrupted by a crash/restart without re-running finished work."""
        recovered = []
        rows = self.db.all("SELECT id, status FROM tasks WHERE status IN ('pending','planning','ready','running','reviewing','repairing','applying_changes','approved')")
        for r in rows:
            tid, st = r["id"], r["status"]
            if st == sm.APPLYING:
                props = [p for p in self.store.proposals(tid) if p["status"] == "applied"]
                if props:
                    sm.transition(self.db, tid, sm.COMPLETED, "recovered: proposal had been applied before the interruption", outcome="success")
                else:
                    sm.transition(self.db, tid, sm.FAILED, "interrupted while applying changes; files may be partially written, check the project and backups",
                                  outcome="failed", error="Interrupted while applying changes. Inspect the project directory and the backup folder before retrying.")
                self.store.log(tid, "warn", "Recovered task interrupted during apply")
                recovered.append(tid)
                continue
            n = self.db.execute("UPDATE subtasks SET status='ready', claim_token='' WHERE task_id=? AND status='running'", (tid,)).rowcount
            self.store.set_task(tid, owner_instance="")
            if n:
                self.store.log(tid, "warn", f"Recovered after interruption: {n} subtask(s) that were running will be re-run (finished work is kept)")
            if st != sm.APPROVED:
                # Conservative: do not start spending money silently after a restart. The user resumes explicitly.
                self.store.set_task(tid, paused=True, pause_reason="interrupted: the app stopped while this task was running; press Resume to continue")
            else:
                self.start(tid)  # user already approved this exact version; applying is verified and local
            recovered.append(tid)
        return recovered

    # ------------------------------------------------------------------ user controls
    def pause(self, task_id: str, reason: str = "paused by user") -> None:
        self.store.set_task(task_id, paused=True, pause_reason=reason)
        self.store.log(task_id, "info", f"Paused: {reason}")

    def resume(self, task_id: str, clarification: str = "") -> None:
        t = self.store.get_task(task_id)
        if clarification.strip():
            self.store.set_task(task_id, request=t["request"] + "\n\nUser clarification: " + clarification.strip())
            if t["status"] == sm.PLANNING:
                self.store.set_task(task_id, plan={})
        self.store.set_task(task_id, paused=False, pause_reason="")
        self.store.log(task_id, "info", "Resumed")
        self.start(task_id)

    def retry(self, task_id: str) -> None:
        t = self.store.get_task(task_id)
        if t["status"] != sm.FAILED:
            raise sm.IllegalTransition(t["status"], sm.READY)
        n = self.db.execute("UPDATE subtasks SET status='ready', error='', claim_token='' WHERE task_id=? AND status IN ('failed','skipped')", (task_id,)).rowcount
        has_plan = bool(self.store.subtasks(task_id))
        sm.transition(self.db, task_id, sm.READY if has_plan else sm.PLANNING, f"retry requested ({n} subtask(s) reset)", error="", outcome="", paused=0, pause_reason="")
        self.start(task_id)

    async def cancel(self, task_id: str) -> None:
        t = self.store.get_task(task_id)
        if t["status"] in (sm.COMPLETED, sm.CANCELLED):
            return
        self.store.set_task(task_id, cancel_requested=True)
        runner = self._runners.get(task_id)
        if runner and not runner.done():
            runner.cancel()  # propagates into the in-flight provider request
            await asyncio.gather(runner, return_exceptions=True)
        t = self.store.get_task(task_id)
        if t["status"] not in (sm.CANCELLED, sm.COMPLETED):
            try:
                sm.transition(self.db, task_id, sm.CANCELLED, "cancelled by user", outcome="cancelled", paused=0, pause_reason="")
            except sm.IllegalTransition:
                pass
        self.db.execute("UPDATE subtasks SET status='skipped', error='task cancelled' WHERE task_id=? AND status IN ('ready','running')", (task_id,))
        for p in self.store.proposals(task_id):
            if p["status"] in ("pending", "approved"):
                self.db.execute("UPDATE proposals SET status='superseded' WHERE id=?", (p["id"],))
        self._finalize_report(task_id)
        self.store.log(task_id, "info", "Task cancelled")

    # ------------------------------------------------------------------ approvals (called by the API)
    async def approve(self, proposal_id: str, content_hash: str, note: str = "", acknowledge_issues: bool = False) -> dict:
        p = self.store.get_proposal(proposal_id)
        if not p:
            raise ApprovalError("unknown proposal")
        lock = self._apply_locks.setdefault(p["task_id"], asyncio.Lock())
        async with lock:  # two clicks cannot apply twice
            self.staging.approve(proposal_id, content_hash, note, acknowledge_issues)
            await self._apply_step(self.store.get_task(p["task_id"]))
        return self.store.get_proposal(proposal_id)

    def reject(self, proposal_id: str, note: str = "", request_revision: bool = True) -> dict:
        p = self.store.get_proposal(proposal_id)
        if not p:
            raise ApprovalError("unknown proposal")
        task = self.store.get_task(p["task_id"])
        if task["status"] != sm.AWAITING_APPROVAL:
            raise ApprovalError(f"task is {task['status']}, not awaiting approval")
        out = self.staging.reject(proposal_id, note)
        if request_revision:
            sm.transition(self.db, task["id"], sm.REPAIRING, "revision requested by user", revision_feedback=note or "The user rejected this proposal without comment; reconsider and improve it.",
                          revision_rounds=task["revision_rounds"] + 1)
            self.start(task["id"])
        else:
            sm.transition(self.db, task["id"], sm.CANCELLED, "proposal rejected by user", outcome="cancelled")
            self._finalize_report(task["id"])
        return out

    # ------------------------------------------------------------------ driver
    async def _drive(self, task_id: str) -> None:
        self.store.set_task(task_id, owner_instance=self.instance_id)
        timeout = float(self.settings.get_key("limits")["task_timeout_s"])
        try:
            await asyncio.wait_for(self._loop(task_id), timeout=timeout)
        except TaskAbort:
            pass
        except asyncio.TimeoutError:
            self._fail(task_id, f"Task exceeded its {int(timeout)}s time limit. Finished work is preserved; Retry continues.")
        except asyncio.CancelledError:
            t = self.store.get_task(task_id)
            if t and t["cancel_requested"]:
                return
            raise  # app shutdown: leave state as-is, recover() handles it
        except Exception as e:  # a bug must never leave a task silently "running"
            self.store.log(task_id, "error", f"Internal error: {type(e).__name__}: {e}")
            self._fail(task_id, f"Internal error: {type(e).__name__}: {e}")
        finally:
            if self._runners.get(task_id) is asyncio.current_task():
                self._runners.pop(task_id, None)

    async def _loop(self, task_id: str) -> None:
        handlers = {sm.PENDING: self._plan_step, sm.PLANNING: self._plan_step, sm.READY: self._to_running, sm.RUNNING: self._execute_step,
                    sm.REVIEWING: self._review_step, sm.REPAIRING: self._repair_step, sm.APPROVED: self._apply_wrapper, sm.APPLYING: self._apply_wrapper}
        while True:
            t = self.store.get_task(task_id)
            if t["status"] in (sm.AWAITING_APPROVAL, sm.COMPLETED, sm.CANCELLED, sm.FAILED):
                return
            if t["cancel_requested"] or t["paused"]:
                return
            await handlers[t["status"]](t)

    def _fail(self, task_id: str, message: str) -> None:
        t = self.store.get_task(task_id)
        if not t or t["status"] in (sm.COMPLETED, sm.CANCELLED, sm.FAILED):
            return
        self.db.execute("UPDATE subtasks SET status='failed', error='interrupted because the task failed', claim_token='' WHERE task_id=? AND status='running'", (task_id,))
        subs = self.store.subtasks(task_id)
        outcome = "partial" if any(s["status"] == "done" for s in subs) else "failed"
        try:
            sm.transition(self.db, task_id, sm.FAILED, message[:300], error=message, outcome=outcome)
        except sm.IllegalTransition:
            return
        self.store.log(task_id, "error", message)
        self._finalize_report(task_id)

    def _finalize_report(self, task_id: str) -> None:
        self.store.set_task(task_id, report=build_report(self.store, task_id))

    def _pause_for_budget(self, task_id: str, e: BudgetExceeded) -> None:
        self.store.set_task(task_id, paused=True, pause_reason=f"budget: {e.message} Raise the limit in Settings, then Resume.")
        self.store.log(task_id, "error", f"Stopped by spending/usage limit ({e.kind}): {e.message}")
        raise TaskAbort()

    # ------------------------------------------------------------------ planning
    def _available(self) -> set[str]:
        return self.registry.available()

    async def _plan_step(self, t: dict) -> None:
        tid = t["id"]
        if t["status"] == sm.PENDING:
            sm.transition(self.db, tid, sm.PLANNING, "planning started")
            t = self.store.get_task(tid)
        project = self.store.get_project(t["project_id"])
        lim = self.settings.get_key("limits")
        avail = self._available()
        plan: Plan | None = None
        planner = None
        if t["routing_mode"] == "manual" and t["manual_routes"].get("plan"):
            planner = t["manual_routes"]["plan"]
        else:
            planner = next((p for p in ("openai", "anthropic") if p in avail), None)
        used_fallback = ""
        if planner and planner in avail:
            retrieved = self.memory.retrieve(t["project_id"], t["request"])
            ctx = self.memory.render_context(project, retrieved)
            prompt = assignment(t["request"], "Plan the work", "Produce a plan for the original request.",
                                [f"At most {int(lim['max_subtasks'])} subtasks", "kinds: research, analysis, implementation, document, synthesis",
                                 "depends_on holds 0-based indexes of EARLIER subtasks", "Only set blocking_questions if the task cannot start without an answer",
                                 "Mark research subtasks time_sensitive=true when the answer changes over time"],
                                project_context=ctx, extra=f"<available_agents>{', '.join(sorted(avail))}</available_agents>")
            try:
                plan, _ = await self.gateway.call_structured(planner, AgentRequest(purpose="plan", prompt=prompt, system=SYSTEM["plan"], max_output_tokens=2000),
                                                             Plan, task_id=tid)
            except BudgetExceeded as e:
                self._pause_for_budget(tid, e)
            except (ProviderError, asyncio.TimeoutError) as e:
                used_fallback = f"planner {planner} failed ({e}); used local heuristic plan"
        else:
            used_fallback = "no planner model available; used local heuristic plan"
        if plan is None:
            plan = heuristic_plan(t["request"])
            self.store.log(tid, "warn", used_fallback or "used local heuristic plan")
        if plan.blocking_questions:
            self.store.set_task(tid, plan=plan.model_dump(), paused=True,
                                pause_reason="needs input: " + " | ".join(plan.blocking_questions)[:500])
            self.store.log(tid, "info", "Planner needs more information before work can start", data={"questions": plan.blocking_questions})
            raise TaskAbort()
        plan.subtasks = plan.subtasks[: int(lim["max_subtasks"])]
        try:
            self.db.execute("DELETE FROM subtasks WHERE task_id=?", (tid,))  # a crash mid-planning must not duplicate subtasks
            ids: list[str] = []
            for i, st in enumerate(plan.subtasks):
                r = route(st.kind, avail, t["routing_mode"], t["manual_routes"])
                deps = [ids[d] for d in st.depends_on if d < i]
                sub = self.store.add_subtask(tid, i, st.title, st.kind, st.objective, st.acceptance_criteria, deps, r.provider, r.note)
                if st.kind == "research" and st.time_sensitive:
                    self.store.set_subtask(sub["id"], output={"time_sensitive": True})
                ids.append(sub["id"])
        except RoutingError as e:
            self.db.execute("DELETE FROM subtasks WHERE task_id=?", (tid,))
            self._fail(tid, f"Cannot route the plan: {e}")
            raise TaskAbort()
        self.store.set_task(tid, plan=plan.model_dump())
        self.store.log(tid, "info", f"Plan ready: {len(plan.subtasks)} subtask(s)", data={"summary": plan.summary, "assumptions": plan.assumptions})
        sm.transition(self.db, tid, sm.READY, "plan created")

    async def _to_running(self, t: dict) -> None:
        sm.transition(self.db, t["id"], sm.RUNNING, "execution started")

    # ------------------------------------------------------------------ execution
    def _deps_state(self, sub: dict, by_id: dict[str, dict]) -> str:
        states = [by_id[d]["status"] for d in sub["depends_on"] if d in by_id]
        if any(s in ("failed", "skipped") for s in states):
            return "blocked"
        return "ok" if all(s == "done" for s in states) else "waiting"

    async def _execute_step(self, t: dict) -> None:
        tid = t["id"]
        progressed = True
        while progressed:
            progressed = False
            subs = self.store.subtasks(tid)
            by_id = {s["id"]: s for s in subs}
            for sub in subs:
                if sub["status"] != "ready":
                    continue
                state = self._deps_state(sub, by_id)
                if state == "blocked":
                    self.db.execute("UPDATE subtasks SET status='skipped', error='skipped because a dependency failed or was skipped' WHERE id=?", (sub["id"],))
                    self.store.log(tid, "warn", f"Subtask '{sub['title']}' skipped: a dependency did not complete", subtask_id=sub["id"])
                    progressed = True
                    continue
                if state == "waiting":
                    continue
                cur = self.store.get_task(tid)
                if cur["paused"] or cur["cancel_requested"]:
                    raise TaskAbort()
                await self._run_subtask(cur, sub, by_id)
                progressed = True
                break  # re-read state after every subtask
        subs = self.store.subtasks(tid)
        failed = [s for s in subs if s["status"] in ("failed", "skipped")]
        if failed:
            first = next((s for s in failed if s["status"] == "failed"), failed[0])
            self._fail(tid, f"{len(failed)} subtask(s) did not complete; first problem: '{first['title']}': {first['error'] or 'skipped'}. "
                            "Finished work is preserved and shown as a partial result.")
            raise TaskAbort()
        if not all(s["status"] == "done" for s in subs):
            raise TaskAbort()
        await self._after_execution(self.store.get_task(tid), subs)

    async def _after_execution(self, t: dict, subs: list[dict]) -> None:
        tid = t["id"]
        needs_review = any(s["kind"] in ("implementation", "document") for s in subs) or \
            (is_high_impact(t["request"]) and any(s["kind"] in ("synthesis", "research") for s in subs))
        if needs_review:
            sm.transition(self.db, tid, sm.REVIEWING, "execution finished; independent review")
        else:
            self._finish(tid, unresolved=0)

    async def _run_subtask(self, t: dict, sub: dict, by_id: dict[str, dict]) -> None:
        tid, sid = t["id"], sub["id"]
        token = uuid.uuid4().hex
        if not self.store.claim_subtask(sid, token):
            return  # someone else owns it: never run twice
        self.store.log(tid, "info", f"Started '{sub['title']}' on {sub['agent']} ({sub['kind']})", agent=sub["agent"], subtask_id=sid)
        try:
            output = await self._execute_kind(t, self.store.get_subtask(sid), by_id)
        except BudgetExceeded as e:
            self.db.execute("UPDATE subtasks SET status='ready', claim_token='' WHERE id=?", (sid,))
            self._pause_for_budget(tid, e)
            return
        except asyncio.CancelledError:
            self.db.execute("UPDATE subtasks SET status='ready', claim_token='' WHERE id=?", (sid,))
            raise
        except (ProviderError, StagingError, ValueError) as e:
            self.store.fail_subtask(sid, token, str(e))
            self.store.log(tid, "error", f"'{sub['title']}' failed: {e}", agent=sub["agent"], subtask_id=sid)
            return
        self.store.finish_subtask(sid, token, output)
        self.store.log(tid, "info", f"Finished '{sub['title']}'", agent=sub["agent"], subtask_id=sid)

    def _upstream(self, sub: dict, by_id: dict[str, dict]) -> str:
        parts = []
        for d in sub["depends_on"]:
            dep = by_id.get(d)
            if not dep or dep["status"] != "done":
                continue
            parts.append(untrusted(f"{dep['kind']} result from {dep['agent']}: {dep['title']}", compact(self._summarize_output(dep))))
        return "\n".join(parts)

    def _summarize_output(self, dep: dict) -> dict:
        o = dep["output"]
        if dep["kind"] == "research":
            claims = [c for c in self.store.claims(dep["task_id"]) if c["subtask_id"] == dep["id"]]
            return {"summary": o.get("summary"), "claims": [{"claim": c["text"], "status": c["status"], "support": c["support"], "urls": c["source_urls"]} for c in claims][:15],
                    "unresolved_questions": o.get("unresolved_questions", []), "limitations": o.get("limitations", ""), "no_web_access": o.get("no_web_access", False)}
        if dep["kind"] == "implementation":
            return {"summary": o.get("summary"), "files": o.get("files"), "notes": o.get("notes")}
        return {k: o.get(k) for k in ("summary", "content", "recommendation", "risks", "uncertainties", "title") if o.get(k)}

    async def _execute_kind(self, t: dict, sub: dict, by_id: dict[str, dict]) -> dict:
        kind = sub["kind"]
        project = self.store.get_project(t["project_id"])
        retrieved = self.memory.retrieve(t["project_id"], t["request"] + " " + sub["objective"])
        pctx = self.memory.render_context(project, retrieved)
        feedback = t["revision_feedback"] if kind in REVIEWABLE and t["status"] == sm.REPAIRING else ""
        upstream = self._upstream(sub, by_id)
        lim = self.settings.get_key("limits")
        base = dict(request=t["request"], title=sub["title"], objective=sub["objective"], criteria=sub["acceptance_criteria"], project_context=pctx, upstream=upstream, feedback=feedback)
        if kind == "research":
            return await self._do_research(t, sub, base)
        if kind == "analysis":
            has_research = any(by_id[d]["kind"] == "research" for d in sub["depends_on"] if d in by_id)
            schema, purpose = (EvidenceEvaluation, "evaluation") if has_research else (AnalysisOutput, "analysis")
            extra = ""
            if has_research:
                claims = [c for c in self.store.claims(t["id"]) if c["status"] != "verified"]
                extra = "Claims to evaluate:\n" + "\n".join(f"<claim>{c['text']}</claim>" for c in claims[:15])
            req = AgentRequest(purpose=purpose, system=SYSTEM[purpose], prompt=assignment(**base, extra=extra), max_output_tokens=3000)
            out, _ = await self.gateway.call_structured(sub["agent"], req, schema, task_id=t["id"], subtask_id=sub["id"])
            if has_research:
                for rid in [d for d in sub["depends_on"] if by_id[d]["kind"] == "research"]:
                    self.research.apply_evaluation(t["id"], rid, [v.model_dump() for v in out.claim_verdicts])
            return out.model_dump()
        if kind == "implementation":
            return await self._do_implementation(t, sub, base, project)
        purpose = "document" if kind == "document" else "synthesis"
        extra = ""
        if any(by_id[d]["kind"] == "research" for d in sub["depends_on"] if d in by_id):
            extra = "Cite only the sources listed in the research results. Mark claims that are not verified as unverified in the text."
        req = AgentRequest(purpose=purpose, system=SYSTEM[purpose], prompt=assignment(**base, extra=extra), max_output_tokens=int(lim["max_output_tokens"]))
        out, _ = await self.gateway.call_structured(sub["agent"], req, SynthesisOutput, task_id=t["id"], subtask_id=sub["id"])
        return {"title": out.title, "content": out.content, "open_issues": out.open_issues, "summary": out.content[:300]}

    # ---- research -------------------------------------------------------------
    async def _do_research(self, t: dict, sub: dict, base: dict) -> dict:
        tid, sid = t["id"], sub["id"]
        time_sensitive = bool(sub["output"].get("time_sensitive"))
        question = sub["objective"]
        cached = self.research.cache_lookup(question, time_sensitive)
        if cached:
            report = ResearchReport.model_validate(cached["report"])
            psrc = [Source(**s) for s in cached["provider_sources"]]
            self.store.log(tid, "info", "Reused cached research (no research call made)", agent="coordinator", subtask_id=sid,
                           data={"cached_at": cached.get("cache_created_at")})
            retrieved_at, from_cache, no_web = cached.get("retrieved_at"), True, cached.get("no_web_access", False)
        else:
            no_web = sub["agent"] != "perplexity"
            lim = self.settings.get_key("limits")
            req = AgentRequest(purpose="research", system=SYSTEM["research_no_web" if no_web else "research"],
                               prompt=assignment(**{**base, "feedback": ""}, extra=f"<research_question>{question}</research_question>"),
                               max_output_tokens=3000,
                               research=None if no_web else ResearchOptions(recency="year" if time_sensitive else None,
                                                                            max_results=int(lim["research_max_results"]), time_sensitive=time_sensitive))
            report, result = await self.gateway.call_structured(sub["agent"], req, ResearchReport, task_id=tid, subtask_id=sid)
            psrc = [] if no_web else result.sources
            if no_web:  # a model without web access cannot have sources: drop anything it wrote
                report.sources = []
                for f in report.key_findings:
                    f.source_urls, f.support = [], "interpretation"
            retrieved_at, from_cache = None, False
            if not no_web:
                self.research.cache_store(question, time_sensitive, {"report": report.model_dump(), "no_web_access": False,
                                                                       "provider_sources": [s.__dict__ for s in psrc], "retrieved_at": _now()})
        report.time_sensitive = time_sensitive or report.time_sensitive
        self.research.register(tid, sid, question, report, psrc, retrieved_at)
        verification = await self.research.verify(tid, sid, report.time_sensitive)
        return {"summary": "; ".join(f.claim for f in report.key_findings[:3])[:400], "question": question, "cached": from_cache,
                "no_web_access": no_web, "limitations": report.confidence_and_limitations,
                "unresolved_questions": report.unresolved_questions, "claims_requiring_verification": report.claims_requiring_verification,
                "conflicting_evidence": report.conflicting_evidence, "sources": len(psrc) or len(report.sources), "verification": verification,
                "time_sensitive": report.time_sensitive}

    # ---- implementation ---------------------------------------------------------
    async def _project_files_context(self, t: dict, project: dict | None, sub: dict) -> str:
        if not project or not project.get("root_dir"):
            return ""
        ctx = ToolContext(self.cfg, self.store, self.settings.get(), project, t["id"])
        listing = await self.tools.invoke("list_files", {"max_entries": 80}, ctx)
        if not listing["ok"]:
            return ""
        parts = ["Files in the project directory (read-only view):\n" + "\n".join(listing["result"]["files"])]
        words = list(dict.fromkeys(re.findall(r"[A-Za-z_]{5,}", sub["objective"])))[:8]
        seen: list[str] = []
        # files the request names explicitly come first
        mentioned = f'{sub["objective"]} {t["request"]}'.lower()
        for path in listing["result"]["files"]:
            if path.lower() in mentioned or path.rsplit("/", 1)[-1].lower() in mentioned:
                seen.append(path)
        for w in words:
            res = await self.tools.invoke("search_files", {"query": w, "max_results": 5}, ctx)
            if res["ok"]:
                for h in res["result"]["hits"]:
                    if h["path"] not in seen:
                        seen.append(h["path"])
        for path in seen[:3]:
            r = await self.tools.invoke("read_file", {"path": path}, ctx)
            if r["ok"]:
                parts.append(untrusted(f"project file {path}", r["result"]["text"], 6000))
        return "\n\n".join(parts)

    async def _do_implementation(self, t: dict, sub: dict, base: dict, project: dict | None) -> dict:
        tid = t["id"]
        files_ctx = await self._project_files_context(t, project, sub)
        extra = files_ctx + "\n\nReturn every file you want to add or change in `files` with its COMPLETE content (not a diff)."
        req = AgentRequest(purpose="implementation", system=SYSTEM["implementation"], prompt=assignment(**base, extra=extra),
                           max_output_tokens=int(self.settings.get_key("limits")["max_output_tokens"]))
        out, _ = await self.gateway.call_structured(sub["agent"], req, ImplementationOutput, task_id=tid, subtask_id=sub["id"])
        proposal = self.staging.create_proposal(tid, project, out.files, sub["title"], out.summary, sub["agent"])
        return {"summary": out.summary, "notes": out.notes, "proposal_id": proposal["id"], "files": [f["path"] for f in proposal["files"]]}

    # ------------------------------------------------------------------ review & repair
    def _current_proposal(self, tid: str) -> dict | None:
        for p in reversed(self.store.proposals(tid)):
            if p["status"] in ("pending", "approved"):
                return p
        return None

    async def _run_tests(self, t: dict, project: dict | None, proposal: dict | None) -> dict | None:
        if not proposal:
            return None
        cmd = (project or {}).get("test_command", "").strip()
        if not cmd:
            self.store.add_test_run(t["id"], proposal["id"], "", "not_run", None, "No test command is configured for this project, so no tests were run.", 0)
            return {"status": "not_run", "output": "no test command configured"}
        st = self.settings.get()
        from .tools.base import ToolError
        try:
            argv = parse_command(cmd, st["allowed_test_executables"])
            root = None
            if project and project.get("root_dir") and Path(project["root_dir"]).expanduser().is_dir():
                root = Path(project["root_dir"]).expanduser().resolve()
            run_id = uuid.uuid4().hex[:8]
            sandbox = build_sandbox(self.cfg, t["id"], run_id, root, [(f["path"], Path(f["staged_path"])) for f in proposal["files"]])
            try:
                status, code, out, ms = await run_command(argv, sandbox, float(st["test_timeout_s"]))
            finally:
                import shutil
                shutil.rmtree(sandbox, ignore_errors=True)
        except ToolError as e:
            status, code, out, ms = "error", None, f"{e.code}: {e.message}", 0
        self.store.add_test_run(t["id"], proposal["id"], cmd, status, code, out, ms)
        self.store.log(t["id"], "info" if status == "passed" else "warn", f"Tests {status} ({cmd})", agent="coordinator")
        return {"status": status, "output": out}

    async def _review_step(self, t: dict) -> None:
        tid = t["id"]
        project = self.store.get_project(t["project_id"])
        subs = self.store.subtasks(tid)
        lim = self.settings.get_key("limits")
        round_ = t["repair_attempts"] + 1 + t["revision_rounds"]
        target = next((s for s in reversed(subs) if s["kind"] == "implementation"), None) or \
            next((s for s in reversed(subs) if s["kind"] in ("document", "synthesis")), None) or subs[-1]
        proposal = self._current_proposal(tid) if target["kind"] == "implementation" else None
        tests = await self._run_tests(t, project, proposal) if proposal else None
        try:
            r = route_review(target["agent"], self._available(), t["routing_mode"], t["manual_routes"])
        except RoutingError as e:
            self._fail(tid, f"Cannot choose a reviewer: {e}")
            raise TaskAbort()
        artifact = self._review_artifact(tid, target, proposal)
        claims = [c for c in self.store.claims(tid)]
        extra = ("<artifact>\n" + artifact + "\n</artifact>\n" + (f"<test_result>{tests['status']}\n{tests['output'][-1500:]}</test_result>\n" if tests else "") +
                 (f"<claim_statuses>{compact([{'claim': c['text'], 'status': c['status'], 'support': c['support']} for c in claims][:15], 2500)}</claim_statuses>\n" if claims else "") +
                 f"<review_round>{round_}</review_round>\n<author>{target['agent']}</author>")
        prompt = assignment(t["request"], f"Review: {target['title']}", "Independently review the artifact against the request and acceptance criteria.",
                            target["acceptance_criteria"], extra=extra)
        try:
            review, _ = await self.gateway.call_structured(r.provider, AgentRequest(purpose="review", system=SYSTEM["review"], prompt=prompt, max_output_tokens=1800),
                                                           ReviewOutput, task_id=tid, subtask_id=target["id"])
        except BudgetExceeded as e:
            self._pause_for_budget(tid, e)
        except ProviderError as e:
            self.store.log(tid, "error", f"Review could not be completed: {e}", agent=r.provider)
            self._finish(tid, unresolved=1, note=f"Independent review could not be completed ({e.kind}).")
            return
        issues = [i.model_dump() for i in review.issues]
        if tests and tests["status"] in ("failed", "error", "timeout"):
            issues.append({"severity": "blocker", "location": "tests", "description": f"Automated tests {tests['status']}.",
                           "evidence": tests["output"][-600:]})
        self.store.add_review(tid, target["id"], r.provider, target["agent"], round_, review.verdict, issues, review.summary + ("" if r.note == "independent reviewer" else f" [{r.note}]"))
        blocking = review.verdict == "fail" or any(i["severity"] == "blocker" for i in issues)
        self.store.log(tid, "info" if not blocking else "warn", f"Review by {r.provider}: {review.verdict}, {len(issues)} issue(s) ({r.note})", agent=r.provider)
        if not blocking:
            self._finish(tid, unresolved=0)
            return
        fingerprint = _fingerprint(issues, tests)
        prev = self.store.reviews(tid)
        repeated = len(prev) >= 2 and _similar(fingerprint, _fingerprint(prev[-2]["issues"], None))
        if t["repair_attempts"] >= int(lim["max_repair_attempts"]) or repeated:
            why = "repeated failure detected" if repeated else f"repair limit ({int(lim['max_repair_attempts'])}) reached"
            self.store.log(tid, "warn", f"Stopping the repair loop: {why}", agent="coordinator")
            self._finish(tid, unresolved=1, note=f"Unresolved review issues remain ({why}).")
            return
        feedback = "Fix these problems found by the independent reviewer and the test run:\n" + "\n".join(
            f"- [{i['severity']}] {i['description']} (evidence: {i['evidence'][:300]})" for i in issues)
        sm.transition(self.db, tid, sm.REPAIRING, f"review found {len(issues)} issue(s); repair attempt {t['repair_attempts'] + 1}",
                      repair_attempts=t["repair_attempts"] + 1, revision_feedback=feedback)

    def _review_artifact(self, tid: str, target: dict, proposal: dict | None) -> str:
        if proposal:
            parts = []
            for f in proposal["files"]:
                text = Path(f["staged_path"]).read_text(encoding="utf-8", errors="replace")
                parts.append(untrusted(f"proposed file {f['path']} ({f['action']})", text, 5000))
            return "\n".join(parts)
        return untrusted(f"deliverable by {target['agent']}", compact(target["output"], 6000), 6000)

    async def _repair_step(self, t: dict) -> None:
        tid = t["id"]
        subs = self.store.subtasks(tid)
        by_id = {s["id"]: s for s in subs}
        targets = [s for s in subs if s["kind"] == "implementation"] or [s for s in subs if s["kind"] in ("document", "synthesis")][-1:]
        if not targets:
            self._fail(tid, "Nothing to repair: no deliverable subtask found.")
            raise TaskAbort()
        for sub in targets:
            token = uuid.uuid4().hex
            self.db.execute("UPDATE subtasks SET status='ready', claim_token='' WHERE id=?", (sub["id"],))
            if not self.store.claim_subtask(sub["id"], token):
                continue
            fresh = self.store.get_subtask(sub["id"])
            history = {"previous_output": sub["output"]}
            try:
                out = await self._execute_kind(self.store.get_task(tid), fresh, by_id)
            except BudgetExceeded as e:
                self.db.execute("UPDATE subtasks SET status='done', claim_token='' WHERE id=?", (sub["id"],))
                self._pause_for_budget(tid, e)
            except (ProviderError, StagingError, ValueError) as e:
                self.store.fail_subtask(sub["id"], token, str(e), output=history["previous_output"])
                self._fail(tid, f"Repair attempt failed for '{sub['title']}': {e}")
                raise TaskAbort()
            except asyncio.CancelledError:
                self.db.execute("UPDATE subtasks SET status='done', claim_token='' WHERE id=?", (sub["id"],))
                raise
            out["history"] = history
            self.store.finish_subtask(sub["id"], token, out)
        self.store.log(tid, "info", "Repair attempt produced a new version; sending back to review", agent=targets[0]["agent"])
        sm.transition(self.db, tid, sm.REVIEWING, "repaired; re-review")

    # ------------------------------------------------------------------ finishing / applying
    def _finish(self, tid: str, unresolved: int, note: str = "") -> None:
        t = self.store.get_task(tid)
        proposal = self._current_proposal(tid)
        if proposal:
            self.db.execute("UPDATE proposals SET unresolved_issues=? WHERE id=?", (unresolved, proposal["id"]))
            sm.transition(self.db, tid, sm.AWAITING_APPROVAL, note or "ready for your approval", unresolved_issues=unresolved)
            self.store.log(tid, "info", "Proposal awaiting approval", agent="coordinator")
        else:
            outcome = "success" if not unresolved else "completed_with_issues"
            sm.transition(self.db, tid, sm.COMPLETED, note or "all subtasks finished", unresolved_issues=unresolved, outcome=outcome)
        self._finalize_report(tid)

    async def _apply_wrapper(self, t: dict) -> None:
        lock = self._apply_locks.setdefault(t["id"], asyncio.Lock())
        async with lock:
            await self._apply_step(self.store.get_task(t["id"]))

    async def _apply_step(self, t: dict) -> None:
        tid = t["id"]
        if t["status"] == sm.COMPLETED:
            return
        project = self.store.get_project(t["project_id"])
        proposal = next((p for p in reversed(self.store.proposals(tid)) if p["status"] == "approved"), None)
        if t["status"] == sm.APPROVED:
            sm.transition(self.db, tid, sm.APPLYING, "applying approved changes")
        try:
            if not proposal:
                raise ApprovalError("no approved proposal found")
            paths = self.staging.apply(proposal["id"], project)
        except (ApplyConflict, ApprovalError, StagingError, OSError) as e:
            self.db.execute("UPDATE proposals SET status='failed' WHERE id=? AND status='approved'", (proposal["id"],)) if proposal else None
            sm.transition(self.db, tid, sm.FAILED, "apply refused", error=f"Changes were NOT applied: {e}", outcome="failed")
            self.store.log(tid, "error", f"Apply refused: {e}")
            self._finalize_report(tid)
            return
        outcome = "completed_with_issues" if proposal["unresolved_issues"] else "success"
        sm.transition(self.db, tid, sm.COMPLETED, f"applied {len(paths)} file(s)", outcome=outcome)
        self._finalize_report(tid)


def _now() -> str:
    from .db import now
    return now()


def _fingerprint(issues: list[dict], tests: dict | None) -> set[str]:
    toks: set[str] = set()
    for i in issues:
        toks |= set(re.findall(r"[a-z]{4,}", i.get("description", "").lower()))
    if tests and tests.get("status") in ("failed", "error", "timeout"):
        toks |= {"tests:" + w for w in re.findall(r"[a-z]{4,}", tests["output"][-300:].lower())}
    return toks


def _similar(a: set[str], b: set[str]) -> bool:
    return bool(a and b) and len(a & b) / len(a | b) >= 0.6
