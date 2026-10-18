from pathlib import Path

import pytest

from app.config import Config
from app.context import build_context


class FakeFetcher:
    """Test double for the web verifier. Maps url -> page text; anything else is a 404."""

    def __init__(self, pages: dict[str, str] | None = None):
        self.pages = pages or {}
        self.calls: list[str] = []

    async def fetch(self, url: str):
        from app.tools.webcheck import FetchResult
        self.calls.append(url)
        if url in self.pages:
            return FetchResult(True, 200, self.pages[url], url)
        return FetchResult(False, 404, error="HTTP 404", final_url=url)


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(mode="demo", data_dir=tmp_path / "data", allowed_origins=["http://localhost:8787"])


@pytest.fixture
def ctx(cfg):
    c = build_context(cfg, sleep=no_sleep)
    yield c
    c.db.close()


@pytest.fixture
def project_dir(tmp_path) -> Path:
    d = tmp_path / "proj"
    d.mkdir()
    return d


@pytest.fixture
def project(ctx, project_dir):
    return ctx.store.create_project("Sample", root_dir=str(project_dir),
                                    test_command="python -m unittest discover -s demo_output")


async def run_task(ctx, request: str, project_id: str | None = None, routing: str = "auto", routes: dict | None = None, wait: bool = True) -> dict:
    t = ctx.store.create_task(request, project_id, routing, routes or {})
    ctx.engine.start(t["id"])
    if wait:
        await ctx.engine.wait(t["id"])
    return ctx.store.get_task(t["id"])


def set_limits(ctx, **limits):
    ctx.settings.update({"limits": limits})


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch):
    """Tests must never be able to reach a paid API, even if the developer's shell has keys set."""
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ACC_MODE", "demo")


def make_symlink(target, link):
    """Symlinks need a privilege on Windows (Developer Mode or admin); skip, rather than fail, where that is not available."""
    import os
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"cannot create symlinks here: {e}")
