"""Deterministic planning helpers: request classification and a heuristic fallback plan.

Simple parsing and routing is done in plain code on purpose (cost: no model call needed).
"""
from __future__ import annotations

import re

from .schemas import Plan, PlanSubtask

_RESEARCH = r"\b(research|find out|look up|latest|current|currently|recent|news|sources?|documentation|docs|pricing|price|compare|comparison|alternatives|approaches|investigate|survey|state of|best practices|versions?)\b"
_IMPLEMENT = r"\b(implement|write (?:a |the )?(?:code|script|function|program|class|module|test)|code|build|create (?:a |an |the )?(?:script|function|app|module|file|program|class)|add (?:a |an )?(?:feature|function|endpoint)|fix|refactor|script|function|program|branch)\b"
_DOCUMENT = r"\b(draft|write (?:a |an |the )?(?:document|report|memo|readme|summary|essay|email|post|guide|plan)|document|summari[sz]e|readme|report)\b"
_ANALYSIS = r"\b(analy[sz]e|evaluate|trade-?offs?|decide|decision|pros and cons|plan|strategy|assess|review)\b"
_TIME_SENSITIVE = r"\b(latest|current|currently|today|now|recent|news|price|pricing|version|release|this (?:week|month|year)|20\d\d)\b"
_HIGH_IMPACT = r"\b(legal|medical|health|financial|invest|tax|security|production|deploy|safety|compliance|contract)\b"


def classify(request: str) -> dict[str, bool]:
    t = request.lower()
    return {
        "research": bool(re.search(_RESEARCH, t)),
        "implementation": bool(re.search(_IMPLEMENT, t)),
        "document": bool(re.search(_DOCUMENT, t)),
        "analysis": bool(re.search(_ANALYSIS, t)),
        "time_sensitive": bool(re.search(_TIME_SENSITIVE, t)),
        "high_impact": bool(re.search(_HIGH_IMPACT, t)),
    }


def is_high_impact(request: str) -> bool:
    return classify(request)["high_impact"]


def heuristic_plan(request: str) -> Plan:
    """Build a small plan from keywords. Used as the demo-mode planner and as a live fallback."""
    c = classify(request)
    subtasks: list[PlanSubtask] = []
    short = request.strip().replace("\n", " ")
    short = short[:160] + ("…" if len(short) > 160 else "")

    research_idx = analysis_idx = None
    if c["research"]:
        subtasks.append(PlanSubtask(
            title="Research current information",
            kind="research",
            objective=f"Find relevant, current, citable information for: {short}",
            acceptance_criteria=["Every finding cites at least one source URL",
                                 "Conflicting evidence and open questions are listed"],
            time_sensitive=c["time_sensitive"],
        ))
        research_idx = 0
    if c["research"] or c["analysis"]:
        subtasks.append(PlanSubtask(
            title="Analyze options and trade-offs",
            kind="analysis",
            objective=f"Evaluate the evidence/options and recommend an approach for: {short}",
            acceptance_criteria=["Options compared against explicit criteria",
                                 "Assumptions and logical gaps are identified"],
            depends_on=[research_idx] if research_idx is not None else [],
        ))
        analysis_idx = len(subtasks) - 1
    deps = [i for i in (analysis_idx, research_idx) if i is not None][:1]
    if c["implementation"]:
        subtasks.append(PlanSubtask(
            title="Implement the solution as proposed changes",
            kind="implementation",
            objective=f"Produce the code/files that fulfil: {short}. Output files only as proposed changes.",
            acceptance_criteria=["Files are complete and runnable", "Includes tests where practical"],
            depends_on=deps,
        ))
    elif c["document"] or not subtasks:
        subtasks.append(PlanSubtask(
            title="Produce the deliverable",
            kind="document" if c["document"] else "synthesis",
            objective=f"Produce the requested deliverable for: {short}",
            acceptance_criteria=["Directly answers the request", "States limitations honestly"],
            depends_on=deps,
        ))
    elif c["research"] and not c["implementation"]:
        subtasks.append(PlanSubtask(
            title="Synthesize findings into the final deliverable",
            kind="synthesis",
            objective=f"Turn the verified findings into a practical deliverable for: {short}",
            acceptance_criteria=["Cites sources for factual claims", "Lists open uncertainties"],
            depends_on=[len(subtasks) - 1],
        ))
    return Plan(
        summary="Heuristic plan built by local rules (no model call).",
        assumptions=["Request interpreted literally; no additional context supplied."],
        subtasks=subtasks,
    )
