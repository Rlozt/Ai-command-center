"""Local-model adapter for Ollama (https://ollama.com), using its native chat API (`POST /api/chat`).

Runs on the user's own machine: no API key and no per-token charge. The native API is used instead of the
OpenAI-compatible one because it lets every request set `num_ctx`; Ollama's default context window is small
and would silently truncate our long prompts.

Documented shapes used here (docs.ollama.com/api): request `{model, messages, stream, format, options}`;
response `{model, message:{role,content}, done, done_reason, prompt_eval_count, eval_count}`; `GET /api/tags`
lists installed models; errors are `{"error": "..."}` (404 when the model is not pulled).
"""
from __future__ import annotations

import time

import httpx

from ..config import Config
from .base import AgentRequest, AgentResult, Provider, ProviderError, Usage


class LocalProvider(Provider):
    name = "local"
    role = "Local model"
    billable = False  # no API charge; electricity and time are yours
    local = True

    def __init__(self, cfg: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    @property
    def model(self) -> str:
        return self.cfg.local_model

    def is_configured(self) -> bool:
        return bool(self.cfg.local_model)

    def config_detail(self) -> str:
        return "" if self.is_configured() else "Set LOCAL_MODEL (for example llama3.1:8b) and run Ollama"

    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self.cfg.local_base_url.rstrip("/"), transport=self._transport,
                                           headers={"Content-Type": "application/json"})
        return self._http

    async def validate(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, self.config_detail()
        try:
            r = await self.http().get("/api/tags", timeout=10)
        except httpx.HTTPError:
            return False, (f"Could not reach Ollama at {self.cfg.local_base_url}. Install it from https://ollama.com and make sure it is "
                           "running (the Ollama icon in the system tray, or `ollama serve`).")
        if r.status_code >= 400:
            return False, f"Ollama answered HTTP {r.status_code} at {self.cfg.local_base_url}"
        try:
            names = [m.get("name", "") for m in (r.json().get("models") or [])]
        except ValueError:
            return False, "The server at LOCAL_BASE_URL did not answer like Ollama (invalid JSON)"
        want = self.cfg.local_model
        if want in names or (":" not in want and f"{want}:latest" in names):
            return True, f"Ollama is running and model {want} is installed. Nothing was generated and nothing was charged."
        installed = ", ".join(names) or "none"
        return False, f"Ollama is running but model {want} is not installed. Run: ollama pull {want}   (installed: {installed})"

    def _body(self, req: AgentRequest) -> dict:
        messages = ([{"role": "system", "content": req.system}] if req.system else []) + [{"role": "user", "content": req.prompt}]
        body: dict = {"model": self.cfg.local_model, "messages": messages, "stream": False,
                      "options": {"num_ctx": self.cfg.local_num_ctx, "num_predict": req.max_output_tokens, "temperature": 0.2}}
        if req.expect_json and req.purpose != "ping":
            body["format"] = "json"  # constrains output to valid JSON; the schema itself is still checked by the app
        return body

    async def complete(self, req: AgentRequest) -> AgentResult:
        t0 = time.monotonic()
        try:
            r = await self.http().post("/api/chat", json=self._body(req), timeout=req.timeout_s)
        except httpx.TimeoutException as e:
            raise ProviderError("timeout", "The local model did not answer in time. Small models on a CPU can be slow: raise "
                                "'Local model call timeout' in Settings or pick a smaller model.", retryable=True, provider="local") from e
        except httpx.HTTPError as e:
            raise ProviderError("unavailable", f"Could not reach Ollama at {self.cfg.local_base_url}. Is it running? ({e})",
                                retryable=True, provider="local") from e
        if r.status_code >= 400:
            raise self._http_error(r)
        try:
            data = r.json()
        except ValueError as e:
            raise ProviderError("unavailable", "The local server returned a non-JSON response", retryable=True, provider="local") from e
        if data.get("error"):
            raise ProviderError("bad_request", f"Ollama error: {data['error']}", provider="local")
        text = ((data.get("message") or {}).get("content")) or ""
        reason = data.get("done_reason") or ""
        usage = Usage(input_tokens=data.get("prompt_eval_count"), output_tokens=data.get("eval_count"), cost_usd=0.0, cost_basis="local")
        return AgentResult(provider="local", model=data.get("model", self.model), text=text, usage=usage,
                           stop_reason="max_tokens" if reason == "length" else "end_turn",
                           meta={"latency_ms": int((time.monotonic() - t0) * 1000), "done_reason": reason})

    @staticmethod
    def _http_error(r: httpx.Response) -> ProviderError:
        try:
            detail = r.json().get("error", "")
        except Exception:
            detail = ""
        if r.status_code == 404:
            return ProviderError("config", f"Ollama does not have this model installed ({detail or 'HTTP 404'}). Run: ollama pull <LOCAL_MODEL>",
                                 status_code=404, provider="local")
        if r.status_code in (408, 429) or r.status_code >= 500:
            return ProviderError("unavailable", f"Ollama error (HTTP {r.status_code}): {detail}", retryable=True,
                                 status_code=r.status_code, provider="local")
        return ProviderError("bad_request", f"Ollama rejected the request (HTTP {r.status_code}): {detail}", status_code=r.status_code, provider="local")

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
