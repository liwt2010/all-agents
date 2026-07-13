"""
Storage backend abstraction for MultiLinkGraph (PR-9).

Inspired by UAMS (Universal Agent Memory) design — 6 backend pattern.
all-agents implements JSON / SQLite / PostgreSQL for PR-9 (PR-10 adds Redis).

Production: PostgreSQL (write) + Redis (read cache)
Development: SQLite (zero-ops single file)
Migration:   JSON (import/export format only)

See docs/STORAGE.md for full design.
"""

from typing import TYPE_CHECKING, List, Optional, Protocol

if TYPE_CHECKING:
    from agent_system.memory.graph import (
        MultiLinkGraph,
        GraphNode,
        GraphLink,
        NodeType,
    )


class GraphStorage(Protocol):
    """Storage backend interface for MultiLinkGraph nodes and links."""

    # ── Node operations ──

    def save_node(self, node: "GraphNode") -> None:
        """Save or update a single node."""

    def load_node(self, node_id: str) -> Optional["GraphNode"]:
        """Load a single node by id. Returns None if not found."""

    def delete_node(self, node_id: str) -> bool:
        """Delete a node. Returns True if existed."""

    def list_nodes(self, node_type: Optional["NodeType"] = None) -> list["GraphNode"]:
        """List all nodes, optionally filtered by type."""

    # ── Link operations ──

    def save_link(self, link: "GraphLink") -> None:
        """Save or update a single link."""

    def list_links(
        self,
        node_id: str,
        direction: str = "out",
        link_type: str | None = None,
    ) -> list["GraphLink"]:
        """List links connected to a node.
        direction: 'out' = outgoing, 'in' = incoming, 'both' = either.
        """

    def delete_links_for_node(self, node_id: str) -> int:
        """Delete all links where source_id or target_id == node_id. Returns count deleted."""

    # ── Bulk operations ──

    def save_graph(self, graph: "MultiLinkGraph") -> int:
        """Save entire graph atomically. Returns count of nodes saved."""

    def load_graph(self, graph: "MultiLinkGraph") -> int:
        """Load all nodes + links into the in-memory graph. Returns count of nodes loaded."""

    # ── Lifecycle ──

    def init(self) -> None:
        """Initialize schema (tables, indexes). Idempotent — safe to call multiple times."""

    def close(self) -> None:
        """Release connections / file handles."""

    # ── Introspection (for health checks) ──

    def backend_name(self) -> str:
        """Human-readable backend name for logging / metrics."""

    def ping(self) -> bool:
        """Health check. Returns True if backend is reachable."""