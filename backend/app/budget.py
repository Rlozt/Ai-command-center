"""Spend tracking and hard limits. Dollar limits only apply to billable providers."""
from __future__ import annotations

from datetime import datetime, timezone

from .db import Database
from .settings_service import SettingsService


class BudgetExceeded(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # per_task | daily | overall | provider | calls | research_calls
        self.message = message


def utc_day_start() -> str:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="milliseconds")


class BudgetService:
    def __init__(self, db: Database, settings: SettingsService):
        self.db = db
        self.settings = settings

    # --- pricing ---------------------------------------------------------
    def price(self, model: str) -> tuple[float, float, bool]:
        s = self.settings.get()
        p = s["prices"].get(model)
        known = p is not None
        p = p or s["unknown_model_price"]
        return float(p["in"]), float(p["out"]), known

    def cost_for_tokens(self, model: str, in_tok: int, out_tok: int) -> float:
        pin, pout, _ = self.price(model)
        return (in_tok * pin + out_tok * pout) / 1_000_000

    def worst_case(self, model: str, prompt_chars: int, max_out: int) -> float:
        # ~3 chars per token is deliberately pessimistic
        return self.cost_for_tokens(model, int(prompt_chars / 3) + 1, max_out)

    # --- totals ----------------------------------------------------------
    def _sum(self, where: str, params: tuple = ()) -> float:
        r = self.db.one(f"SELECT COALESCE(SUM(cost_usd),0) AS s FROM calls WHERE billable=1 AND {where}", params)
        return float(r["s"])

    def spent_task(self, task_id: str) -> float:
        return self._sum("task_id=?", (task_id,))

    def spent_today(self, provider: str | None = None) -> float:
        if provider:
            return self._sum("at>=? AND provider=?", (utc_day_start(), provider))
        return self._sum("at>=?", (utc_day_start(),))

    def spent_total(self) -> float:
        return self._sum("1=1")

    def calls_for_task(self, task_id: str, provider: str | None = None) -> int:
        if provider:
            return self.db.one("SELECT COUNT(*) c FROM calls WHERE task_id=? AND provider=? AND purpose NOT IN ('ping')", (task_id, provider))["c"]
        return self.db.one("SELECT COUNT(*) c FROM calls WHERE task_id=? AND purpose NOT IN ('ping')", (task_id,))["c"]

    # --- enforcement -----------------------------------------------------
    def check(self, *, task_id: str | None, provider: str, billable: bool, estimate: float, is_research: bool = False) -> None:
        lim = self.settings.get_key("limits")
        if task_id:
            if self.calls_for_task(task_id) >= lim["max_calls_per_task"]:
                raise BudgetExceeded("calls", f"Task reached its limit of {int(lim['max_calls_per_task'])} model calls.")
            if is_research and self.calls_for_task(task_id, "perplexity") >= lim["max_research_calls_per_task"]:
                raise BudgetExceeded("research_calls", f"Task reached its limit of {int(lim['max_research_calls_per_task'])} research calls.")
        if not billable:
            return
        if task_id and lim["per_task_usd"] and self.spent_task(task_id) + estimate > lim["per_task_usd"]:
            raise BudgetExceeded("per_task", f"Per-task spending limit ${lim['per_task_usd']:.2f} would be exceeded "
                                 f"(spent ${self.spent_task(task_id):.4f}, next call up to ${estimate:.4f}).")
        if lim["daily_usd"] and self.spent_today() + estimate > lim["daily_usd"]:
            raise BudgetExceeded("daily", f"Daily spending limit ${lim['daily_usd']:.2f} would be exceeded "
                                 f"(spent today ${self.spent_today():.4f}, next call up to ${estimate:.4f}).")
        if lim["overall_usd"] and self.spent_total() + estimate > lim["overall_usd"]:
            raise BudgetExceeded("overall", f"Overall spending limit ${lim['overall_usd']:.2f} would be exceeded.")
        plim = lim["per_provider_daily_usd"].get(provider)
        if plim and self.spent_today(provider) + estimate > plim:
            raise BudgetExceeded("provider", f"{provider} daily limit ${plim:.2f} would be exceeded "
                                 f"(spent today ${self.spent_today(provider):.4f}).")

    def summary(self) -> dict:
        lim = self.settings.get_key("limits")
        providers = {}
        for name in ("openai", "anthropic", "perplexity", "local"):
            r = self.db.one("SELECT COUNT(*) c, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o FROM calls WHERE provider=? AND at>=?",
                            (name, utc_day_start()))
            providers[name] = {"spent_today_usd": round(self.spent_today(name), 6), "calls_today": r["c"],
                               "input_tokens_today": r["i"], "output_tokens_today": r["o"],
                               "limit_usd": lim["per_provider_daily_usd"].get(name)}
        demo_calls = self.db.one("SELECT COUNT(*) c FROM calls WHERE billable=0 AND cost_basis!='local' AND at>=?", (utc_day_start(),))["c"]
        local_calls = self.db.one("SELECT COUNT(*) c FROM calls WHERE cost_basis='local' AND at>=?", (utc_day_start(),))["c"]
        return {"spent_today_usd": round(self.spent_today(), 6), "spent_total_usd": round(self.spent_total(), 6),
                "daily_limit_usd": lim["daily_usd"], "per_task_limit_usd": lim["per_task_usd"], "overall_limit_usd": lim["overall_usd"],
                "providers": providers, "demo_calls_today": demo_calls, "local_calls_today": local_calls,
                "note": "Costs are USD. 'reported' = returned by the provider; 'estimated' = tokens x editable price table. "
                        "Failed or timed-out requests may still be billed by the provider and are not counted here."}
