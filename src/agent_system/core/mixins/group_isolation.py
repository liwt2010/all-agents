"""
GroupIsolationMixin — PLATFORM §5.2, §7.7, §29

Tenant + Group isolation for memory reads and writes. Ensures agents
operating in a tenant context can only see/act on resources within
that tenant (or those explicitly shared with the tenant's groups).

This mixin assumes a UserContext / tenant_id is available. It works with
the access_control module's 6-space model (private / perm_group / group /
project / external / tenant_public).
"""

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from agent_system.core.auth.context import TenantContext, get_current_tenant

logger = logging.getLogger(__name__)


class GroupIsolationMixin:
    """
    Mixin that scopes memory + tool access to a tenant/group.

    Agents that mix this in automatically:
      - Read nodes only from their own tenant
      - Tag newly-written nodes with their tenant_id + group_id
      - Reject cross-tenant reads
    """

    agent_name: str = "isolated_agent"
    # Subclasses can override
    default_tenant_id: str = "default"
    default_group_id: str = "default"

    def _current_tenant_id(self) -> str:
        """Get the tenant id for this agent (or default)."""
        ctx = get_current_tenant()
        if ctx and ctx.user and ctx.user.tenant_id:
            return ctx.user.tenant_id
        return self.default_tenant_id

    def _current_group_ids(self) -> List[str]:
        """Get the group ids the current user belongs to."""
        ctx = get_current_tenant()
        if ctx and ctx.user:
            return list(ctx.user.group_ids)
        return [self.default_group_id]

    def _current_user_id(self) -> str:
        """Get the current user id."""
        ctx = get_current_tenant()
        if ctx and ctx.user:
            return ctx.user.user_id
        return self.agent_name

    # ── Tenant-scoped memory helpers ──

    def _get_graph(self) -> Any:
        """Lazy access to the memory graph. Looks for BaseMixin's _get_graph
        first, then falls back to the global singleton."""
        # BaseMixin provides this; if not present, use the global.
        getter = getattr(self, "_get_graph_base", None)
        if getter is None:
            from agent_system.memory.graph import get_graph
            return get_graph()
        return getter()

    def remember_isolated(self, node):
        """Add a node, tagged with the current tenant_id."""
        # Ensure the node has tenant_id set
        if hasattr(node, "metadata") and node.metadata is not None:
            if "tenant_id" not in node.metadata:
                node.metadata["tenant_id"] = self._current_tenant_id()
            if "group_ids" not in node.metadata:
                node.metadata["group_ids"] = self._current_group_ids()
        graph = self._get_graph()
        return graph.add_node(node)

    def recall_tenant_nodes(self, node_type=None, **filters):
        """Find nodes within the current tenant only."""
        graph = self._get_graph()
        candidates = graph.find_nodes(node_type=node_type, **filters)
        tenant_id = self._current_tenant_id()
        return [
            n for n in candidates
            if (n.metadata or {}).get("tenant_id", "default") == tenant_id
        ]

    def recall_tenant_visible(self, node_type=None, **filters):
        """
        Find nodes that the current user can see, considering:
          - Same tenant
          - Tenant public visibility
          - Own resources
          - Same group membership
        """
        from agent_system.core.auth.context import get_current_tenant
        from agent_system.core.access_control import (
            SpaceVisibility,
            Resource,
            UserContext,
            access_control,
        )

        ctx = get_current_tenant()
        if ctx is None or ctx.user is None:
            # No tenant context — fall back to same-tenant-only
            return self.recall_tenant_nodes(node_type=node_type, **filters)

        graph = self._get_graph()
        all_nodes = graph.find_nodes(node_type=node_type, **filters)
        visible = []
        for n in all_nodes:
            # Build a Resource-like object to check access
            visibility = (n.metadata or {}).get("visibility", "private")
            try:
                visibility_enum = SpaceVisibility(visibility)
            except ValueError:
                visibility_enum = SpaceVisibility.PRIVATE

            resource = Resource(
                id=n.id,
                type="node",
                tenant_id=(n.metadata or {}).get("tenant_id", "default"),
                owner_id=(n.metadata or {}).get("owner_id", ""),
                visibility=visibility_enum,
                group_ids=(n.metadata or {}).get("group_ids", []),
                project_ids=(n.metadata or {}).get("project_ids", []),
                shared_with=(n.metadata or {}).get("shared_with", []),
            )
            if access_control.can_read(ctx.user, resource):
                visible.append(n)
        return visible

    # ── Resource tagging helpers ──

    def tag_for_tenant(self, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Build a metadata dict tagged with current tenant/group/user."""
        md = dict(metadata or {})
        md.setdefault("tenant_id", self._current_tenant_id())
        md.setdefault("owner_id", self._current_user_id())
        md.setdefault("group_ids", self._current_group_ids())
        return md

    # ── Cross-tenant safety ──

    def assert_same_tenant(self, resource_tenant_id: str) -> None:
        """Raise PermissionError if a resource is in a different tenant."""
        my_tenant = self._current_tenant_id()
        if resource_tenant_id and resource_tenant_id != my_tenant:
            from agent_system.core.auth.context import CrossTenantAccessError
            raise CrossTenantAccessError(
                f"Agent {self.agent_name} (tenant={my_tenant}) "
                f"cannot access resource from tenant={resource_tenant_id}"
            )
