"""Runtime-adjustable settings (stored in SQLite). Secrets are never part of these."""
from __future__ import annotations

from typing import Any

from .db import Database

PROVIDERS = ["openai", "anthropic", "perplexity"]

# USD per 1M tokens (input, output). These are editable ESTIMATES used for budget checks and for
# providers that do not report cost. Sources: OpenAI pricing page, Anthropic model table, Perplexity
# Agent API models page, retrieved 2026-10-18. Verify before relying on them.
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "gpt-6-astra": {"in": 10.0, "out": 50.0},
    "gpt-6.1-sol": {"in": 2.0, "out": 10.0},
    "gpt-6-luna": {"in": 0.10, "out": 0.50},
    "claude-opus-5-5": {"in": 4.0, "out": 20.0},
    "claude-opus-5": {"in": 5.0, "out": 25.0},
    "claude-sonnet-5-5": {"in": 2.0, "out": 10.0},
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50},
    "claude-fable-5-1": {"in": 10.0, "out": 50.0},
    "perplexity/sonar": {"in": 0.25, "out": 2.50},
}

DEFAULTS: dict[str, Any] = {
    "providers_enabled": {"openai": True, "anthropic": True, "perplexity": True},
    "routing_mode": "auto",  # auto | manual (default for new tasks)
    "limits": {
        "per_task_usd": 1.00,
        "daily_usd": 5.00,
        "overall_usd": 0,  # 0 = no lifetime cap
        "per_provider_daily_usd": {"openai": 3.00, "anthropic": 3.00, "perplexity": 1.00},
        "max_calls_per_task": 25,
        "max_research_calls_per_task": 4,
        "max_repair_attempts": 2,
        "max_subtasks": 8,
        "call_timeout_s": 120,
        "task_timeout_s": 1800,
        "max_retries_per_call": 2,
        "max_output_tokens": 4096,
        "research_max_results": 8,
        "research_cache_ttl_hours": 72,
        "time_sensitive_ttl_hours": 6,
    },
    "prices": DEFAULT_PRICES,
    "unknown_model_price": {"in": 10.0, "out": 50.0},  # conservative so limits still bite for unknown ids
    "allowed_test_executables": ["python", "python3", "py", "pytest", "node", "npm", "npx", "pnpm", "yarn", "go", "cargo", "dotnet"],
    "allow_web_verification": True,
    "test_timeout_s": 120,
}


class SettingsService:
    def __init__(self, db: Database):
        self.db = db

    def get(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, default in DEFAULTS.items():
            stored = self.db.get_setting(key, None)
            if isinstance(default, dict) and isinstance(stored, dict):
                merged = dict(default)
                for k, v in stored.items():
                    if isinstance(merged.get(k), dict) and isinstance(v, dict):
                        merged[k] = {**merged[k], **v}
                    else:
                        merged[k] = v
                out[key] = merged
            else:
                out[key] = default if stored is None else stored
        return out

    def get_key(self, key: str) -> Any:
        return self.get()[key]

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.get()
        for key, value in patch.items():
            if key not in DEFAULTS:
                raise ValueError(f"unknown setting: {key}")
            self._validate(key, value)
            if isinstance(DEFAULTS[key], dict) and isinstance(value, dict):
                merged = dict(current[key])
                for k, v in value.items():
                    if isinstance(merged.get(k), dict) and isinstance(v, dict):
                        merged[k] = {**merged[k], **v}
                    else:
                        merged[k] = v
                value = merged
            self.db.set_setting(key, value)
        return self.get()

    @staticmethod
    def _validate(key: str, value: Any) -> None:
        if key == "providers_enabled":
            if not isinstance(value, dict) or set(value) - set(PROVIDERS) or not all(isinstance(v, bool) for v in value.values()):
                raise ValueError("providers_enabled must map openai/anthropic/perplexity to booleans")
        elif key == "routing_mode":
            if value not in ("auto", "manual"):
                raise ValueError("routing_mode must be 'auto' or 'manual'")
        elif key == "limits":
            if not isinstance(value, dict):
                raise ValueError("limits must be an object")
            for k, v in value.items():
                if k not in DEFAULTS["limits"]:
                    raise ValueError(f"unknown limit: {k}")
                if k == "per_provider_daily_usd":
                    if not isinstance(v, dict) or any((not isinstance(x, (int, float))) or x < 0 for x in v.values()):
                        raise ValueError("per_provider_daily_usd must map providers to non-negative numbers")
                elif not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                    raise ValueError(f"limit {k} must be a non-negative number")
        elif key == "prices":
            if not isinstance(value, dict) or any(not isinstance(v, dict) or set(v) != {"in", "out"} for v in value.values()):
                raise ValueError("prices must map model ids to {in, out} (USD per 1M tokens)")
