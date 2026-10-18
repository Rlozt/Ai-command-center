"""Provider interface shared by OpenAI, Anthropic and Perplexity adapters (and the demo mocks).

Each adapter keeps its own authentication and response parsing; callers only see
AgentRequest / AgentResult / ProviderError.
"""
from __future__ import annotations

import abc
import json
import re
from dataclasses import dataclass, field
from typing import Any


class ProviderError(Exception):
    """A normalised provider failure.

    kind: auth | rate_limit | timeout | unavailable | bad_request | config | refused | invalid_output | cancelled
    """

    def __init__(self, kind: str, message: str, *, retryable: bool = False, retry_after: float | None = None,
                 status_code: int | None = None, provider: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after
        self.status_code = status_code
        self.provider = provider

    def __str__(self) -> str:
        return f"[{self.provider or 'provider'}:{self.kind}] {self.message}"


@dataclass
class ResearchOptions:
    recency: str | None = None  # hour | day | week | month | year
    domains: list[str] = field(default_factory=list)
    max_results: int = 8
    time_sensitive: bool = False


@dataclass
class AgentRequest:
    purpose: str  # plan | research | analysis | implementation | document | synthesis | review | evaluation | ping
    prompt: str
    system: str = ""
    max_output_tokens: int = 4096
    model: str | None = None  # override the configured model
    expect_json: bool = True
    timeout_s: float = 120.0
    research: ResearchOptions | None = None


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None  # provider-reported cost when exposed
    cost_basis: str = "none"  # none | demo | reported | estimated


@dataclass
class Source:
    url: str
    title: str = ""
    snippet: str = ""
    published: str | None = None
    last_updated: str | None = None


@dataclass
class AgentResult:
    provider: str
    model: str
    text: str
    usage: Usage = field(default_factory=Usage)
    sources: list[Source] = field(default_factory=list)  # as returned by the provider's search, never model-written
    response_id: str = ""
    stop_reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderStatus:
    provider: str
    role: str
    enabled: bool
    configured: bool  # credentials present (or demo mode)
    ready: bool  # enabled and configured
    demo: bool
    model: str
    detail: str = ""
    validated: bool | None = None  # result of the last live validation call, if any


class Provider(abc.ABC):
    name: str = ""
    role: str = ""
    billable: bool = True
    demo: bool = False
    local: bool = False  # runs on the user's machine: free of API charges but NOT a demo mock

    @property
    @abc.abstractmethod
    def model(self) -> str: ...

    @abc.abstractmethod
    def is_configured(self) -> bool: ...

    def config_detail(self) -> str:
        return "" if self.is_configured() else "API key not set"

    @abc.abstractmethod
    async def validate(self) -> tuple[bool, str]:
        """Check credentials with a free (non-generating) API call. Returns (ok, message)."""

    @abc.abstractmethod
    async def complete(self, req: AgentRequest) -> AgentResult:
        """Send one request. Must raise ProviderError for every provider-side failure."""

    async def aclose(self) -> None:  # pragma: no cover - optional
        return None


_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n```\s*$", re.DOTALL)


def extract_json(text: str) -> Any:
    """Parse a JSON object out of model text (tolerates code fences and leading/trailing prose)."""
    s = text.strip()
    m = _FENCE.match(s)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start : i + 1])
                    except json.JSONDecodeError:
                        break
        start = s.find("{", start + 1)
    raise ValueError("no valid JSON object found in model output")
