"""Independent source verification: open a cited URL and check whether the cited excerpt is really there.

Fetched page text is only ever compared with strings in code. It is never forwarded to a model, so
instructions hidden in a web page cannot influence any agent through this path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin

import httpx

from ..security import check_public_http_url
from .documents import html_to_text

MAX_BYTES = 1_500_000


@dataclass
class FetchResult:
    ok: bool
    status: int | None = None
    text: str = ""
    final_url: str = ""
    error: str = ""


class Fetcher(Protocol):
    async def fetch(self, url: str) -> FetchResult: ...


class HttpFetcher:
    """Public-internet fetcher with an SSRF guard applied to the first URL and every redirect hop."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0, check_url=check_public_http_url):
        self._transport = transport
        self._timeout = timeout
        self._check = check_url

    async def fetch(self, url: str) -> FetchResult:
        try:
            async with httpx.AsyncClient(transport=self._transport, follow_redirects=False, timeout=self._timeout,
                                         headers={"User-Agent": "ai-command-center-verifier/0.1", "Accept": "text/html,text/plain,application/json"}) as c:
                current = url
                for _ in range(4):
                    self._check(current)
                    r = await c.get(current)
                    if r.is_redirect and r.headers.get("location"):
                        current = urljoin(current, r.headers["location"])
                        continue
                    if r.status_code >= 400:
                        return FetchResult(False, r.status_code, error=f"HTTP {r.status_code}", final_url=current)
                    ctype = r.headers.get("content-type", "")
                    if not any(t in ctype for t in ("text/", "json", "xml", "html")):
                        return FetchResult(False, r.status_code, error=f"unsupported content type {ctype or 'unknown'}", final_url=current)
                    raw = r.content[:MAX_BYTES].decode(r.encoding or "utf-8", errors="replace")
                    text = html_to_text(raw) if "html" in ctype or raw.lstrip().startswith("<") else raw
                    return FetchResult(True, r.status_code, text, current)
                return FetchResult(False, None, error="too many redirects", final_url=current)
        except ValueError as e:  # SSRF guard
            return FetchResult(False, None, error=f"blocked: {e}")
        except httpx.HTTPError as e:
            return FetchResult(False, None, error=f"{type(e).__name__}: {e}")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def excerpt_in_text(excerpt: str, page_text: str) -> bool:
    """True if the excerpt appears in the page (exact after normalisation, or >=70% of its 5-word shingles)."""
    e, p = _norm(excerpt), _norm(page_text)
    if len(e) < 20:
        return False
    if e in p:
        return True
    words = e.split()
    if len(words) < 6:
        return False
    shingles = {" ".join(words[i : i + 5]) for i in range(len(words) - 4)}
    hit = sum(1 for s in shingles if s in p)
    return hit / len(shingles) >= 0.7
