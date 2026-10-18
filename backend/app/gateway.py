"""The only path to a model provider. Applies budget checks, timeouts, bounded retries with backoff,
call accounting and error normalisation. Nothing else in the app talks to a provider directly."""
from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Awaitable, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from .budget import BudgetExceeded, BudgetService
from .providers.base import AgentRequest, AgentResult, ProviderError, Usage, extract_json
from .registry import ProviderRegistry
from .security import redact
from .settings_service import SettingsService
from .store import Store

T = TypeVar("T", bound=BaseModel)


def _compact_schema(model_cls: type[BaseModel]) -> str:
    schema = model_cls.model_json_schema()

    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if k not in ("title", "description", "default")}
        if isinstance(o, list):
            return [strip(x) for x in o]
        return o

    return json.dumps(strip(schema), separators=(",", ":"))


class Gateway:
    def __init__(self, registry: ProviderRegistry, budget: BudgetService, store: Store, settings: SettingsService,
                 sleep: Callable[[float], Awaitable[None]] | None = None):
        self.registry = registry
        self.budget = budget
        self.store = store
        self.settings = settings
        self._sleep = sleep or asyncio.sleep

    async def call(self, provider_name: str, req: AgentRequest, *, task_id: str | None = None,
                   subtask_id: str | None = None) -> AgentResult:
        lim = self.settings.get_key("limits")
        provider = self.registry.get(provider_name)
        if not self.registry.enabled(provider_name):
            raise ProviderError("config", f"{provider_name} is disabled in settings", provider=provider_name)
        if not provider.is_configured():
            raise ProviderError("config", provider.config_detail() or f"{provider_name} is not configured", provider=provider_name)
        req.timeout_s = min(req.timeout_s, float(lim["call_timeout_s"])) if req.timeout_s else float(lim["call_timeout_s"])
        req.max_output_tokens = min(req.max_output_tokens, int(lim["max_output_tokens"]))
        max_attempts = int(lim["max_retries_per_call"]) + 1
        model = req.model or provider.model
        last_err: ProviderError | None = None
        for attempt in range(1, max_attempts + 1):
            est = self.budget.worst_case(model, len(req.prompt) + len(req.system), req.max_output_tokens) if provider.billable else 0.0
            # Raises BudgetExceeded before ANY billable request is sent (every retry counts as a call).
            self.budget.check(task_id=task_id, provider=provider_name, billable=provider.billable, estimate=est,
                              is_research=(provider_name == "perplexity" and req.purpose == "research"))
            t0 = time.monotonic()
            try:
                result = await asyncio.wait_for(provider.complete(req), timeout=req.timeout_s + (5 if req.timeout_s >= 30 else 0.1))
            except asyncio.TimeoutError:
                err = ProviderError("timeout", f"No response within {req.timeout_s:.0f}s", retryable=True, provider=provider_name)
                last_err = self._record_failure(task_id, subtask_id, provider_name, model, req, err, t0)
            except asyncio.CancelledError:
                self.store.record_call(task_id=task_id, subtask_id=subtask_id, provider=provider_name, model=model, purpose=req.purpose,
                                       status="error", error_kind="cancelled", error="cancelled by user or shutdown",
                                       billable=provider.billable, cost_basis="none", latency_ms=int((time.monotonic() - t0) * 1000))
                raise
            except ProviderError as e:
                e.provider = e.provider or provider_name
                last_err = self._record_failure(task_id, subtask_id, provider_name, model, req, e, t0)
            except Exception as e:  # adapter bug or unexpected SDK failure: never crash the engine
                err = ProviderError("unavailable", f"Unexpected adapter failure: {type(e).__name__}: {e}", provider=provider_name)
                last_err = self._record_failure(task_id, subtask_id, provider_name, model, req, err, t0)
            else:
                self._record_success(task_id, subtask_id, provider, model, req, result, t0)
                return result
            if not last_err.retryable or attempt == max_attempts:
                raise last_err
            delay = last_err.retry_after if last_err.retry_after is not None else min(30.0, 1.0 * (2 ** (attempt - 1)))
            delay = min(delay, 60.0) + random.uniform(0, 0.25) * (1 if last_err.retry_after is None else 0)
            self.store.log(task_id, "warn", f"{provider_name} {last_err.kind}; retry {attempt}/{max_attempts - 1} in {delay:.1f}s",
                           agent=provider_name, subtask_id=subtask_id)
            await self._sleep(delay)
        raise last_err  # pragma: no cover

    def _record_failure(self, task_id, subtask_id, name, model, req, err: ProviderError, t0) -> ProviderError:
        self.store.record_call(task_id=task_id, subtask_id=subtask_id, provider=name, model=model, purpose=req.purpose, status="error",
                               error_kind=err.kind, error=err.message, billable=self.registry.get(name).billable, cost_usd=0.0,
                               cost_basis="none", latency_ms=int((time.monotonic() - t0) * 1000))
        return err

    def _record_success(self, task_id, subtask_id, provider, model, req, result: AgentResult, t0) -> None:
        u: Usage = result.usage
        cost, basis = u.cost_usd, u.cost_basis
        if not provider.billable:
            cost, basis = 0.0, "demo"
        elif cost is None:
            if u.input_tokens is not None and u.output_tokens is not None:
                cost = self.budget.cost_for_tokens(result.model or model, u.input_tokens, u.output_tokens)
            else:  # usage not exposed: charge the worst case so limits stay safe
                cost = self.budget.worst_case(result.model or model, len(req.prompt) + len(req.system), req.max_output_tokens)
            basis = "estimated"
        u.cost_usd, u.cost_basis = cost, basis
        self.store.record_call(task_id=task_id, subtask_id=subtask_id, provider=provider.name, model=result.model or model, purpose=req.purpose,
                               status="ok", input_tokens=u.input_tokens, output_tokens=u.output_tokens, cost_usd=cost, cost_basis=basis,
                               billable=provider.billable, latency_ms=int((time.monotonic() - t0) * 1000),
                               meta={"response_id": result.response_id, "stop_reason": result.stop_reason, "sources": len(result.sources)})

    async def call_structured(self, provider_name: str, req: AgentRequest, model_cls: type[T], *, task_id: str | None = None,
                              subtask_id: str | None = None) -> tuple[T, AgentResult]:
        """Call a provider and validate the answer against a pydantic contract. One repair re-ask on bad output."""
        req.prompt += ("\n\nRespond with ONE JSON object only (no prose, no code fences) that matches this JSON Schema:\n"
                       + _compact_schema(model_cls))
        last_problem = ""
        for round_ in (1, 2):
            result = await self.call(provider_name, req, task_id=task_id, subtask_id=subtask_id)
            try:
                if result.stop_reason in ("max_tokens", "max_output_tokens"):
                    raise ValueError("output was truncated at the token limit")
                return model_cls.model_validate(extract_json(result.text)), result
            except (ValueError, ValidationError) as e:
                last_problem = str(e)[:500]
                self.store.log(task_id, "warn", f"{provider_name} returned invalid structured output ({last_problem[:200]})",
                               agent=provider_name, subtask_id=subtask_id)
                if round_ == 1:
                    req = AgentRequest(purpose=req.purpose, system=req.system, model=req.model, max_output_tokens=req.max_output_tokens,
                                       timeout_s=req.timeout_s, research=req.research,
                                       prompt=req.prompt + f"\n\nYour previous reply was rejected: {last_problem}\nReturn only the corrected JSON object.")
        raise ProviderError("invalid_output", f"{provider_name} did not return valid structured output: {last_problem}", provider=provider_name)
