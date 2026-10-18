"""OpenAI adapter, using the Responses API (`POST /v1/responses`) through the official SDK."""
from __future__ import annotations

import time

import openai

from ..config import Config
from .base import AgentRequest, AgentResult, Provider, ProviderError, Usage


def _retry_after(exc: openai.APIStatusError) -> float | None:
    try:
        v = exc.response.headers.get("retry-after")
        return float(v) if v else None
    except Exception:
        return None


def map_openai_error(exc: Exception) -> ProviderError:
    if isinstance(exc, openai.APITimeoutError):
        return ProviderError("timeout", "OpenAI request timed out", retryable=True, provider="openai")
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError("unavailable", f"Could not reach OpenAI: {exc}", retryable=True, provider="openai")
    if isinstance(exc, openai.APIStatusError):
        code = exc.status_code
        if code in (401, 403):
            return ProviderError("auth", "OpenAI rejected the API key or the key lacks access to this model "
                                 f"(HTTP {code}). Check OPENAI_API_KEY and billing.", status_code=code, provider="openai")
        if code == 429:
            msg = "OpenAI rate limit or quota exceeded (HTTP 429)"
            return ProviderError("rate_limit", msg, retryable=True, retry_after=_retry_after(exc), status_code=code, provider="openai")
        if code in (408, 409) or code >= 500:
            return ProviderError("unavailable", f"OpenAI service error (HTTP {code})", retryable=True, status_code=code, provider="openai")
        return ProviderError("bad_request", f"OpenAI rejected the request (HTTP {code}): {getattr(exc, 'message', exc)}",
                             status_code=code, provider="openai")
    return ProviderError("unavailable", f"Unexpected OpenAI client error: {exc}", provider="openai")


class OpenAIProvider(Provider):
    name = "openai"
    role = "Analysis"

    def __init__(self, cfg: Config, client: openai.AsyncOpenAI | None = None):
        self.cfg = cfg
        self._client = client

    @property
    def model(self) -> str:
        return self.cfg.openai_model

    def is_configured(self) -> bool:
        return bool(self.cfg.openai_api_key) or self._client is not None

    def client(self) -> openai.AsyncOpenAI:
        if self._client is None:
            if not self.cfg.openai_api_key:
                raise ProviderError("config", "OPENAI_API_KEY is not set", provider="openai")
            kwargs = {"api_key": self.cfg.openai_api_key, "max_retries": 0}  # retries are handled (and counted) by the gateway
            if self.cfg.openai_base_url:
                kwargs["base_url"] = self.cfg.openai_base_url
            self._client = openai.AsyncOpenAI(**kwargs)
        return self._client

    async def validate(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "OPENAI_API_KEY is not set"
        try:
            await self.client().models.list()  # free call; proves the key is accepted
        except ProviderError as e:
            return False, e.message
        except Exception as e:
            err = map_openai_error(e)
            return False, err.message
        return True, f"API key accepted. Configured model: {self.model} (model id is not checked until first use)."

    async def complete(self, req: AgentRequest) -> AgentResult:
        client = self.client()
        model = req.model or self.model
        kwargs = dict(model=model, input=req.prompt, max_output_tokens=req.max_output_tokens,
                      timeout=req.timeout_s, store=False)
        if req.system:
            kwargs["instructions"] = req.system
        t0 = time.monotonic()
        try:
            resp = await client.responses.create(**kwargs)
        except ProviderError:
            raise
        except Exception as e:
            raise map_openai_error(e) from e
        status = getattr(resp, "status", "completed")
        if status == "failed":
            err = getattr(resp, "error", None)
            raise ProviderError("unavailable", f"OpenAI response failed: {getattr(err, 'message', err)}", retryable=True, provider="openai")
        text = getattr(resp, "output_text", "") or ""
        usage = Usage(cost_basis="estimated")
        u = getattr(resp, "usage", None)
        if u is not None:
            usage.input_tokens = getattr(u, "input_tokens", None)
            usage.output_tokens = getattr(u, "output_tokens", None)
        stop = "end_turn"
        if status == "incomplete":
            reason = getattr(getattr(resp, "incomplete_details", None), "reason", "") or "incomplete"
            stop = reason
        for item in getattr(resp, "output", []) or []:
            for part in getattr(item, "content", []) or []:
                if getattr(part, "type", "") == "refusal":
                    raise ProviderError("refused", f"OpenAI declined the request: {getattr(part, 'refusal', '')}", provider="openai")
        return AgentResult(provider="openai", model=getattr(resp, "model", model) or model, text=text, usage=usage,
                           response_id=getattr(resp, "id", ""), stop_reason=stop,
                           meta={"latency_ms": int((time.monotonic() - t0) * 1000)})

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
