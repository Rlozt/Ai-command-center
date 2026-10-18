"""Deterministic agent routing. Default roles are preferences, not restrictions."""
from __future__ import annotations

from dataclasses import dataclass

# preference order per subtask kind; later entries are fallbacks when earlier ones are unavailable
PREFERENCES: dict[str, list[str]] = {
    "research": ["perplexity"],  # web research only runs on Perplexity; see fallback handling below
    "analysis": ["openai", "anthropic", "local"],
    "implementation": ["anthropic", "openai", "local"],
    "document": ["anthropic", "openai", "local"],
    "synthesis": ["anthropic", "openai", "local"],
}
# If Perplexity is unavailable, another model can still answer from its own knowledge but has NO web access.
RESEARCH_FALLBACKS = ["openai", "anthropic", "local"]


class RoutingError(Exception):
    pass


@dataclass
class Route:
    provider: str
    note: str = ""
    no_web_access: bool = False


def route(kind: str, available: set[str], mode: str = "auto", manual: dict[str, str] | None = None) -> Route:
    if not available:
        raise RoutingError("No AI provider is available. Enable and configure at least one provider (or use demo mode).")
    if mode == "manual":
        choice = (manual or {}).get(kind)
        if not choice:
            raise RoutingError(f"Manual routing is on but no provider was chosen for '{kind}' subtasks.")
        if choice not in available:
            raise RoutingError(f"Manual route for '{kind}' is {choice}, which is disabled or not configured.")
        return Route(choice, "manual route", no_web_access=(kind == "research" and choice != "perplexity"))
    for p in PREFERENCES[kind]:
        if p in available:
            return Route(p, "default role")
    if kind == "research":
        for p in RESEARCH_FALLBACKS:
            if p in available:
                return Route(p, "Perplexity unavailable: answering from model knowledge WITHOUT live web access; "
                                "findings are unverified", no_web_access=True)
    for p in sorted(available):
        return Route(p, f"fallback: preferred providers for '{kind}' unavailable")
    raise RoutingError("No provider available")


def route_review(author: str, available: set[str], mode: str = "auto", manual: dict[str, str] | None = None) -> Route:
    """Pick an independent reviewer: a different provider than the author whenever possible."""
    if mode == "manual" and (manual or {}).get("review"):
        choice = manual["review"]
        if choice not in available:
            raise RoutingError(f"Manual route for 'review' is {choice}, which is disabled or not configured.")
        note = "manual route" + ("" if choice != author else " (same agent as author: NOT independent)")
        return Route(choice, note)
    # Perplexity is a search agent, so it is never used as a code/document reviewer.
    order = {"openai": ["anthropic", "local"], "anthropic": ["openai", "local"], "perplexity": ["openai", "anthropic", "local"],
             "local": ["openai", "anthropic"]}.get(author, ["openai"])
    for p in order:
        if p in available:
            return Route(p, "independent reviewer")
    if author in available:
        return Route(author, "only one provider available: self-review is NOT independent")
    raise RoutingError("No provider available for review")
