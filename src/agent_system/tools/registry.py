"""
Legacy re-exports — for backward compatibility with iteration 1 code.

New code should import from agent_system.tools.base directly.
"""

from agent_system.tools.base import (
    Tool,
    ToolDefinition,
    ToolResult,
    ToolRegistry,
    discover_tools,
    filter_registry,
    register,
)

from agent_system.tools.file_tools import (
    ReadFileTool,
    WriteFileTool,
    ListFilesTool,
)


def create_default_registry() -> ToolRegistry:
    """Create the default tool registry via auto-discovery"""
    registry = discover_tools()
    return registry


__all__ = [
    "Tool", "ToolDefinition", "ToolResult", "ToolRegistry",
    "discover_tools", "filter_registry", "register",
    "ReadFileTool", "WriteFileTool", "ListFilesTool",
    "create_default_registry",
]
