"""Adapter behaviour against fake SDK clients / mocked HTTP. No network and no paid calls."""
import json
from types import SimpleNamespace as NS

import anthropic
import httpx
import httpx2
import openai
import pytest

from app.config import Config
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import AgentRequest, ProviderError, ResearchOptions, extract_json
from app.providers.openai_provider import OpenAIProvider
from app.providers.perplexity_provider import PerplexityProvider


def req(**kw):
    return AgentRequest(purpose="analysis", prompt="hello", system="sys", **kw)


# ---------------------------------------------------------------- OpenAI
class FakeOpenAI:
    def __init__(self, result=None, exc=None):
        self.calls = []
        self.models = NS(list=self._list)
        self.responses = NS(create=self._create)
        self.result, self.exc = result, exc

    async def _list(self):
        if self.exc:
            raise self.exc
        return []

    async def _create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return self.result


def oa_resp(text="hi", status="completed", **kw):
    return NS(output_text=text, status=status, id="resp_1", model="gpt-x", output=[],
              usage=NS(input_tokens=11, output_tokens=7), incomplete_details=kw.get("incomplete"), error=None)


def oa_err(cls, code, headers=None):
    r = httpx.Response(code, request=httpx.Request("POST", "https://api.openai.com/v1/responses"), headers=headers or {})
    return cls("boom", response=r, body=None)


async def test_openai_success_uses_responses_api_and_reports_usage():
    fake = FakeOpenAI(oa_resp("answer"))
    p = OpenAIProvider(Config(openai_api_key="sk-test0000000000000000"), client=fake)
    res = await p.complete(req(max_output_tokens=500))
    assert res.text == "answer" and res.usage.input_tokens == 11 and res.usage.output_tokens == 7
    kw = fake.calls[0]
    assert kw["instructions"] == "sys" and kw["input"] == "hello" and kw["max_output_tokens"] == 500 and kw["store"] is False


async def test_openai_error_mapping():
    cases = [(oa_err(openai.AuthenticationError, 401), "auth", False), (oa_err(openai.RateLimitError, 429, {"retry-after": "2"}), "rate_limit", True),
             (oa_err(openai.InternalServerError, 500), "unavailable", True), (oa_err(openai.BadRequestError, 400), "bad_request", False),
             (openai.APITimeoutError(request=httpx.Request("POST", "https://x")), "timeout", True)]
    for exc, kind, retryable in cases:
        p = OpenAIProvider(Config(openai_api_key="sk-test0000000000000000"), client=FakeOpenAI(exc=exc))
        with pytest.raises(ProviderError) as e:
            await p.complete(req())
        assert e.value.kind == kind and e.value.retryable is retryable
    p = OpenAIProvider(Config(openai_api_key="k"), client=FakeOpenAI(exc=oa_err(openai.RateLimitError, 429, {"retry-after": "2"})))
    with pytest.raises(ProviderError) as e:
        await p.complete(req())
    assert e.value.retry_after == 2.0


async def test_openai_missing_key_is_a_config_error_not_a_crash():
    p = OpenAIProvider(Config())
    assert not p.is_configured()
    ok, msg = await p.validate()
    assert not ok and "OPENAI_API_KEY" in msg
    with pytest.raises(ProviderError) as e:
        await p.complete(req())
    assert e.value.kind == "config"


async def test_openai_truncated_output_is_flagged():
    p = OpenAIProvider(Config(openai_api_key="k"), client=FakeOpenAI(oa_resp("par", status="incomplete", incomplete=NS(reason="max_output_tokens"))))
    assert (await p.complete(req())).stop_reason == "max_output_tokens"


async def test_openai_validate_uses_free_models_call():
    p = OpenAIProvider(Config(openai_api_key="k"), client=FakeOpenAI())
    ok, _ = await p.validate()
    assert ok
    bad = OpenAIProvider(Config(openai_api_key="k"), client=FakeOpenAI(exc=oa_err(openai.AuthenticationError, 401)))
    ok, msg = await bad.validate()
    assert not ok and "rejected" in msg


# ---------------------------------------------------------------- Anthropic
class FakeAnthropic:
    def __init__(self, result=None, exc=None, retrieve_exc=None):
        self.calls = []
        self.messages = NS(create=self._create)
        self.models = NS(retrieve=self._retrieve)
        self.result, self.exc, self.retrieve_exc = result, exc, retrieve_exc

    async def _retrieve(self, model):
        if self.retrieve_exc:
            raise self.retrieve_exc
        return NS(id=model)

    async def _create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return self.result


def an_resp(text="hi", stop="end_turn"):
    return NS(content=[NS(type="thinking", thinking=""), NS(type="text", text=text)], stop_reason=stop, id="msg_1", model="claude-x",
              usage=NS(input_tokens=20, output_tokens=9), stop_details=None)


def an_err(cls, code, headers=None):
    r = httpx2.Response(code, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"), headers=headers or {})
    return cls("boom", response=r, body=None)


async def test_anthropic_success_joins_text_blocks_and_passes_params():
    fake = FakeAnthropic(an_resp("done"))
    p = AnthropicProvider(Config(anthropic_api_key="sk-ant-test000000000000", anthropic_model="claude-opus-5-5", anthropic_effort="medium"), client=fake)
    res = await p.complete(req(max_output_tokens=900))
    assert res.text == "done" and res.usage.input_tokens == 20
    kw = fake.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["max_tokens"] == 900 and kw["system"] == "sys"
    assert kw["output_config"] == {"effort": "medium"} and "thinking" not in kw and "temperature" not in kw


async def test_anthropic_error_mapping_and_refusal():
    for exc, kind, retryable in [(an_err(anthropic.AuthenticationError, 401), "auth", False), (an_err(anthropic.RateLimitError, 429), "rate_limit", True),
                                 (an_err(anthropic.InternalServerError, 529), "unavailable", True), (an_err(anthropic.BadRequestError, 400), "bad_request", False)]:
        p = AnthropicProvider(Config(anthropic_api_key="k"), client=FakeAnthropic(exc=exc))
        with pytest.raises(ProviderError) as e:
            await p.complete(req())
        assert (e.value.kind, e.value.retryable) == (kind, retryable)
    p = AnthropicProvider(Config(anthropic_api_key="k"), client=FakeAnthropic(an_resp("x", stop="refusal")))
    with pytest.raises(ProviderError) as e:
        await p.complete(req())
    assert e.value.kind == "refused"


async def test_anthropic_validate_checks_key_and_model_id():
    ok, msg = await AnthropicProvider(Config(anthropic_api_key="k", anthropic_model="claude-opus-5-5"), client=FakeAnthropic()).validate()
    assert ok
    nf = AnthropicProvider(Config(anthropic_api_key="k", anthropic_model="nope"), client=FakeAnthropic(retrieve_exc=an_err(anthropic.NotFoundError, 404)))
    ok, msg = await nf.validate()
    assert not ok and "nope" in msg
    ok, msg = await AnthropicProvider(Config()).validate()
    assert not ok and "ANTHROPIC_API_KEY" in msg


# ---------------------------------------------------------------- Perplexity
PPLX_BODY = {
    "id": "resp_p1", "object": "response", "model": "openai/gpt-x", "status": "completed",
    "output": [
        {"type": "search_results", "queries": ["q"], "results": [
            {"id": 1, "url": "https://docs.example.org/a", "title": "Docs A", "snippet": "A says X.", "date": "2026-09-01", "last_updated": "2026-09-10", "source": "web"},
            {"id": 2, "url": "https://blog.example.org/b", "title": "Blog B", "snippet": "B says Y.", "date": None, "last_updated": None, "source": "web"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "{\"ok\": true}",
            "annotations": [{"type": "url_citation", "url": "https://docs.example.org/a", "title": "Docs A", "start_index": 0, "end_index": 3}]}]},
    ],
    "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "cost": {"input_cost": 0.001, "output_cost": 0.002, "total_cost": 0.0123, "currency": "USD"}},
}


def pplx(handler, **cfg):
    c = Config(perplexity_api_key="pplx-test0000000000", **cfg)
    return PerplexityProvider(c, transport=httpx.MockTransport(handler)), c


async def test_perplexity_request_shape_and_response_parsing():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=PPLX_BODY)

    p, _ = pplx(handler, perplexity_preset="low")
    res = await p.complete(AgentRequest(purpose="research", prompt="q", system="s", max_output_tokens=1000,
                                        research=ResearchOptions(recency="year", domains=["example.org"], max_results=5)))
    assert seen["url"] == "https://api.perplexity.ai/v1/agent" and seen["auth"] == "Bearer pplx-test0000000000"
    b = seen["body"]
    assert b["preset"] == "low" and "model" not in b and b["max_output_tokens"] == 1000 and b["instructions"] == "s"
    assert b["tools"] == [{"type": "web_search", "max_results": 5, "filters": {"search_recency_filter": "year", "search_domain_filter": ["example.org"]}}]
    assert res.text == '{"ok": true}'
    assert [s.url for s in res.sources] == ["https://docs.example.org/a", "https://blog.example.org/b"]
    assert res.sources[0].published == "2026-09-01" and res.sources[0].snippet == "A says X."
    assert res.usage.cost_usd == 0.0123 and res.usage.cost_basis == "reported" and res.usage.input_tokens == 100


async def test_perplexity_model_override_replaces_preset():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=PPLX_BODY)

    p, _ = pplx(handler, perplexity_model="perplexity/sonar")
    await p.complete(AgentRequest(purpose="research", prompt="q", research=ResearchOptions()))
    assert seen["body"]["model"] == "perplexity/sonar" and "preset" not in seen["body"]


async def test_perplexity_without_cost_falls_back_to_estimated_basis():
    body = json.loads(json.dumps(PPLX_BODY))
    del body["usage"]["cost"]
    p, _ = pplx(lambda r: httpx.Response(200, json=body))
    res = await p.complete(AgentRequest(purpose="research", prompt="q", research=ResearchOptions()))
    assert res.usage.cost_usd is None and res.usage.cost_basis == "estimated"


@pytest.mark.parametrize("status,kind,retryable", [(401, "auth", False), (403, "auth", False), (429, "rate_limit", True), (500, "unavailable", True),
                                                     (503, "unavailable", True), (400, "bad_request", False)])
async def test_perplexity_http_errors(status, kind, retryable):
    p, _ = pplx(lambda r: httpx.Response(status, json={"error": {"message": "nope"}}, headers={"retry-after": "4"} if status == 429 else {}))
    with pytest.raises(ProviderError) as e:
        await p.complete(AgentRequest(purpose="research", prompt="q"))
    assert (e.value.kind, e.value.retryable) == (kind, retryable)
    if status == 429:
        assert e.value.retry_after == 4.0


async def test_perplexity_timeout_and_connection_errors():
    def boom(kind):
        def handler(request):
            raise kind("x", request=request)
        return handler

    p, _ = pplx(boom(httpx.ReadTimeout))
    with pytest.raises(ProviderError) as e:
        await p.complete(AgentRequest(purpose="research", prompt="q"))
    assert e.value.kind == "timeout" and e.value.retryable
    p, _ = pplx(boom(httpx.ConnectError))
    with pytest.raises(ProviderError) as e:
        await p.complete(AgentRequest(purpose="research", prompt="q"))
    assert e.value.kind == "unavailable"


async def test_perplexity_failed_status_and_missing_key():
    p, _ = pplx(lambda r: httpx.Response(200, json={"status": "failed", "error": {"message": "overloaded"}, "output": []}))
    with pytest.raises(ProviderError) as e:
        await p.complete(AgentRequest(purpose="research", prompt="q"))
    assert "overloaded" in str(e.value)
    nokey = PerplexityProvider(Config())
    assert not nokey.is_configured()
    ok, msg = await nokey.validate()
    assert not ok and "PERPLEXITY_API_KEY" in msg
    with pytest.raises(ProviderError) as e:
        await nokey.complete(AgentRequest(purpose="research", prompt="q"))
    assert e.value.kind == "config" and "subscription" in e.value.message


async def test_perplexity_validate_does_not_claim_the_key_is_proven():
    p, _ = pplx(lambda r: httpx.Response(200, json={"object": "list", "data": []}))
    ok, msg = await p.validate()
    assert ok and "only proven by a real request" in msg
    p, _ = pplx(lambda r: httpx.Response(401, json={}))
    assert (await p.validate())[0] is False


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is: {"a": {"b": "}"}} thanks') == {"a": {"b": "}"}}
    with pytest.raises(ValueError):
        extract_json("no json here")
