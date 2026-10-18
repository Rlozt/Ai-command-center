"""Wires the services together. Tests build this with fake providers; production builds it from Config."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from .budget import BudgetService
from .config import Config
from .db import Database
from .engine import Engine
from .gateway import Gateway
from .memory import MemoryService
from .providers.base import Provider
from .registry import ProviderRegistry
from .research import ResearchService
from .security import register_secrets
from .settings_service import SettingsService
from .staging import StagingService
from .store import Store
from .tools import build_tool_registry
from .tools.base import ToolContext, ToolRegistry
from .tools.webcheck import Fetcher, HttpFetcher


@dataclass
class AppContext:
    cfg: Config
    db: Database
    store: Store
    settings: SettingsService
    registry: ProviderRegistry
    budget: BudgetService
    gateway: Gateway
    staging: StagingService
    memory: MemoryService
    research: ResearchService
    tools: ToolRegistry
    engine: Engine
    fetcher: Fetcher | None

    def tool_context(self, project: dict | None = None, task_id: str | None = None) -> ToolContext:
        return ToolContext(self.cfg, self.store, self.settings.get(), project, task_id,
                           services={"staging": self.staging, "memory": self.memory, "fetcher": self.fetcher})


def build_context(cfg: Config, *, providers: dict[str, Provider] | None = None, fetcher: Fetcher | None = None,
                  sleep: Callable[[float], Awaitable[None]] | None = None, db_path: str | None = None) -> AppContext:
    register_secrets(cfg.secrets())
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(db_path or cfg.db_path)
    store = Store(db)
    settings = SettingsService(db)
    registry = ProviderRegistry(cfg, settings, providers)
    budget = BudgetService(db, settings)
    gateway = Gateway(registry, budget, store, settings, sleep=sleep)
    staging = StagingService(cfg, db, store)
    memory = MemoryService(db, store)
    if fetcher is None and cfg.mode == "live":
        fetcher = HttpFetcher()
    research = ResearchService(db, store, settings, fetcher, verification_enabled=(fetcher is not None and settings.get_key("allow_web_verification")))
    tools = build_tool_registry()
    engine = Engine(cfg, db, store, settings, registry, gateway, staging, memory, research, tools)
    return AppContext(cfg, db, store, settings, registry, budget, gateway, staging, memory, research, tools, engine, fetcher)
