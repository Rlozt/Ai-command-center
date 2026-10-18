"""Demo-mode providers. They run entirely offline, make no billable calls, and are labelled DEMO everywhere.

They are deterministic stand-ins used for local development and the automated test-suite. They do NOT
pretend to browse the web: demo research says so explicitly and cites a placeholder URL.

Failure injection (for testing / trying failure handling in the UI) is triggered by tokens inside the
original request text, e.g. `[mock:rate-limit]`, `[mock:rate-limit-once]`, `[mock:timeout]`,
`[mock:auth]`, `[mock:bad-json]`, `[mock:review-fail]`, `[mock:review-fail-always]`, `[mock:conflict]`.
"""
from __future__ import annotations

import asyncio
import json
import re

from .base import AgentRequest, AgentResult, Provider, ProviderError, Source, Usage
from ..planning import heuristic_plan


def _request_text(prompt: str) -> str:
    m = re.search(r"<original_request>(.*?)</original_request>", prompt, re.DOTALL)
    return (m.group(1) if m else prompt).strip()


def _tag(prompt: str, tag: str, default: str = "") -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", prompt, re.DOTALL)
    return m.group(1).strip() if m else default


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    stop = {"a", "an", "the", "to", "of", "and", "in", "for", "write", "create", "make", "build", "that", "with", "python", "function", "script", "please"}
    keep = [w for w in words if w not in stop][:3]
    return "_".join(keep) or "demo"


class MockProvider(Provider):
    billable = False
    demo = True

    def __init__(self, name: str, role: str, model: str):
        self.name = name
        self.role = role
        self._model = model
        self._once_seen: set[str] = set()
        self.calls: list[AgentRequest] = []  # inspected by tests

    @property
    def model(self) -> str:
        return self._model

    def is_configured(self) -> bool:
        return True

    async def validate(self) -> tuple[bool, str]:
        return True, "Demo mode: offline mock provider, no credentials needed and no billable calls."

    async def complete(self, req: AgentRequest) -> AgentResult:
        self.calls.append(req)
        original = _request_text(req.prompt)
        await self._maybe_fail(req, original)
        payload = self._respond(req, original)
        text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
        if "[mock:bad-json]" in original and req.expect_json and req.purpose != "ping":
            text = "Sorry, here is some prose instead of JSON."
        usage = Usage(input_tokens=max(1, len(req.prompt) // 4), output_tokens=max(1, len(text) // 4), cost_usd=0.0, cost_basis="demo")
        sources: list[Source] = []
        if req.purpose == "research":
            sources = [Source(url="https://example.com/demo-source", title="[DEMO] placeholder source",
                              snippet="Demo mode performs no web search; this is a placeholder, not evidence.")]
        return AgentResult(provider=self.name, model=self._model, text=text, usage=usage, sources=sources,
                           response_id=f"demo-{len(self.calls)}", stop_reason="end_turn", meta={"demo": True})

    async def _maybe_fail(self, req: AgentRequest, original: str) -> None:
        if "[mock:auth]" in original:
            raise ProviderError("auth", "mock auth failure", provider=self.name)
        if "[mock:rate-limit]" in original:
            raise ProviderError("rate_limit", "mock rate limit", retryable=True, retry_after=0, provider=self.name)
        if "[mock:rate-limit-once]" in original and req.purpose not in self._once_seen:
            self._once_seen.add(req.purpose)
            raise ProviderError("rate_limit", "mock rate limit (once)", retryable=True, retry_after=0, provider=self.name)
        if "[mock:timeout]" in original and self.name in ("anthropic",) and req.purpose in ("implementation", "document", "synthesis"):
            await asyncio.sleep(3600)  # the gateway's timeout must cut this off
        if "[mock:fail-research]" in original and req.purpose == "research":
            raise ProviderError("unavailable", "mock research outage", retryable=False, provider=self.name)

    def _respond(self, req: AgentRequest, original: str):
        p = req.purpose
        if p == "ping":
            return "pong"
        if p == "plan":
            return heuristic_plan(original).model_dump()
        if p == "research":
            return self._research(req, original)
        if p in ("analysis",):
            return {"summary": "[DEMO] Analysis of the request against the supplied research.",
                    "content": "## Options\n- Option A: simplest approach.\n- Option B: more flexible, higher cost.\n\n"
                               "## Recommendation\nOption A, because it satisfies the stated acceptance criteria with the least risk.\n\n"
                               "_Demo-mode output: generated offline by a mock provider._",
                    "risks": ["Demo output is not based on real analysis."], "unsupported_assumptions": [],
                    "recommendation": "Option A"}
        if p == "evaluation":
            claims = re.findall(r"<claim>(.*?)</claim>", req.prompt, re.DOTALL)
            verdicts = []
            for i, c in enumerate(claims):
                status = "unverified"
                if "[mock:conflict]" in original and i == 0:
                    status = "conflicting"
                if "[mock:outdated]" in original and i == 0:
                    status = "outdated"
                verdicts.append({"claim": c.strip(), "status": status, "note": "Demo evaluation: no external evidence was examined."})
            return {"summary": "[DEMO] Evidence evaluation.", "content": "Demo mode cannot assess real evidence.",
                    "claim_verdicts": verdicts, "uncertainties": ["All findings are demo placeholders."]}
        if p in ("implementation",):
            return self._implementation(req, original)
        if p in ("document", "synthesis"):
            return {"title": "Deliverable (demo)", "content": f"# Deliverable\n\nRequest: {original[:300]}\n\n"
                    "_Demo mode: this text was generated offline by a mock provider; no AI model was called._\n",
                    "open_issues": ["Run in live mode with API keys for real output."]}
        if p == "review":
            return self._review(req, original)
        return "ok"

    def _research(self, req: AgentRequest, original: str) -> dict:
        return {
            "question": original[:200],
            "key_findings": [{"claim": "[DEMO] No live web search was performed in demo mode.", "support": "interpretation",
                              "source_urls": ["https://example.com/demo-source"], "confidence": "low"}],
            "sources": [{"title": "[DEMO] placeholder source", "url": "https://example.com/demo-source", "publisher": "example.com",
                         "published": None, "excerpt": "Placeholder, not evidence."}],
            "confidence_and_limitations": "Demo mode: nothing here comes from the web.",
            "conflicting_evidence": [], "claims_requiring_verification": ["[DEMO] No live web search was performed in demo mode."],
            "unresolved_questions": ["Everything: run in live mode with PERPLEXITY_API_KEY."],
            "time_sensitive": bool(req.research and req.research.time_sensitive),
        }

    def _implementation(self, req: AgentRequest, original: str) -> dict:
        slug = _slug(original)
        feedback = _tag(req.prompt, "revision_feedback")
        greet = "Hello" if not feedback else "Hello"  # content is identical; feedback is just recorded
        files = [
            {"path": f"demo_output/{slug}.py", "action": "create",
             "content": f'def greet(name: str) -> str:\n    """Return a friendly greeting."""\n    return f"{greet}, {{name}}!"\n'},
            {"path": f"demo_output/test_{slug}.py", "action": "create",
             "content": f"import unittest\n\nfrom {slug} import greet\n\n\nclass GreetTest(unittest.TestCase):\n"
                        f'    def test_greet(self):\n        self.assertEqual(greet("World"), "{greet}, World!")\n\n\n'
                        "if __name__ == '__main__':\n    unittest.main()\n"},
        ]
        if "[mock:break-tests]" in original and not feedback:
            files[1]["content"] = files[1]["content"].replace(f'"{greet}, World!"', '"Goodbye, World!"')
        if "[mock:unsafe-path]" in original:
            files.append({"path": "../escape.txt", "action": "create", "content": "nope"})
        return {"summary": f"[DEMO] Proposed a small module `{slug}` with a unittest.", "files": files,
                "notes": "Demo-mode output generated offline by a mock provider."}

    def _review(self, req: AgentRequest, original: str) -> dict:
        rnd = int(_tag(req.prompt, "review_round", "1") or 1)
        fail = "[mock:review-fail-always]" in original or ("[mock:review-fail]" in original and rnd == 1)
        if fail:
            return {"verdict": "fail", "summary": "[DEMO] Reviewer found a problem.",
                    "checks_performed": ["Read the proposed files"],
                    "issues": [{"severity": "major", "location": "demo_output", "description": "Docstring missing a usage example.",
                                "evidence": "The module has no usage example in its docstring."}]}
        return {"verdict": "pass", "summary": "[DEMO] No blocking problems found.",
                "checks_performed": ["Read the proposed files", "Compared output against acceptance criteria"], "issues": []}
