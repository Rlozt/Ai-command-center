"""Ollama adapter (https://ollama.com), using its native chat API (`POST /api/chat`). Works two ways:

* **Local**: a model running on the user's own machine (`LOCAL_BASE_URL=http://127.0.0.1:11434`, the default).
  Nothing leaves the computer.
* **Ollama cloud**: a model name ending in `:cloud` served through the local Ollama app after `ollama signin`, or
  direct calls to `https://ollama.com` with an `OLLAMA_API_KEY`. Prompts ARE sent to Ollama's servers, so these calls
  are recorded as `ollama_cloud` and labelled as such everywhere.

The native API is used instead of the OpenAI-compatible one because it lets every request set `num_ctx`; Ollama's
default context window is small and would silently truncate our long prompts.

Shapes used (docs.ollama.com/api, read 2026-10-18): request `{model, messages, stream, format, options}`; response
`{model, message:{role,content}, done, done_reason, prompt_eval_count, eval_count}`; `GET /api/tags` lists models
(`name`/`model`); errors are `{"error": "..."}` with 400/404/429/500/502; cloud requests send `Authorization: Bearer`.
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx

from ..config import Config
from .base import AgentRequest, AgentResult, Provider, ProviderError, Usage

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}


class LocalProvider(Provider):
    name = "local"
    role = "Ollama"
    billable = False  # not billed by this app (local: your electricity; cloud: your Ollama plan's own limits)
    local = True

    def __init__(self, cfg: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    @property
    def model(self) -> str:
        return self.cfg.local_model

    @property
    def is_cloud(self) -> bool:
        host = (urlparse(self.cfg.local_base_url).hostname or "").lower()
        return self.cfg.local_model.endswith(":cloud") or host not in _LOOPBACK

    @property
    def free_basis(self) -> str:
        return "ollama_cloud" if self.is_cloud else "local"

    def _direct_cloud(self) -> bool:
        return (urlparse(self.cfg.local_base_url).hostname or "").lower() not in _LOOPBACK

    def is_configured(self) -> bool:
        if not self.cfg.local_model:
            return False
        return bool(self.cfg.local_api_key) if self._direct_cloud() else True

    def config_detail(self) -> str:
        if not self.cfg.local_model:
            return "Set LOCAL_MODEL (for example llama3.2:3b) and run Ollama"
        if self._direct_cloud() and not self.cfg.local_api_key:
            return "Direct Ollama cloud calls need OLLAMA_API_KEY (create one at https://ollama.com/settings/keys)"
        if self.is_cloud:
            return "Ollama CLOUD model: prompts are sent to Ollama's servers"
        return ""

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.local_api_key}"} if self.cfg.local_api_key else {}

    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self.cfg.local_base_url.rstrip("/"), transport=self._transport,
                                           headers={"Content-Type": "application/json"})
        return self._http

    async def validate(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, self.config_detail()
        try:
            r = await self.http().get("/api/tags", headers=self._headers(), timeout=15)
        except httpx.HTTPError:
            where = self.cfg.local_base_url
            hint = ("Check your internet connection." if self._direct_cloud() else
                    "Install it from https://ollama.com and make sure it is running (the Ollama icon in the system tray, or `ollama serve`).")
            return False, f"Could not reach Ollama at {where}. {hint}"
        if r.status_code in (401, 403):
            return False, "Ollama rejected the credentials. For direct cloud calls check OLLAMA_API_KEY; otherwise run `ollama signin`."
        if r.status_code >= 400:
            return False, f"Ollama answered HTTP {r.status_code} at {self.cfg.local_base_url}"
        try:
            models = r.json().get("models") or []
        except ValueError:
            return False, "The server at LOCAL_BASE_URL did not answer like Ollama (invalid JSON)"
        names = {m.get(k, "") for m in models for k in ("name", "model")} - {""}
        want = self.cfg.local_model
        if want in names or (":" not in want and f"{want}:latest" in names):
            tail = (" This is a CLOUD model: prompts are sent to Ollama's servers." if self.is_cloud else "")
            return True, f"Ollama is reachable and model {want} is available. Nothing was generated and nothing was charged.{tail}"
        installed = ", ".join(sorted(names)) or "none"
        if want.endswith(":cloud") and not self._direct_cloud():
            fix = f"Run `ollama signin`, then `ollama run {want}` once so it appears here"
        elif self._direct_cloud():
            fix = "Use a model name from the cloud model list (direct API calls use names like gemma4:31b, not gemma4:cloud)"
        else:
            fix = f"Run: ollama pull {want}"
        return False, f"Ollama is reachable but model {want} is not available. {fix}. (available: {installed})"

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
            r = await self.http().post("/api/chat", json=self._body(req), headers=self._headers(), timeout=req.timeout_s)
        except httpx.TimeoutException as e:
            raise ProviderError("timeout", "The Ollama model did not answer in time. Small models on a CPU can be slow: raise "
                                "'Ollama call timeout' in Settings or pick a smaller model.", retryable=True, provider="local") from e
        except httpx.HTTPError as e:
            raise ProviderError("unavailable", f"Could not reach Ollama at {self.cfg.local_base_url}. Is it running? ({e})",
                                retryable=True, provider="local") from e
        if r.status_code >= 400:
            raise self._http_error(r)
        try:
            data = r.json()
        except ValueError as e:
            raise ProviderError("unavailable", "The Ollama server returned a non-JSON response", retryable=True, provider="local") from e
        if data.get("error"):
            raise ProviderError("bad_request", f"Ollama error: {data['error']}", provider="local")
        text = ((data.get("message") or {}).get("content")) or ""
        reason = data.get("done_reason") or ""
        usage = Usage(input_tokens=data.get("prompt_eval_count"), output_tokens=data.get("eval_count"), cost_usd=0.0, cost_basis=self.free_basis)
        return AgentResult(provider="local", model=data.get("model", self.model), text=text, usage=usage,
                           stop_reason="max_tokens" if reason == "length" else "end_turn",
                           meta={"latency_ms": int((time.monotonic() - t0) * 1000), "done_reason": reason, "cloud": self.is_cloud})

    @staticmethod
    def _http_error(r: httpx.Response) -> ProviderError:
        try:
            detail = r.json().get("error", "")
        except Exception:
            detail = ""
        code = r.status_code
        if code in (401, 403):
            return ProviderError("auth", "Ollama rejected the credentials. Run `ollama signin` (cloud models via the Ollama app) or check "
                                 "OLLAMA_API_KEY (direct cloud calls).", status_code=code, provider="local")
        if code == 404:
            return ProviderError("config", f"Ollama does not have this model ({detail or 'HTTP 404'}). Local: `ollama pull <model>`; "
                                 "cloud: `ollama signin` and use a model name from Ollama's list.", status_code=404, provider="local")
        if code == 429:
            ra = r.headers.get("retry-after")
            return ProviderError("rate_limit", f"Ollama rate or usage limit reached (HTTP 429): {detail}. Free cloud plans have usage limits; "
                                 "check your usage on ollama.com.", retryable=True,
                                 retry_after=float(ra) if ra and ra.replace(".", "", 1).isdigit() else None, status_code=code, provider="local")
        if code in (408,) or code >= 500:
            return ProviderError("unavailable", f"Ollama error (HTTP {code}): {detail}", retryable=True, status_code=code, provider="local")
        return ProviderError("bad_request", f"Ollama rejected the request (HTTP {code}): {detail}", status_code=code, provider="local")

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
