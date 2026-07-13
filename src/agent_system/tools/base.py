"""
Plugin-based Tool System (v13-style)

Base class + @register decorator + auto-discovery + config-driven loading.
Reference: ARCHITECTURE.md Ch.9 Plugin-based Tool System
"""

import importlib
import inspect
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ToolDefinition(BaseModel):
    """Tool metadata for LLM tool calling"""
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolResult(BaseModel):
    """Tool execution result"""
    success: bool
    output: Any = None
    error: str | None = None


# Global tool registry
_tool_classes: dict[str, type["Tool"]] = {}


def register(cls):
    """Decorator: automatically register a Tool subclass"""
    if not (inspect.isclass(cls) and issubclass(cls, Tool)):
        raise TypeError(f"@register can only be used on Tool subclasses, got {cls}")
    instance = cls()
    if not instance.name:
        raise ValueError(f"Tool subclass {cls.__name__} must have a non-empty name")
    _tool_classes[instance.name] = cls
    logger.debug(f"Registered tool: {instance.name} ({cls.__name__})")
    return cls


class Tool(ABC):
    """Tool base class — all tools inherit from this.

    Subclasses must define:
        name: str
        description: str
        input_schema: dict

    Subclasses must implement:
        async def execute(self, inputs: Dict[str, Any]) -> ToolResult

    Use @register decorator for auto-registration.
    """

    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {}

    @abstractmethod
    async def execute(self, inputs: dict[str, Any]) -> ToolResult:
        ...

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
        )


class ToolRegistry:
    """Runtime tool registry — manages loaded tool instances"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_definitions(self) -> list[ToolDefinition]:
        return [t.to_definition() for t in self._tools.values()]

    def get_names(self) -> list[str]:
        return list(self._tools.keys())

    async def execute(self, name: str, inputs: dict[str, Any]) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(success=False, error=f"Unknown tool: {name}")
        return await tool.execute(inputs)

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """Convert to OpenAI/Claude function-calling format"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in self._tools.values()
        ]


def discover_tools(tools_dir: str | None = None) -> ToolRegistry:
    """Auto-discover all @register-decorated tools from a directory.

    Scans all .py files in tools_dir (default: same directory as this file),
    imports them, collects @register-decorated classes, and returns a ToolRegistry.
    """
    if tools_dir is None:
        tools_dir = os.path.dirname(os.path.abspath(__file__))

    registry = ToolRegistry()
    tools_path = Path(tools_dir)

    # Import all .py files in the tools directory
    for py_file in sorted(tools_path.glob("*.py")):
        if py_file.name.startswith("_") or py_file.name == "base.py":
            continue
        module_name = py_file.stem
        try:
            importlib.import_module(f"agent_system.tools.{module_name}")
            logger.debug(f"Loaded tool module: {module_name}")
        except Exception as e:
            logger.warning(f"Failed to load tool module {module_name}: {e}")

    # Instantiate all registered tool classes
    for name, cls in _tool_classes.items():
        try:
            instance = cls()
            registry.register(instance)
            logger.info(f"Discovered tool: {name}")
        except Exception as e:
            logger.warning(f"Failed to instantiate tool {name}: {e}")

    return registry


def filter_registry(registry: ToolRegistry, enabled_names: list[str]) -> ToolRegistry:
    """Filter registry to only include enabled tools"""
    filtered = ToolRegistry()
    for name in enabled_names:
        tool = registry.get(name)
        if tool:
            filtered.register(tool)
    return filtered
