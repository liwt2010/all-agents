"""
Agent Registry — auto-discovery and registration for all @register_agent decorated classes.

Mirrors the ToolRegistry design (tools/base.py) but for agents.

Usage:
    from agent_system.core.registry import register_agent, agent_registry

    @register_agent
    class MyAgent(SmartAgent):
        agent_name = "my_agent"
        ...

    # Discover all agents in agents/ directory (called by agents/__init__.py)
    from agent_system.core.registry import discover_agents
    discover_agents()

    # Query
    agent_registry.all_classes()
    agent_registry.names_excluding("ceo_agent")
"""

import importlib
import inspect
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Runtime registry of agent classes + their default instances."""

    def __init__(self):
        self._classes: dict[str, type] = {}     # name -> agent class
        self._instances: dict[str, Any] = {}   # name -> lazily-instantiated default instance
        self._indexed: bool = False             # whether _classes is keyed by canonical agent_name

    def register(self, agent_cls: type) -> type:
        """Register an agent class. Idempotent on (class identity).

        Note: @register_agent decorator runs BEFORE the class body executes, so
        the `agent_name` attribute is not yet set. We use the class name as a
        provisional key and defer the `agent_name` validation to get_instance().
        """
        if not inspect.isclass(agent_cls):
            raise TypeError(f"register expects a class, got {type(agent_cls)}")
        # Use class name as provisional key (decorator runs pre-class-body)
        provisional_key = agent_cls.__name__
        # Replace later when agent_name becomes available — see _resolve_name()
        self._classes[provisional_key] = agent_cls
        # Invalidate cached canonical index — next query will reindex.
        self._indexed = False
        logger.debug(f"Registered (provisional): {provisional_key} → {agent_cls.__name__}")
        return agent_cls

    def _resolve_name(self, agent_cls: type) -> str | None:
        """Get the canonical agent_name from a registered class, or None if absent.

        Handles both regular Python classes (attribute on class) and Pydantic
        BaseModel subclasses (Field with a default value).
        """
        # Regular class attribute (non-Pydantic)
        if hasattr(agent_cls, "agent_name"):
            val = getattr(agent_cls, "agent_name", None)
            # Skip Pydantic FieldInfo (truthy but not a string)
            if isinstance(val, str) and val:
                return val
        # Pydantic model field
        model_fields = getattr(agent_cls, "model_fields", None)
        if model_fields and "agent_name" in model_fields:
            default = model_fields["agent_name"].default
            if isinstance(default, str) and default:
                return default
        # Pydantic v1 compat
        __fields__ = getattr(agent_cls, "__fields__", None)
        if __fields__ and "agent_name" in __fields__:
            default = __fields__["agent_name"].default
            if isinstance(default, str) and default:
                return default
        return None

    def _rebuild_index(self):
        """Re-index by canonical agent_name once all classes have run their bodies.
        Called automatically from get_instance / all_names / count."""
        if not hasattr(self, "_indexed") or not self._indexed:
            new_classes: dict[str, type] = {}
            for cls in self._classes.values():
                name = self._resolve_name(cls)
                if name is None:
                    logger.warning(
                        f"Agent class {cls.__name__} has no agent_name attribute — skipping"
                    )
                    continue
                if name in new_classes and new_classes[name] is not cls:
                    logger.debug(f"Agent {name} already indexed, overwriting with {cls.__name__}")
                new_classes[name] = cls
            self._classes = new_classes
            self._indexed = True

    def get_class(self, name: str) -> type | None:
        self._rebuild_index()
        return self._classes.get(name)

    def get_instance(self, name: str) -> Any | None:
        """Get or lazily create the default instance for `name`."""
        self._rebuild_index()
        if name not in self._instances:
            cls = self._classes.get(name)
            if cls is None:
                return None
            try:
                self._instances[name] = cls()
            except Exception as e:
                logger.warning(f"Failed to instantiate agent {name}: {e}")
                return None
        return self._instances.get(name)

    def all_classes(self) -> list[type]:
        self._rebuild_index()
        return list(self._classes.values())

    def all_names(self) -> list[str]:
        self._rebuild_index()
        return list(self._classes.keys())

    def all_instances(self) -> list[Any]:
        return [self.get_instance(n) for n in self._classes if self.get_instance(n) is not None]

    def names_excluding(self, excluded: str) -> list[str]:
        """Return all registered names except `excluded` (the self-agent)."""
        self._rebuild_index()
        return [n for n in self._classes if n != excluded]

    def instances_excluding(self, excluded: str) -> list[Any]:
        """Return instances of all agents except `excluded`, preserving registration order."""
        self._rebuild_index()
        result = []
        for name in self._classes:
            if name == excluded:
                continue
            inst = self.get_instance(name)
            if inst is not None:
                result.append(inst)
        return result

    def count(self) -> int:
        self._rebuild_index()
        return len(self._classes)

    def reset(self):
        """Clear all registrations. Used in tests."""
        self._classes.clear()
        self._instances.clear()
        self._indexed = False


# Global registry (singleton)
agent_registry = AgentRegistry()


def register_agent(cls: type) -> type:
    """Decorator: register an agent class with the global AgentRegistry.

    Usage:
        @register_agent
        class ProductAgent(SmartAgent):
            agent_name = "product_agent"
            ...
    """
    agent_registry.register(cls)
    return cls


def discover_agents(agents_dir: str | None = None) -> AgentRegistry:
    """Scan a directory for `*_agent.py` modules and import them, triggering @register_agent.

    Called by `agent_system.agents.__init__`. Safe to call multiple times — registration
    is idempotent and a duplicate import only re-evaluates decorators (last write wins).
    """
    if agents_dir is None:
        agents_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "agents",
        )

    agents_path = Path(agents_dir)
    if not agents_path.exists():
        logger.warning(f"Agents directory not found: {agents_dir}")
        return agent_registry

    for py_file in sorted(agents_path.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = py_file.stem
        try:
            importlib.import_module(f"agent_system.agents.{module_name}")
            logger.debug(f"Loaded agent module: {module_name}")
        except Exception as e:
            logger.warning(f"Failed to load agent module {module_name}: {e}")

    return agent_registry