from .base import ToolRegistry
from . import files, service_tools


def build_tool_registry() -> ToolRegistry:
    reg = ToolRegistry()
    files.register(reg)
    service_tools.register(reg)
    return reg
