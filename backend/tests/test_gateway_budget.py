import asyncio

import pytest

from app.budget import BudgetExceeded
from app.config import Config
from app.context import build_context
from app.providers.base import AgentRequest, ProviderError
from app.schemas import ReviewOutput
from tests.conftest import no_sleep
from tests.helpers import ScriptedProvider


def make(tmp_path, providers, sleeps=None):
    async def rec(d):
        if sleeps is not None:
            sleeps.append(d)
    cfg = Config(mode="live", data_dir=tmp_path / "d")
    return build_context(cfg, providers=providers, sleep=rec)


def r(purpose="analysis", **kw):
    return AgentRequest(purpose=purpose, prompt="hi", **kw)


async def test_success_records_call_tokens_and_estimated_cost(tmp_path):
    p = ScriptedProvider(script=["ok"], in_tok=1_000_000, out_tok=100_000)  # gpt-6.1-sol: $2 in, $10 out per 1M
    ctx = make(tmp_path, {"openai": p})
    res = await ctx.gateway.call("openai", r())
    assert res.text == "ok"
    row = ctx.store.calls()[0]
    assert row["status"] == "ok" and row["billable"] and row["cost_basis"] == "estimated"
    assert row["cost_usd"] == pytest.approx(3.0)
    assert row["input_tokens"] == 1_000_000


async def test_provider_reported_cost_wins_over_estimate(tmp_path):
    ctx = make(tmp_path, {"perplexity": ScriptedProvider("perplexity", "Research", cost=0.0123)})
    await ctx.gateway.call("perplexity", r("research"))
    row = ctx.store.calls()[0]
    assert row["cost_usd"] == 0.0123 and row["cost_basis"] == "reported"


async def test_retries_are_bounded_and_back_off(tmp_path):
    sleeps = []
    err = ProviderError("rate_limit", "429", retryable=True, provider="openai")
    p = ScriptedProvider(script=[err])
    ctx = make(tmp_path, {"openai": p}, sleeps)
    ctx.settings.update({"limits": {"max_retries_per_call": 2}})
    with pytest.raises(ProviderError) as e:
        await ctx.gateway.call("openai", r())
    assert e.value.kind == "rate_limit"
    assert len(p.calls) == 3  # 1 try + 2 retries, then stop
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]  # exponential backoff
    assert [c["status"] for c in ctx.store.calls()] == ["error"] * 3


async def test_retry_after_header_is_respected_and_transient_error_recovers(tmp_path):
    sleeps = []
    p = ScriptedProvider(script=[ProviderError("rate_limit", "429", retryable=True, retry_after=7.0), "fine"])
    ctx = make(tmp_path, {"openai": p}, sleeps)
    assert (await ctx.gateway.call("openai", r())).text == "fine"
    assert sleeps == [7.0]


async def test_non_retryable_errors_are_not_retried(tmp_path):
    p = ScriptedProvider(script=[ProviderError("auth", "bad key")])
    ctx = make(tmp_path, {"openai": p})
    with pytest.raises(ProviderError) as e:
        await ctx.gateway.call("openai", r())
    assert e.value.kind == "auth" and len(p.calls) == 1


async def test_hung_provider_times_out_and_is_retried_within_limit(tmp_path):
    p = ScriptedProvider(script=["hang"])
    ctx = make(tmp_path, {"openai": p})
    ctx.settings.update({"limits": {"max_retries_per_call": 1}})
    with pytest.raises(ProviderError) as e:
        await ctx.gateway.call("openai", r(timeout_s=0.05))
    assert e.value.kind == "timeout" and len(p.calls) == 2


async def test_disabled_provider_is_refused(tmp_path):
    p = ScriptedProvider(script=["ok"])
    ctx = make(tmp_path, {"openai": p})
    ctx.settings.update({"providers_enabled": {"openai": False}})
    with pytest.raises(ProviderError) as e:
        await ctx.gateway.call("openai", r())
    assert e.value.kind == "config" and p.calls == []


async def test_cancellation_propagates_into_the_in_flight_call(tmp_path):
    p = ScriptedProvider(script=["hang"])
    ctx = make(tmp_path, {"openai": p})
    t = asyncio.create_task(ctx.gateway.call("openai", r(timeout_s=30)))
    await asyncio.sleep(0.05)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert ctx.store.calls()[0]["error_kind"] == "cancelled"


# ---------------------------------------------------------------- budget
async def test_per_task_limit_is_a_hard_stop_before_the_request_is_sent(tmp_path):
    p = ScriptedProvider(script=["ok"], in_tok=1_000_000, out_tok=0)  # each call costs $2.00
    ctx = make(tmp_path, {"openai": p})
    ctx.settings.update({"limits": {"per_task_usd": 3.0, "daily_usd": 100.0, "max_output_tokens": 5_000_000}})
    t = ctx.store.create_task("budget test", None, "auto", {})
    await ctx.gateway.call("openai", r(max_output_tokens=10), task_id=t["id"])
    with pytest.raises(BudgetExceeded) as e:
        await ctx.gateway.call("openai", r(max_output_tokens=2_000_000), task_id=t["id"])  # worst case blows the limit
    assert e.value.kind == "per_task"
    assert len(p.calls) == 1  # the second request was never sent


async def test_daily_limit_applies_across_tasks(tmp_path):
    p = ScriptedProvider(script=["ok"], in_tok=1_000_000, out_tok=0)
    ctx = make(tmp_path, {"openai": p})
    ctx.settings.update({"limits": {"per_task_usd": 100, "daily_usd": 2.5, "per_provider_daily_usd": {"openai": 100}, "max_output_tokens": 5_000_000}})
    a, b = ctx.store.create_task("a task", None, "auto", {}), ctx.store.create_task("b task", None, "auto", {})
    await ctx.gateway.call("openai", r(max_output_tokens=10), task_id=a["id"])
    with pytest.raises(BudgetExceeded) as e:
        await ctx.gateway.call("openai", r(max_output_tokens=500_000), task_id=b["id"])
    assert e.value.kind == "daily"


async def test_per_provider_limit(tmp_path):
    p = ScriptedProvider("perplexity", "Research", script=["ok"], cost=0.6)
    ctx = make(tmp_path, {"perplexity": p})
    ctx.settings.update({"limits": {"per_provider_daily_usd": {"perplexity": 1.0}, "daily_usd": 100, "per_task_usd": 100, "max_output_tokens": 5_000_000}})
    await ctx.gateway.call("perplexity", r("research", max_output_tokens=10))
    await ctx.gateway.call("perplexity", r("research", max_output_tokens=10))
    with pytest.raises(BudgetExceeded) as e:
        await ctx.gateway.call("perplexity", r("research", max_output_tokens=300_000))
    assert e.value.kind == "provider"


async def test_overall_limit(tmp_path):
    p = ScriptedProvider(script=["ok"], cost=1.0)
    ctx = make(tmp_path, {"openai": p})
    ctx.settings.update({"limits": {"overall_usd": 1.5, "daily_usd": 100, "per_task_usd": 100, "per_provider_daily_usd": {"openai": 100}, "max_output_tokens": 5_000_000}})
    await ctx.gateway.call("openai", r(max_output_tokens=10))
    with pytest.raises(BudgetExceeded) as e:
        await ctx.gateway.call("openai", r(max_output_tokens=200_000))
    assert e.value.kind == "overall"


async def test_max_calls_per_task_includes_retries(tmp_path):
    p = ScriptedProvider(script=["ok"], in_tok=1, out_tok=1)
    ctx = make(tmp_path, {"openai": p})
    ctx.settings.update({"limits": {"max_calls_per_task": 2}})
    t = ctx.store.create_task("calls test", None, "auto", {})
    await ctx.gateway.call("openai", r(), task_id=t["id"])
    await ctx.gateway.call("openai", r(), task_id=t["id"])
    with pytest.raises(BudgetExceeded) as e:
        await ctx.gateway.call("openai", r(), task_id=t["id"])
    assert e.value.kind == "calls"


async def test_research_call_limit(tmp_path):
    p = ScriptedProvider("perplexity", "Research", script=["ok"], in_tok=1, out_tok=1)
    ctx = make(tmp_path, {"perplexity": p})
    ctx.settings.update({"limits": {"max_research_calls_per_task": 1}})
    t = ctx.store.create_task("research limit", None, "auto", {})
    await ctx.gateway.call("perplexity", r("research"), task_id=t["id"])
    with pytest.raises(BudgetExceeded) as e:
        await ctx.gateway.call("perplexity", r("research"), task_id=t["id"])
    assert e.value.kind == "research_calls"


async def test_unknown_model_uses_conservative_price(tmp_path):
    p = ScriptedProvider(model="brand-new-model", script=["ok"], in_tok=1_000_000, out_tok=0)
    ctx = make(tmp_path, {"openai": p})
    await ctx.gateway.call("openai", r(max_output_tokens=10))
    assert ctx.store.calls()[0]["cost_usd"] == pytest.approx(10.0)  # unknown_model_price input $10/1M


async def test_demo_providers_never_count_toward_spend(ctx):
    await ctx.gateway.call("openai", r())
    row = ctx.store.calls()[0]
    assert row["billable"] is False and row["cost_usd"] == 0.0 and row["cost_basis"] == "demo"
    assert ctx.budget.spent_total() == 0
    assert ctx.budget.summary()["demo_calls_today"] == 1


async def test_settings_validation_rejects_nonsense(ctx):
    for bad in ({"limits": {"per_task_usd": -1}}, {"limits": {"nope": 1}}, {"routing_mode": "chaos"}, {"providers_enabled": {"gemini": True}}, {"unknown": 1}):
        with pytest.raises(ValueError):
            ctx.settings.update(bad)


# ---------------------------------------------------------------- structured output
GOOD = '{"verdict":"pass","summary":"ok","checks_performed":["read it"],"issues":[]}'


async def test_structured_call_repairs_once_then_succeeds(tmp_path):
    p = ScriptedProvider(script=["not json at all", GOOD])
    ctx = make(tmp_path, {"openai": p})
    out, _ = await ctx.gateway.call_structured("openai", r("review"), ReviewOutput)
    assert out.verdict == "pass" and len(p.calls) == 2
    assert "rejected" in p.calls[1].prompt


async def test_structured_call_gives_up_after_one_repair(tmp_path):
    p = ScriptedProvider(script=["nope"])
    ctx = make(tmp_path, {"openai": p})
    with pytest.raises(ProviderError) as e:
        await ctx.gateway.call_structured("openai", r("review"), ReviewOutput)
    assert e.value.kind == "invalid_output" and len(p.calls) == 2


async def test_reviewer_must_provide_evidence_for_a_failing_verdict(tmp_path):
    lazy = '{"verdict":"fail","summary":"bad","checks_performed":[],"issues":[{"severity":"major","description":"it is wrong","evidence":""}]}'
    p = ScriptedProvider(script=[lazy, lazy])
    ctx = make(tmp_path, {"openai": p})
    with pytest.raises(ProviderError):
        await ctx.gateway.call_structured("openai", r("review"), ReviewOutput)
    lazy_pass = '{"verdict":"pass","summary":"lgtm","checks_performed":[],"issues":[]}'
    p2 = ScriptedProvider(script=[lazy_pass])
    ctx2 = make(tmp_path / "x", {"openai": p2})
    with pytest.raises(ProviderError):
        await ctx2.gateway.call_structured("openai", r("review"), ReviewOutput)


async def test_truncated_structured_output_is_not_accepted(tmp_path):
    from app.providers.base import AgentResult, Usage

    class Trunc(ScriptedProvider):
        async def complete(self, req):
            return AgentResult("openai", "m", GOOD, Usage(1, 1), stop_reason="max_output_tokens")

    ctx = make(tmp_path, {"openai": Trunc()})
    with pytest.raises(ProviderError) as e:
        await ctx.gateway.call_structured("openai", r("review"), ReviewOutput)
    assert e.value.kind == "invalid_output"
