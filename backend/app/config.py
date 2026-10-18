"""Process-level configuration read from environment variables and an optional .env file.

Secrets (API keys) only ever live here, in memory. They are never written to the
database, logs, or API responses.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent


def load_dotenv(path: Path) -> None:
    """Tiny .env loader. Existing environment variables win over file values."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Config:
    mode: str = "demo"  # "demo" (mock providers, no network) or "live"
    data_dir: Path = field(default_factory=lambda: REPO_DIR / "data")
    host: str = "127.0.0.1"
    port: int = 8787
    allowed_origins: list[str] = field(default_factory=list)
    frontend_dir: Path = field(default_factory=lambda: REPO_DIR / "frontend" / "dist")

    openai_api_key: str = ""
    anthropic_api_key: str = ""
    perplexity_api_key: str = ""

    openai_model: str = "gpt-6.1-sol"
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "medium"
    perplexity_preset: str = "low"
    perplexity_model: str = ""  # optional "provider/model" override; empty = use preset
    openai_base_url: str = ""
    anthropic_base_url: str = ""
    perplexity_base_url: str = "https://api.perplexity.ai"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "acc.sqlite3"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def sandbox_dir(self) -> Path:
        return self.data_dir / "sandbox"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "session-token.txt"

    def secrets(self) -> list[str]:
        return [s for s in (self.openai_api_key, self.anthropic_api_key, self.perplexity_api_key) if s]

    def api_key(self, provider: str) -> str:
        return {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "perplexity": self.perplexity_api_key,
        }.get(provider, "")


def load_config(env_file: Path | None = None) -> Config:
    load_dotenv(env_file or (REPO_DIR / ".env"))
    port = int(_env("ACC_PORT", "8787") or "8787")
    default_origins = ",".join(
        [
            f"http://localhost:{port}",
            f"http://127.0.0.1:{port}",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )
    origins = [o.strip().rstrip("/") for o in _env("ACC_ALLOWED_ORIGINS", default_origins).split(",") if o.strip()]
    cfg = Config(
        mode=_env("ACC_MODE", "demo").lower(),
        data_dir=Path(_env("ACC_DATA_DIR") or (REPO_DIR / "data")).resolve(),
        host=_env("ACC_HOST", "127.0.0.1"),
        port=port,
        allowed_origins=origins,
        openai_api_key=_env("OPENAI_API_KEY"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        perplexity_api_key=_env("PERPLEXITY_API_KEY"),
        openai_model=_env("OPENAI_MODEL", "gpt-6.1-sol"),
        anthropic_model=_env("ANTHROPIC_MODEL", "claude-opus-5-5"),
        anthropic_effort=_env("ANTHROPIC_EFFORT", "medium"),
        perplexity_preset=_env("PERPLEXITY_PRESET", "low"),
        perplexity_model=_env("PERPLEXITY_MODEL"),
        openai_base_url=_env("OPENAI_BASE_URL"),
        anthropic_base_url=_env("ANTHROPIC_BASE_URL"),
        perplexity_base_url=_env("PERPLEXITY_BASE_URL", "https://api.perplexity.ai"),
    )
    if cfg.mode not in ("demo", "live"):
        raise ValueError("ACC_MODE must be 'demo' or 'live'")
    return cfg
