"""Tool framework: each tool has an input model, an output model, a permission class and structured errors.

Tools are invoked by the coordinator and by API endpoints, never directly by model output. Model text is
data; it cannot name a tool to run.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError

from ..config import Config
from ..security import PathError, redact
from ..store import Store

PERMISSIONS = ("read", "stage", "execute", "network", "memory")


class ToolError(Exception):
    """A tool failed in an expected way (bad input, permission denied, missing file)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ToolContext:
    cfg: Config
    store: Store
    settings: dict[str, Any]
    project: dict | None = None
    task_id: str | None = None
    services: dict[str, Any] | None = None  # staging, memory, fetcher, ...


@dataclass
class Tool:
    name: str
    description: str
    permission: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], Awaitable[BaseModel]]
    boundary: str  # human-readable statement of what the tool may touch


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        assert tool.permission in PERMISSIONS
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "permission": t.permission, "boundary": t.boundary,
                 "input_schema": t.input_model.model_json_schema(), "output_schema": t.output_model.model_json_schema()}
                for t in self._tools.values()]

    async def invoke(self, name: str, args: dict, ctx: ToolContext) -> dict:
        """Returns {"ok": True, "result": {...}} or {"ok": False, "error": {"code", "message"}}. Never raises for expected failures."""
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": {"code": "unknown_tool", "message": f"no such tool: {name}"}}
        t0 = time.monotonic()
        try:
            parsed = tool.input_model.model_validate(args)
            out = await tool.handler(ctx, parsed)
            result = tool.output_model.model_validate(out.model_dump() if isinstance(out, BaseModel) else out)
            return {"ok": True, "result": result.model_dump(), "duration_ms": int((time.monotonic() - t0) * 1000)}
        except ValidationError as e:
            return {"ok": False, "error": {"code": "invalid_input", "message": redact(str(e))[:500]}}
        except ToolError as e:
            return {"ok": False, "error": {"code": e.code, "message": redact(e.message)}}
        except PathError as e:
            return {"ok": False, "error": {"code": "path_denied", "message": str(e)}}
        except Exception as e:  # tool bug: report, do not crash the caller
            ctx.store.log(ctx.task_id, "error", f"tool {name} crashed: {type(e).__name__}: {e}")
            return {"ok": False, "error": {"code": "tool_crashed", "message": f"{type(e).__name__}: {redact(str(e))[:300]}"}}
