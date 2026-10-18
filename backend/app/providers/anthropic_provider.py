"""Anthropic adapter, using the Messages API (`POST /v1/messages`) through the official SDK."""
from __future__ import annotations

import time

import anthropic

from ..config import Config
from .base import AgentRequest, AgentResult, Provider, ProviderError, Usage


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    try:
        v = exc.response.headers.get("retry-after")
        return float(v) if v else None
    except Exception:
        return None


def map_anthropic_error(exc: Exception) -> ProviderError:
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderError("timeout", "Anthropic request timed out", retryable=True, provider="anthropic")
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError("unavailable", f"Could not reach Anthropic: {exc}", retryable=True, provider="anthropic")
    if isinstance(exc, anthropic.APIStatusError):
        code = exc.status_code
        if code in (401, 403):
            return ProviderError("auth", "Anthropic rejected the API key or the key lacks access to this model "
                                 f"(HTTP {code}). Check ANTHROPIC_API_KEY. A Claude subscription does not include API access.",
                                 status_code=code, provider="anthropic")
        if code == 429:
            return ProviderError("rate_limit", "Anthropic rate limit exceeded (HTTP 429)", retryable=True,
                                 retry_after=_retry_after(exc), status_code=code, provider="anthropic")
        if code in (408, 409, 529) or code >= 500:
            return ProviderError("unavailable", f"Anthropic service error or overload (HTTP {code})", retryable=True,
                                 status_code=code, provider="anthropic")
        return ProviderError("bad_request", f"Anthropic rejected the request (HTTP {code}): {getattr(exc, 'message', exc)}",
                             status_code=code, provider="anthropic")
    return ProviderError("unavailable", f"Unexpected Anthropic client error: {exc}", provider="anthropic")


class AnthropicProvider(Provider):
    name = "anthropic"
    role = "Implementation"

    def __init__(self, cfg: Config, client: anthropic.AsyncAnthropic | None = None):
        self.cfg = cfg
        self._client = client

    @property
    def model(self) -> str:
        return self.cfg.anthropic_model

    def is_configured(self) -> bool:
        return bool(self.cfg.anthropic_api_key) or self._client is not None

    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            if not self.cfg.anthropic_api_key:
                raise ProviderError("config", "ANTHROPIC_API_KEY is not set", provider="anthropic")
            kwargs = {"api_key": self.cfg.anthropic_api_key, "max_retries": 0}  # retries handled by the gateway
            if self.cfg.anthropic_base_url:
                kwargs["base_url"] = self.cfg.anthropic_base_url
            self._client = anthropic.AsyncAnthropic(**kwargs)
        return self._client

    async def validate(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "ANTHROPIC_API_KEY is not set"
        try:
            await self.client().models.retrieve(self.model)  # free call: checks the key AND that the model id exists
        except ProviderError as e:
            return False, e.message
        except anthropic.NotFoundError:
            return False, f"API key accepted, but model id '{self.model}' was not found. Change ANTHROPIC_MODEL."
        except Exception as e:
            return False, map_anthropic_error(e).message
        return True, f"API key accepted and model '{self.model}' exists."

    async def complete(self, req: AgentRequest) -> AgentResult:
        client = self.client()
        model = req.model or self.model
        kwargs: dict = dict(model=model, max_tokens=req.max_output_tokens,
                            messages=[{"role": "user", "content": req.prompt}], timeout=req.timeout_s)
        if req.system:
            kwargs["system"] = req.system
        if self.cfg.anthropic_effort:
            kwargs["output_config"] = {"effort": self.cfg.anthropic_effort}
        t0 = time.monotonic()
        try:
            resp = await client.messages.create(**kwargs)
        except ProviderError:
            raise
        except Exception as e:
            raise map_anthropic_error(e) from e
        if resp.stop_reason == "refusal":
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            raise ProviderError("refused", f"Claude declined the request (category: {cat or 'unspecified'})", provider="anthropic")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        usage = Usage(input_tokens=getattr(resp.usage, "input_tokens", None),
                      output_tokens=getattr(resp.usage, "output_tokens", None), cost_basis="estimated")
        return AgentResult(provider="anthropic", model=getattr(resp, "model", model) or model, text=text, usage=usage,
                           response_id=getattr(resp, "id", ""), stop_reason=resp.stop_reason or "",
                           meta={"latency_ms": int((time.monotonic() - t0) * 1000)})

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
