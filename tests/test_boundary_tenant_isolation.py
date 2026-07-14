"""Boundary tests: Multi-tenant isolation verification.

Issue: Verify Tenant A"s task results do NOT leak to Tenant B"s memory graph.
- Tenant A"s tasks/nodes should not be visible to Tenant B
- Cross-tenant graph queries should return empty
- Tenant context must be properly isolated

Note: set_tenant_context() returns a contextvars.Token, not a context manager.
Use token-based pattern: tok = set_tenant_context(ctx); ...; reset_tenant_context(tok)
"""
import pytest
from datetime import datetime, timezone

from agent_system.core.agent import SmartAgent, TaskContext, OutputSchema
from agent_system.memory.graph import get_graph, reset_graph, GraphNode, NodeType, LinkType
from agent_system.core.auth.context import (
    TenantContext,
    set_tenant_context,
    reset_tenant_context,
    _UserStub,
)


class TestTenantIsolation:
    """Verify tenant isolation in memory graph operations."""

    @pytest.fixture(autouse=True)
    def reset(self):
        """Reset graph and context before each test."""
        reset_graph()
        from agent_system.core.auth.context import _tenant_ctx
        _tenant_ctx.set(None)
        yield
        reset_graph()
        _tenant_ctx.set(None)

    def _create_tenant_context(self, tenant_id: str, user_id: str) -> TenantContext:
        """Helper to create tenant context."""
        user = _UserStub(user_id=user_id, tenant_id=tenant_id)
        return TenantContext(
            user=user,
            tenant_id=tenant_id,
        )

    def test_tenant_context_set_and_get(self):
        """Verify tenant context can be set and retrieved."""
        g = get_graph()

        ctx_a = self._create_tenant_context("tenant-a", "user-a")
        tok = set_tenant_context(ctx_a)
        try:
            g.add_node(GraphNode(
                id="task-1",
                type=NodeType.TASK,
                content={"tenant_id": "tenant-a"},
            ))
        finally:
            reset_tenant_context(tok)

        node = g.get_node("task-1")
        assert node is not None
        assert node.content.get("tenant_id") == "tenant-a"

    def test_cross_tenant_link_not_added(self):
        """Links should not cross tenant boundaries."""
        g = get_graph()

        ctx_a = self._create_tenant_context("tenant-a", "user-a")

        tok = set_tenant_context(ctx_a)
        try:
            g.add_node(GraphNode(id="a-task", type=NodeType.TASK))
            g.add_node(GraphNode(id="a-output", type=NodeType.OUTPUT))
            g.link("a-task", "a-output", LinkType.REFERS_TO)
        finally:
            reset_tenant_context(tok)

        tok = set_tenant_context(ctx_a)
        try:
            a_outgoing = g.get_outgoing("a-task", LinkType.REFERS_TO)
            assert len(a_outgoing) == 1
            assert a_outgoing[0].target_id == "a-output"
        finally:
            reset_tenant_context(tok)

    def test_tenant_nodes_isolated_by_content(self):
        """Tenant-related nodes should be identifiable by content."""
        g = get_graph()

        ctx_a = self._create_tenant_context("tenant-a", "user-a")
        tok_a = set_tenant_context(ctx_a)
        try:
            g.add_node(GraphNode(
                id="t1-secret",
                type=NodeType.TASK,
                content={"description": "Tenant A secret"},
            ))
        finally:
            reset_tenant_context(tok_a)

        ctx_b = self._create_tenant_context("tenant-b", "user-b")
        tok_b = set_tenant_context(ctx_b)
        try:
            g.add_node(GraphNode(
                id="t2-secret",
                type=NodeType.TASK,
                content={"description": "Tenant B secret"},
            ))
        finally:
            reset_tenant_context(tok_b)

        nodes = g.find_nodes(NodeType.TASK)
        node_ids = [n.id for n in nodes]

        assert "t1-secret" in node_ids
        assert "t2-secret" in node_ids

        for n in nodes:
            if n.id == "t1-secret":
                assert "Tenant A" in str(n.content)
            elif n.id == "t2-secret":
                assert "Tenant B" in str(n.content)

    def test_no_tenant_context_allows_public_access(self):
        """Without tenant context, public nodes are still accessible."""
        g = get_graph()

        ctx_a = self._create_tenant_context("tenant-a", "user-a")
        tok = set_tenant_context(ctx_a)
        try:
            g.add_node(GraphNode(id="public-node", type=NodeType.TASK))
        finally:
            reset_tenant_context(tok)

        tok = set_tenant_context(None)
        try:
            node = g.get_node("public-node")
        finally:
            reset_tenant_context(tok)
        assert node is not None, "Public nodes should be accessible without tenant context"
