"""
Custom Agent package — exports the public API.

PR 8 / agents/custom/__init__.py
"""

from agent_system.agents.custom.base import (
    CustomAgent,
    CustomAgentConfig,
    CustomAgentSafety,
)
from agent_system.agents.custom.loader import (
    CustomAgentLoadError,
    load_from_directory,
    load_from_yaml_file,
)
from agent_system.agents.custom.registry import (
    CustomAgentRegistry,
    get_custom_agent_registry,
)

__all__ = [
    "CustomAgent",
    "CustomAgentConfig",
    "CustomAgentSafety",
    "CustomAgentLoadError",
    "CustomAgentRegistry",
    "get_custom_agent_registry",
    "load_from_directory",
    "load_from_yaml_file",
]