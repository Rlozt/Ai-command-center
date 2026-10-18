"""Builds the provider set for the current mode and reports provider status without exposing secrets."""
from __future__ import annotations

from .config import Config
from .providers.anthropic_provider import AnthropicProvider
from .providers.base import Provider, ProviderStatus
from .providers.local_provider import LocalProvider
from .providers.mock import MockProvider
from .providers.openai_provider import OpenAIProvider
from .providers.perplexity_provider import PerplexityProvider
from .settings_service import PROVIDERS, SettingsService


class ProviderRegistry:
    def __init__(self, cfg: Config, settings: SettingsService, providers: dict[str, Provider] | None = None):
        self.cfg = cfg
        self.settings = settings
        self.validation: dict[str, tuple[bool, str]] = {}
        if providers is not None:
            self.providers = providers
        elif cfg.mode == "demo":
            self.providers = {
                "openai": MockProvider("openai", "Analysis", "demo-openai"),
                "anthropic": MockProvider("anthropic", "Implementation", "demo-claude"),
                "perplexity": MockProvider("perplexity", "Research", "demo-perplexity"),
                "local": MockProvider("local", "Local model", "demo-local"),
            }
        else:
            self.providers = {
                "openai": OpenAIProvider(cfg),
                "anthropic": AnthropicProvider(cfg),
                "perplexity": PerplexityProvider(cfg),
                "local": LocalProvider(cfg),
            }

    def get(self, name: str) -> Provider:
        return self.providers[name]

    def enabled(self, name: str) -> bool:
        return bool(self.settings.get_key("providers_enabled").get(name, False))

    def available(self) -> set[str]:
        return {n for n, p in self.providers.items() if self.enabled(n) and p.is_configured()}

    def status(self) -> list[ProviderStatus]:
        out = []
        for n in PROVIDERS:
            if n not in self.providers:  # tests may inject a partial set
                continue
            p = self.providers[n]
            enabled = self.enabled(n)
            configured = p.is_configured()
            v = self.validation.get(n)
            detail = p.config_detail()
            if not enabled:
                detail = "Disabled in settings"
            elif p.demo:
                detail = "Demo mode: offline mock, no billable calls"
            out.append(ProviderStatus(provider=n, role=p.role, enabled=enabled, configured=configured,
                                      ready=enabled and configured, demo=p.demo, model=p.model, detail=detail,
                                      validated=v[0] if v else None))
        return out

    async def validate(self, name: str) -> tuple[bool, str]:
        result = await self.providers[name].validate()
        self.validation[name] = result
        return result

    async def aclose(self) -> None:
        for p in self.providers.values():
            await p.aclose()
