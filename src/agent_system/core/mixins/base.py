"""
BaseMixin — PLATFORM §5.2

General-purpose capabilities that every Agent should have, separate from the
business logic. Provides:

  - Event publishing helpers (typed, with event bus injection)
  - Memory graph access (tenant-scoped read helpers)
  - Tooling helpers
  - Common input/output validation

Lightweight: does NOT depend on SmartAgent. Designed to be mixed into any
agent class via standard Python multiple inheritance.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class AgentEvent(BaseModel):
    """Lightweight event payload."""
    event_type: str
    agent_name: str
    task_id: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict[str, Any] = Field(default_factory=dict)
    severity: str = "info"  # info / warning / error / critical


class BaseMixin:
    """
    Drop-in base capabilities for any Agent.

    Subclasses must set:
      agent_name: str
    """

    agent_name: str = "base_agent"
    description: str = "Base agent"
    agent_capabilities: list[str] = []

    # Subclasses can override these to plug in their own implementations
    _event_bus: Any = None  # An object with `publish(event)` method
    _graph: Any = None      # An object exposing the MultiLinkGraph API

    def _get_event_bus(self) -> Any:
        """Lazy access to the event bus. Defaults to a no-op."""
        if self._event_bus is None:
            from agent_system.core.event_bus import event_bus
            return event_bus
        return self._event_bus

    def _get_graph_base(self) -> Any:
        """Lazy access to the memory graph. Defaults to the global singleton."""
        if self._graph is None:
            from agent_system.memory.graph import get_graph
            return get_graph()
        return self._graph

    # Alias for compat with GroupIsolationMixin
    _get_graph = _get_graph_base

    # ── Event helpers ──

    async def emit_event(
        self,
        event_type: str,
        task_id: str = "",
        data: dict[str, Any] | None = None,
        severity: str = "info",
    ):
        """Emit an event to the bus."""
        event = AgentEvent(
            event_type=event_type,
            agent_name=self.agent_name,
            task_id=task_id,
            data=data or {},
            severity=severity,
        )
        bus = self._get_event_bus()
        try:
            await bus.publish(event)
        except Exception as e:
            logger.debug(f"Event publish failed: {e}")

    def emit_event_sync(
        self,
        event_type: str,
        task_id: str = "",
        data: dict[str, Any] | None = None,
        severity: str = "info",
    ):
        """Synchronous event emission (when not in async context)."""
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            return loop.create_task(
                self.emit_event(event_type, task_id, data, severity)
            )
        except RuntimeError:
            # No running loop — skip
            logger.debug("No event loop, skipping event emit")

    # ── Memory helpers ──

    def remember(self, node):
        """Add a node to the memory graph."""
        graph = self._get_graph()
        return graph.add_node(node)

    def recall_nodes(self, node_type=None, **filters):
        """Find nodes in memory."""
        graph = self._get_graph()
        return graph.find_nodes(node_type=node_type, **filters)

    def link_nodes(self, source_id, target_id, link_type, **kwargs):
        """Link two nodes in memory."""
        graph = self._get_graph()
        return graph.link(source_id, target_id, link_type, **kwargs)

    # ── Capability checks ──

    def has_capability(self, capability: str) -> bool:
        """Check if this agent has a given capability."""
        return capability.lower() in [c.lower() for c in self.agent_capabilities]

    def capabilities_summary(self) -> str:
        """Human-readable capability list."""
        if not self.agent_capabilities:
            return "no capabilities"
        return ", ".join(self.agent_capabilities)
