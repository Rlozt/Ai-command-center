"""Perplexity adapter, using the Agent API (`POST /v1/agent`) over plain HTTPS.

Documented response shape (docs.perplexity.ai/api-reference/agent-post): `output[]` items of type
`message` (content parts of type `output_text`, with `url_citation` annotations) and `search_results`
(results with title/url/snippet/date/last_updated); `usage.cost.total_cost` in USD when reported.
There is no top-level `output_text` field in the raw JSON, so text is assembled from message parts.
"""
from __future__ import annotations

import time

import httpx

from ..config import Config
from .base import AgentRequest, AgentResult, Provider, ProviderError, Source, Usage


class PerplexityProvider(Provider):
    name = "perplexity"
    role = "Research"

    def __init__(self, cfg: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    @property
    def model(self) -> str:
        return self.cfg.perplexity_model or f"preset:{self.cfg.perplexity_preset}"

    def is_configured(self) -> bool:
        return bool(self.cfg.perplexity_api_key)

    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self.cfg.perplexity_base_url.rstrip("/"), transport=self._transport,
                                           headers={"Content-Type": "application/json"})
        return self._http

    def _headers(self) -> dict[str, str]:
        if not self.cfg.perplexity_api_key:
            raise ProviderError("config", "PERPLEXITY_API_KEY is not set. A Perplexity subscription does not include "
                                "API access; create an API key in the Perplexity API console.", provider="perplexity")
        return {"Authorization": f"Bearer {self.cfg.perplexity_api_key}"}

    async def validate(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "PERPLEXITY_API_KEY is not set"
        try:
            r = await self.http().get("/v1/models", headers=self._headers(), timeout=15)
        except httpx.HTTPError as e:
            return False, f"Could not reach Perplexity: {e}"
        if r.status_code in (401, 403):
            return False, "Perplexity rejected the API key"
        if r.status_code >= 400:
            return False, f"Perplexity returned HTTP {r.status_code}"
        return True, ("Perplexity is reachable. The free model-list endpoint may not require authentication, so the key is "
                      "only proven by a real request: use 'Send test request' (a few cents at most) to verify it.")

    def _body(self, req: AgentRequest) -> dict:
        body: dict = {"input": req.prompt, "max_output_tokens": req.max_output_tokens}
        if req.system:
            body["instructions"] = req.system
        if self.cfg.perplexity_model:
            body["model"] = self.cfg.perplexity_model
        else:
            body["preset"] = self.cfg.perplexity_preset
        if req.purpose != "ping" and req.research is not None:
            tool: dict = {"type": "web_search", "max_results": max(1, min(50, req.research.max_results))}
            filters: dict = {}
            if req.research.recency in ("hour", "day", "week", "month", "year"):
                filters["search_recency_filter"] = req.research.recency
            if req.research.domains:
                filters["search_domain_filter"] = req.research.domains[:20]
            if filters:
                tool["filters"] = filters
            body["tools"] = [tool]
        return body

    async def complete(self, req: AgentRequest) -> AgentResult:
        headers = self._headers()
        body = self._body(req)
        t0 = time.monotonic()
        try:
            r = await self.http().post("/v1/agent", json=body, headers=headers, timeout=req.timeout_s)
        except httpx.TimeoutException as e:
            raise ProviderError("timeout", "Perplexity request timed out", retryable=True, provider="perplexity") from e
        except httpx.HTTPError as e:
            raise ProviderError("unavailable", f"Could not reach Perplexity: {e}", retryable=True, provider="perplexity") from e
        if r.status_code >= 400:
            raise self._http_error(r)
        try:
            data = r.json()
        except ValueError as e:
            raise ProviderError("unavailable", "Perplexity returned a non-JSON response", retryable=True, provider="perplexity") from e
        return self._parse(data, int((time.monotonic() - t0) * 1000))

    @staticmethod
    def _http_error(r: httpx.Response) -> ProviderError:
        code = r.status_code
        try:
            detail = (r.json().get("error") or {}).get("message", "")
        except Exception:
            detail = ""
        if code in (401, 403):
            return ProviderError("auth", f"Perplexity rejected the API key (HTTP {code}). Check PERPLEXITY_API_KEY and API credits.",
                                 status_code=code, provider="perplexity")
        if code == 429:
            ra = r.headers.get("retry-after")
            return ProviderError("rate_limit", "Perplexity rate limit exceeded (HTTP 429)", retryable=True,
                                 retry_after=float(ra) if ra and ra.replace(".", "", 1).isdigit() else None,
                                 status_code=code, provider="perplexity")
        if code in (408, 409) or code >= 500:
            return ProviderError("unavailable", f"Perplexity service error (HTTP {code})", retryable=True, status_code=code, provider="perplexity")
        return ProviderError("bad_request", f"Perplexity rejected the request (HTTP {code}): {detail}", status_code=code, provider="perplexity")

    def _parse(self, data: dict, latency_ms: int) -> AgentResult:
        status = data.get("status", "completed")
        if status in ("failed", "cancelled"):
            err = data.get("error") or {}
            raise ProviderError("unavailable", f"Perplexity response {status}: {err.get('message', 'no detail')}", retryable=(status == "failed"), provider="perplexity")
        texts: list[str] = []
        sources: dict[str, Source] = {}
        for item in data.get("output") or []:
            t = item.get("type")
            if t == "message":
                for part in item.get("content") or []:
                    if part.get("type") == "output_text":
                        texts.append(part.get("text", ""))
                        for ann in part.get("annotations") or []:
                            if ann.get("type") == "url_citation" and ann.get("url"):
                                s = sources.setdefault(ann["url"], Source(url=ann["url"]))
                                s.title = s.title or ann.get("title", "")
            elif t == "search_results":
                for res in item.get("results") or []:
                    url = res.get("url")
                    if not url:
                        continue
                    s = sources.setdefault(url, Source(url=url))
                    s.title = res.get("title") or s.title
                    s.snippet = res.get("snippet") or s.snippet
                    s.published = res.get("date") or s.published
                    s.last_updated = res.get("last_updated") or s.last_updated
        u = data.get("usage") or {}
        cost = (u.get("cost") or {}).get("total_cost")
        usage = Usage(input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"),
                      cost_usd=float(cost) if cost is not None else None,
                      cost_basis="reported" if cost is not None else "estimated")
        return AgentResult(provider="perplexity", model=data.get("model", self.model), text="".join(texts), usage=usage,
                           sources=list(sources.values()), response_id=data.get("id", ""),
                           stop_reason="incomplete" if status == "incomplete" else "end_turn",
                           meta={"latency_ms": latency_ms, "status": status})

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
