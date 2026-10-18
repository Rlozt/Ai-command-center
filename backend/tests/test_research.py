"""Perplexity research path: routing, cache, claim verification, source origin and cost handling. No network, no paid calls."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Config
from app.context import build_context
from app.providers.base import AgentResult, Source, Usage
from app.providers.mock import MockProvider
from app.providers.perplexity_provider import PerplexityProvider
from tests.conftest import FakeFetcher, no_sleep, run_task
from tests.helpers import ScriptedProvider

URL_A = "https://docs.example.org/a"
URL_B = "https://blog.example.org/b"
EXCERPT_A = "Version 3.14 was released in October"


def report(**over) -> dict:
    base = {
        "question": "q",
        "key_findings": [
            {"claim": "Version 3.14 shipped in October", "support": "source", "source_urls": [URL_A], "confidence": "high"},
            {"claim": "It is probably popular", "support": "interpretation", "source_urls": [], "confidence": "low"},
        ],
        "sources": [{"title": "Docs A", "url": URL_A, "publisher": "example.org", "published": "2026-09-01", "excerpt": EXCERPT_A},
                    {"title": "Invented", "url": "https://invented.example/zzz", "publisher": "invented.example", "excerpt": "made up"}],
        "confidence_and_limitations": "ok", "conflicting_evidence": [], "claims_requiring_verification": [],
        "unresolved_questions": [], "time_sensitive": False,
    }
    base.update(over)
    return base


class SearchProvider(ScriptedProvider):
    """Billable fake Perplexity: returns scripted JSON plus provider-side search results."""

    def __init__(self, payload=None, sources=None, cost=0.01):
        super().__init__("perplexity", "Research", "perplexity/test", script=[json.dumps(payload or report())], cost=cost)
        self.sources = sources if sources is not None else [Source(URL_A, "Docs A", EXCERPT_A, "2026-09-01", "2026-09-01")]

    async def complete(self, req):
        res = await super().complete(req)
        res.sources = list(self.sources)
        return res


def live_ctx(tmp_path, pplx=None, pages=None, enabled=None):
    cfg = Config(mode="live", data_dir=tmp_path / "d")
    providers = {"openai": MockProvider("openai", "Analysis", "mock-o"), "anthropic": MockProvider("anthropic", "Implementation", "mock-a")}
    if pplx is not None:
        providers["perplexity"] = pplx
    ctx = build_context(cfg, providers=providers, fetcher=FakeFetcher(pages or {}), sleep=no_sleep)
    if enabled:
        ctx.settings.update({"providers_enabled": enabled})
    return ctx


REQ = "Research how python packaging works and cite sources"
REQ_FRESH = "Research the latest python release and cite sources"


async def test_research_runs_on_perplexity_with_provider_sources_and_cost(tmp_path):
    pp = SearchProvider()
    ctx = live_ctx(tmp_path, pp, {URL_A: EXCERPT_A + " this year."})
    t = await run_task(ctx, REQ)
    assert t["status"] == "completed", t["error"]
    assert len(pp.calls) == 1 and pp.calls[0].research is not None
    calls = [c for c in ctx.store.calls(task_id=t["id"]) if c["provider"] == "perplexity"]
    assert calls[0]["cost_basis"] == "reported" and calls[0]["billable"]
    rep = t["report"]
    assert rep["usage"]["billable_calls"] >= 1 and rep["usage"]["cost_usd"] >= 0.01  # a paid service was used
    assert "Demo mode" not in rep["cost_statement"] or rep["usage"]["demo"] is False
    src = {s["url"]: s for s in ctx.store.sources(t["id"])}
    assert src[URL_A]["origin"] == "provider"
    assert src["https://invented.example/zzz"]["origin"] == "model_claimed"
    assert "not returned by the search API" in src["https://invented.example/zzz"]["check_note"]


async def test_claim_is_verified_only_when_excerpt_found_on_live_page(tmp_path):
    ctx = live_ctx(tmp_path, SearchProvider(), {URL_A: "blah " + EXCERPT_A + " blah"})
    t = await run_task(ctx, REQ)
    claims = {c["text"]: c for c in ctx.store.claims(t["id"])}
    assert claims["Version 3.14 shipped in October"]["status"] == "verified"
    assert claims["It is probably popular"]["status"] == "unverified"  # interpretation can never be verified by a source


async def test_unreachable_or_mismatching_page_leaves_claim_unverified(tmp_path):
    ctx = live_ctx(tmp_path, SearchProvider(), {})  # every fetch is a 404
    t = await run_task(ctx, REQ)
    c = [c for c in ctx.store.claims(t["id"]) if c["text"].startswith("Version")][0]
    assert c["status"] == "unverified"
    assert [s for s in ctx.store.sources(t["id"]) if s["url"] == URL_A][0]["check_status"] == "unreachable"
    ctx2 = live_ctx(tmp_path / "x", SearchProvider(), {URL_A: "entirely different text"})
    t2 = await run_task(ctx2, REQ)
    c2 = [c for c in ctx2.store.claims(t2["id"]) if c["text"].startswith("Version")][0]
    assert c2["status"] == "unverified" and "not found" in c2["note"]


async def test_claim_citing_only_a_model_written_url_is_not_verified(tmp_path):
    payload = report(key_findings=[{"claim": "Fabricated fact", "support": "source", "source_urls": ["https://invented.example/zzz"], "confidence": "high"}])
    ctx = live_ctx(tmp_path, SearchProvider(payload), {"https://invented.example/zzz": "made up"})
    t = await run_task(ctx, REQ)
    c = ctx.store.claims(t["id"])[0]
    assert c["status"] == "unverified" and "search API" in c["note"]


async def test_old_source_makes_time_sensitive_claim_outdated(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(days=400)).date().isoformat()
    pp = SearchProvider(sources=[Source(URL_A, "Docs A", EXCERPT_A, old, old)])
    ctx = live_ctx(tmp_path, pp, {URL_A: EXCERPT_A})
    t = await run_task(ctx, REQ_FRESH)
    c = [c for c in ctx.store.claims(t["id"]) if c["text"].startswith("Version")][0]
    assert c["status"] == "outdated"


async def test_evaluator_can_downgrade_but_never_verify(tmp_path):
    ctx = live_ctx(tmp_path, SearchProvider(), {})
    t = await run_task(ctx, REQ)
    rid = ctx.store.subtasks(t["id"])[0]["id"]
    claims = ctx.store.claims(t["id"])
    first = claims[0]
    assert first["status"] == "unverified"
    ctx.research.apply_evaluation(t["id"], rid, [{"claim": first["text"], "status": "verified", "note": "trust me"}])
    assert ctx.store.claims(t["id"])[0]["status"] == "unverified"
    ctx.research.apply_evaluation(t["id"], rid, [{"claim": first["text"], "status": "conflicting", "note": "disagrees"}])
    assert ctx.store.claims(t["id"])[0]["status"] == "conflicting"


async def test_cache_reuses_result_and_makes_no_second_paid_call(tmp_path):
    pp = SearchProvider()
    ctx = live_ctx(tmp_path, pp, {URL_A: EXCERPT_A})
    await run_task(ctx, REQ)
    t2 = await run_task(ctx, REQ)
    assert len(pp.calls) == 1
    sub = [s for s in ctx.store.subtasks(t2["id"]) if s["kind"] == "research"][0]
    assert sub["output"]["cached"] is True
    assert ctx.store.calls(task_id=t2["id"], provider="perplexity") == []
    assert any("cached research" in l["message"] for l in ctx.store.logs(t2["id"]))


async def test_near_duplicate_question_hits_the_cache(tmp_path):
    ctx = live_ctx(tmp_path, SearchProvider(), {})
    ctx.research.cache_store("How does python packaging work today for libraries", False, {"report": report(), "provider_sources": []})
    hit = ctx.research.cache_lookup("how does python packaging work today for libraries?", False)
    assert hit is not None
    assert ctx.research.cache_lookup("quantum chromodynamics lattice simulation", False) is None


async def test_cached_non_time_sensitive_answer_does_not_satisfy_time_sensitive_question(tmp_path):
    ctx = live_ctx(tmp_path, SearchProvider(), {})
    ctx.research.cache_store("python release notes", False, {"report": report(), "provider_sources": []})
    assert ctx.research.cache_lookup("python release notes", True) is None
    assert ctx.research.cache_lookup("python release notes", False) is not None


async def test_expired_cache_entries_are_ignored(tmp_path):
    ctx = live_ctx(tmp_path, SearchProvider(), {})
    ctx.research.cache_store("python release notes", True, {"report": report(), "provider_sources": []})
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(timespec="milliseconds")
    ctx.db.execute("UPDATE research_cache SET created_at=?", (old,))
    assert ctx.research.cache_lookup("python release notes", True) is None  # time-sensitive TTL is 6h
    assert ctx.research.cache_lookup("python release notes", False) is not None  # normal TTL is 72h


async def test_perplexity_disabled_falls_back_to_no_web_model_and_says_so(tmp_path):
    pp = SearchProvider()
    ctx = live_ctx(tmp_path, pp, {}, enabled={"openai": True, "anthropic": True, "perplexity": False})
    t = await run_task(ctx, REQ)
    assert t["status"] == "completed", t["error"]
    assert pp.calls == []
    sub = [s for s in ctx.store.subtasks(t["id"]) if s["kind"] == "research"][0]
    assert sub["agent"] in ("openai", "anthropic") and sub["output"]["no_web_access"] is True
    assert ctx.store.sources(t["id"]) == []  # a model with no web access cannot have sources
    assert all(c["status"] != "verified" for c in ctx.store.claims(t["id"]))
    assert any("WITHOUT live web access" in c for c in t["report"]["caveats"])


async def test_missing_perplexity_credentials_are_handled_gracefully(tmp_path):
    cfg = Config(mode="live", data_dir=tmp_path / "d", openai_api_key="sk-test0000000000", anthropic_api_key="sk-ant-test0000000000")
    ctx = build_context(cfg, providers={"openai": MockProvider("openai", "Analysis", "m"), "anthropic": MockProvider("anthropic", "Implementation", "m"),
                                         "perplexity": PerplexityProvider(cfg)}, fetcher=FakeFetcher(), sleep=no_sleep)
    st = {s.provider: s for s in ctx.registry.status()}
    assert st["perplexity"].configured is False and st["perplexity"].ready is False
    t = await run_task(ctx, REQ)  # must not crash or fake a Perplexity result
    assert t["status"] == "completed"
    sub = [s for s in ctx.store.subtasks(t["id"]) if s["kind"] == "research"][0]
    assert sub["agent"] != "perplexity" and sub["output"]["no_web_access"] is True


async def test_research_call_limit_pauses_task_instead_of_overspending(tmp_path):
    pp = SearchProvider()
    ctx = live_ctx(tmp_path, pp, {})
    ctx.settings.update({"limits": {"max_research_calls_per_task": 1}})
    # two distinct research subtasks in one task would need two calls: exercise the gateway guard directly
    from app.budget import BudgetExceeded
    from app.providers.base import AgentRequest, ResearchOptions
    t = ctx.store.create_task("x", None, "auto", {})
    req = AgentRequest(purpose="research", prompt="q", research=ResearchOptions())
    await ctx.gateway.call("perplexity", req, task_id=t["id"])
    with pytest.raises(BudgetExceeded) as e:
        await ctx.gateway.call("perplexity", req, task_id=t["id"])
    assert e.value.kind == "research_calls"
    assert len(pp.calls) == 1


async def test_real_perplexity_adapter_end_to_end_with_mocked_http(tmp_path):
    body = {"id": "r1", "status": "completed", "model": "perplexity/test", "output": [
        {"type": "search_results", "results": [{"url": URL_A, "title": "Docs A", "snippet": EXCERPT_A, "date": "2026-09-01", "last_updated": "2026-09-01"}]},
        {"type": "message", "content": [{"type": "output_text", "text": json.dumps(report())}]}],
        "usage": {"input_tokens": 100, "output_tokens": 50, "cost": {"total_cost": 0.0123}}}
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json=body)

    cfg = Config(mode="live", data_dir=tmp_path / "d", perplexity_api_key="pplx-test0000000000")
    pp = PerplexityProvider(cfg, transport=httpx.MockTransport(handler))
    ctx = build_context(cfg, providers={"openai": MockProvider("openai", "Analysis", "m"), "anthropic": MockProvider("anthropic", "Implementation", "m"),
                                         "perplexity": pp}, fetcher=FakeFetcher({URL_A: EXCERPT_A}), sleep=no_sleep)
    t = await run_task(ctx, REQ)
    assert t["status"] == "completed", t["error"]
    assert len(seen) == 1 and seen[0].url.path == "/v1/agent"
    row = ctx.store.calls(task_id=t["id"], provider="perplexity")[0]
    assert row["cost_usd"] == pytest.approx(0.0123) and row["cost_basis"] == "reported"
    assert any(c["status"] == "verified" for c in ctx.store.claims(t["id"]))
    assert "pplx-test0000000000" not in json.dumps(ctx.store.logs(t["id"]))


async def test_demo_mode_never_marks_claims_verified_or_free_of_label(ctx):
    t = await run_task(ctx, REQ)
    assert all(c["status"] == "unverified" for c in ctx.store.claims(t["id"]))
    assert all(c["cost_basis"] == "demo" for c in ctx.store.calls(task_id=t["id"]))


async def test_manual_claim_override_via_store(ctx):
    t = await run_task(ctx, REQ)
    cid = ctx.store.claims(t["id"])[0]["id"]
    ctx.store.set_claim_status(cid, "conflicting", "manual: reviewer disagrees")
    assert ctx.store.claims(t["id"])[0]["status"] == "conflicting"
