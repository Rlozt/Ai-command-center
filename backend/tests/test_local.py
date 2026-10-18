"""Local model (Ollama) provider: request shape, errors, accounting as 'local' (never 'demo', never billable), routing."""
import json

import httpx
import pytest

from app.config import Config
from app.context import build_context
from app.providers.base import AgentRequest, ProviderError, Usage
from app.providers.local_provider import LocalProvider
from app.providers.mock import MockProvider
from app.reports import build_report
from app.router import route, route_review
from tests.conftest import FakeFetcher, no_sleep, run_task

OLLAMA_OK = {"model": "llama3.1:8b", "message": {"role": "assistant", "content": '{"ok": true}'}, "done": True, "done_reason": "stop",
             "prompt_eval_count": 120, "eval_count": 30}


def local(handler, **cfg):
    c = Config(mode="live", local_model="llama3.1:8b", **cfg)
    return LocalProvider(c, transport=httpx.MockTransport(handler)), c


async def test_request_shape_and_parsing():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"], seen["body"], seen["auth"] = str(request.url), json.loads(request.content), request.headers.get("authorization")
        return httpx.Response(200, json=OLLAMA_OK)

    p, _ = local(handler, local_num_ctx=8192)
    res = await p.complete(AgentRequest(purpose="analysis", prompt="hi", system="sys", max_output_tokens=500))
    b = seen["body"]
    assert seen["url"] == "http://127.0.0.1:11434/api/chat" and seen["auth"] is None  # no credentials involved
    assert b["model"] == "llama3.1:8b" and b["stream"] is False and b["format"] == "json"
    assert b["options"]["num_ctx"] == 8192 and b["options"]["num_predict"] == 500
    assert [m["role"] for m in b["messages"]] == ["system", "user"]
    assert res.text == '{"ok": true}' and res.usage.cost_usd == 0.0 and res.usage.cost_basis == "local"
    assert res.usage.input_tokens == 120 and res.usage.output_tokens == 30 and res.stop_reason == "end_turn"


async def test_plain_text_requests_do_not_force_json_and_truncation_is_reported():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={**OLLAMA_OK, "done_reason": "length"})

    p, _ = local(handler)
    res = await p.complete(AgentRequest(purpose="ping", prompt="pong?", expect_json=False))
    assert "format" not in seen["body"] and res.stop_reason == "max_tokens"


@pytest.mark.parametrize("status,kind,retryable", [(404, "config", False), (500, "unavailable", True), (400, "bad_request", False)])
async def test_http_errors(status, kind, retryable):
    p, _ = local(lambda r: httpx.Response(status, json={"error": "model 'x' not found"}))
    with pytest.raises(ProviderError) as e:
        await p.complete(AgentRequest(purpose="analysis", prompt="hi"))
    assert e.value.kind == kind and e.value.retryable is retryable
    if status == 404:
        assert "ollama pull" in e.value.message


async def test_connection_refused_and_timeout_messages_are_actionable():
    def refuse(request):
        raise httpx.ConnectError("refused")

    p, _ = local(refuse)
    with pytest.raises(ProviderError) as e:
        await p.complete(AgentRequest(purpose="analysis", prompt="hi"))
    assert e.value.kind == "unavailable" and "Is it running" in e.value.message

    def slow(request):
        raise httpx.ReadTimeout("slow")

    p2, _ = local(slow)
    with pytest.raises(ProviderError) as e2:
        await p2.complete(AgentRequest(purpose="analysis", prompt="hi"))
    assert e2.value.kind == "timeout" and "Local model call timeout" in e2.value.message


async def test_validate_distinguishes_not_running_model_missing_and_ok():
    ok, msg = await local(lambda r: httpx.Response(200, json={"models": [{"name": "llama3.1:8b"}]}))[0].validate()
    assert ok and "nothing was charged" in msg
    ok, msg = await local(lambda r: httpx.Response(200, json={"models": [{"name": "mistral:7b"}]}))[0].validate()
    assert not ok and "ollama pull llama3.1:8b" in msg and "mistral:7b" in msg

    def refuse(request):
        raise httpx.ConnectError("refused")

    ok, msg = await local(refuse)[0].validate()
    assert not ok and "https://ollama.com" in msg
    unset = LocalProvider(Config(mode="live"))
    assert unset.is_configured() is False and "LOCAL_MODEL" in (await unset.validate())[1]


def live_ctx(tmp_path, local_provider, **enabled):
    cfg = Config(mode="live", data_dir=tmp_path / "d", local_model="llama3.1:8b")
    providers = {"openai": MockProvider("openai", "Analysis", "m"), "anthropic": MockProvider("anthropic", "Implementation", "m"),
                 "perplexity": MockProvider("perplexity", "Research", "m"), "local": local_provider}
    ctx = build_context(cfg, providers=providers, fetcher=FakeFetcher(), sleep=no_sleep)
    ctx.settings.update({"providers_enabled": {"openai": False, "anthropic": False, "perplexity": False, "local": True, **enabled}})
    return ctx


class DelegatingLocal(LocalProvider):
    """Runs the full pipeline offline: answers come from the demo mock, but accounting/routing see a real LocalProvider."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.mock = MockProvider("local", "Local model", "llama3.1:8b")
        self.calls = self.mock.calls

    async def complete(self, req):
        res = await self.mock.complete(req)
        res.usage = Usage(res.usage.input_tokens, res.usage.output_tokens, 0.0, "local")
        res.sources = []
        return res


async def test_local_only_task_runs_end_to_end_and_is_never_labelled_demo_or_billable(tmp_path):
    cfg = Config(mode="live", data_dir=tmp_path / "d", local_model="llama3.1:8b")
    ctx = live_ctx(tmp_path, DelegatingLocal(cfg))
    t = await run_task(ctx, "Research the current state of local-first software and summarize it")
    assert t["status"] == "completed", t["error"]
    rep = t["report"]
    assert rep["usage"]["billable_calls"] == 0 and rep["usage"]["local_calls"] >= 3 and rep["usage"]["demo"] is False
    assert "local model" in rep["cost_statement"] and "no API charges" in rep["cost_statement"] and "Demo" not in rep["cost_statement"]
    assert any("local model" in c for c in rep["caveats"]) and not any("Demo mode" in c for c in rep["caveats"])
    assert any("WITHOUT live web access" in c for c in rep["caveats"])  # research can't use the web on a local model
    assert {s["agent"] for s in ctx.store.subtasks(t["id"])} == {"local"}
    rows = ctx.store.calls(task_id=t["id"])
    assert rows and all(r["cost_basis"] == "local" and not r["billable"] and r["cost_usd"] == 0 for r in rows)
    assert ctx.store.sources(t["id"]) == []
    brief = ctx.engine.store.task_usage(t["id"])
    assert brief["demo"] is False


async def test_local_implementation_is_staged_for_approval_and_review_is_flagged_not_independent(tmp_path, project_dir):
    cfg = Config(mode="live", data_dir=tmp_path / "d", local_model="llama3.1:8b")
    ctx = live_ctx(tmp_path, DelegatingLocal(cfg))
    project = ctx.store.create_project("P", root_dir=str(project_dir), test_command="python -m unittest discover -s demo_output")
    t = await run_task(ctx, "Write a python function that greets people", project["id"])
    assert t["status"] == "awaiting_approval", t["error"]
    assert list(project_dir.iterdir()) == []  # still nothing applied without approval
    assert ctx.store.reviews(t["id"])[0]["reviewer"] == "local"
    assert "NOT independent" in ctx.store.reviews(t["id"])[0]["summary"]  # self-review is disclosed, never presented as independent


def test_routing_prefers_paid_providers_and_only_falls_back_to_local():
    assert route("implementation", {"anthropic", "local"}).provider == "anthropic"
    assert route("implementation", {"local"}).provider == "local"
    r = route("research", {"local"})
    assert r.provider == "local" and r.no_web_access is True
    r2 = route("research", {"perplexity", "local"})
    assert r2.provider == "perplexity" and r2.no_web_access is False
    assert route_review("anthropic", {"anthropic", "local"}).provider == "local"
    assert route_review("local", {"local"}).provider == "local" and "NOT independent" in route_review("local", {"local"}).note
    assert route_review("local", {"local", "openai"}).provider == "openai"


async def test_local_timeout_uses_the_local_cap_not_the_cloud_cap(tmp_path):
    cfg = Config(mode="live", data_dir=tmp_path / "d", local_model="m")
    lp = DelegatingLocal(cfg)
    ctx = live_ctx(tmp_path, lp)
    ctx.settings.update({"limits": {"call_timeout_s": 30, "local_call_timeout_s": 900}})
    await ctx.gateway.call("local", AgentRequest(purpose="analysis", prompt="x", timeout_s=120))
    assert lp.mock.calls[-1].timeout_s == 900


async def test_local_failures_are_recorded_as_local_not_demo(tmp_path):
    def refuse(request):
        raise httpx.ConnectError("refused")

    ctx = live_ctx(tmp_path, local(refuse)[0])
    ctx.settings.update({"limits": {"max_retries_per_call": 0}})
    with pytest.raises(ProviderError):
        await ctx.gateway.call("local", AgentRequest(purpose="analysis", prompt="x"))
    row = ctx.store.calls()[0]
    assert row["status"] == "error" and row["cost_basis"] == "local" and not row["billable"]


def test_local_is_opt_in_unless_local_model_is_configured(tmp_path):
    ctx = build_context(Config(mode="live", data_dir=tmp_path / "a"), providers={}, sleep=no_sleep)
    assert ctx.settings.get_key("providers_enabled")["local"] is False
    ctx2 = build_context(Config(mode="live", data_dir=tmp_path / "b", local_model="llama3.1:8b"), providers={}, sleep=no_sleep)
    assert ctx2.settings.get_key("providers_enabled")["local"] is True
    ctx2.settings.update({"providers_enabled": {"openai": True, "anthropic": True, "perplexity": True, "local": False}})
    ctx3 = build_context(Config(mode="live", data_dir=tmp_path / "b", local_model="llama3.1:8b"), providers={}, sleep=no_sleep)
    assert ctx3.settings.get_key("providers_enabled")["local"] is False  # an explicit saved choice is never overridden


def test_demo_mode_unaffected_local_mock_is_disabled(ctx):
    assert ctx.settings.get_key("providers_enabled")["local"] is False
    assert "local" not in ctx.registry.available()
