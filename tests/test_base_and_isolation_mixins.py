"""
Tests: BaseMixin + GroupIsolationMixin
"""

import asyncio
import pytest
from datetime import datetime, timezone

from agent_system.core.mixins.base import BaseMixin
from agent_system.core.mixins.group_isolation import GroupIsolationMixin
from agent_system.core.auth.context import (
    TenantContext,
    _UserStub,
    CrossTenantAccessError,
    set_tenant_context,
    reset_tenant_context,
    get_current_tenant,
)


# ── BaseMixin ──

class TestBaseMixin:
    def test_basic_attributes(self):
        class A(BaseMixin):
            agent_name: str = "test_a"
            agent_capabilities: list = ["x", "y"]

        a = A()
        assert a.agent_name == "test_a"
        assert a.has_capability("x")
        assert a.has_capability("Y")  # case-insensitive
        assert not a.has_capability("z")
        assert a.capabilities_summary() == "x, y"

    def test_empty_capabilities(self):
        class A(BaseMixin):
            pass

        a = A()
        assert a.capabilities_summary() == "no capabilities"

    def test_custom_event_bus(self):
        """Pass a custom event bus to BaseMixin via _event_bus"""
        published = []

        class FakeBus:
            async def publish(self, event):
                published.append(event)

        bus = FakeBus()
        a = BaseMixin()
        a._event_bus = bus

        async def run():
            await a.emit_event("test.event", task_id="t1", data={"x": 1})
        asyncio.run(run())

        assert len(published) == 1
        assert published[0].event_type == "test.event"
        assert published[0].agent_name == a.agent_name
        assert published[0].data == {"x": 1}

    def test_remember_and_recall(self):
        from agent_system.memory.graph import reset_graph, GraphNode, NodeType

        reset_graph()
        a = BaseMixin()
        node = GraphNode(id="bm-test-1", type=NodeType.TASK, content={"x": 1})
        a.remember(node)
        found = a.recall_nodes(NodeType.TASK)
        assert any(n.id == "bm-test-1" for n in found)
        reset_graph()


# ── GroupIsolationMixin ──

class TestGroupIsolationMixin:
    def setup_method(self):
        from agent_system.memory.graph import reset_graph
        reset_graph()

    def test_tenant_id_default(self):
        class A(GroupIsolationMixin):
            pass

        a = A()
        assert a._current_tenant_id() == "default"

    def test_tenant_id_from_context(self):
        class A(GroupIsolationMixin):
            pass

        a = A()
        ctx = TenantContext(user=_UserStub(user_id="u1", tenant_id="acme"))
        token = set_tenant_context(ctx)
        try:
            assert a._current_tenant_id() == "acme"
        finally:
            reset_tenant_context(token)

    def test_remember_isolated_tags_tenant(self):
        from agent_system.memory.graph import GraphNode, NodeType, get_graph

        class A(GroupIsolationMixin):
            pass

        a = A()
        ctx = TenantContext(user=_UserStub(user_id="u1", tenant_id="acme", group_ids=["g1"]))
        token = set_tenant_context(ctx)
        try:
            node = GraphNode(id="iso-1", type=NodeType.TASK, content={"x": 1})
            a.remember_isolated(node)
            g = get_graph()
            stored = g.get_node("iso-1")
            assert stored.metadata["tenant_id"] == "acme"
            assert "g1" in stored.metadata["group_ids"]
        finally:
            reset_tenant_context(token)

    def test_recall_tenant_nodes_filters_by_tenant(self):
        """Nodes from other tenants are excluded."""
        from agent_system.memory.graph import GraphNode, NodeType, get_graph

        g = get_graph()
        g.add_node(GraphNode(id="n1", type=NodeType.TASK, content={}, metadata={"tenant_id": "acme"}))
        g.add_node(GraphNode(id="n2", type=NodeType.TASK, content={}, metadata={"tenant_id": "beta"}))
        g.add_node(GraphNode(id="n3", type=NodeType.TASK, content={}, metadata={"tenant_id": "acme"}))
        g.add_node(GraphNode(id="n4", type=NodeType.TASK, content={}, metadata={}))  # default

        class A(GroupIsolationMixin):
            pass

        a = A()

        # Switch to acme tenant
        ctx = TenantContext(user=_UserStub(user_id="u1", tenant_id="acme"))
        token = set_tenant_context(ctx)
        try:
            results = a.recall_tenant_nodes(NodeType.TASK)
            ids = {n.id for n in results}
            assert "n1" in ids
            assert "n3" in ids
            assert "n2" not in ids  # beta, different tenant
            # n4 has no tenant_id, defaults to "default" — excluded
            assert "n4" not in ids
        finally:
            reset_tenant_context(token)

    def test_tag_for_tenant(self):
        class A(GroupIsolationMixin):
            pass

        a = A()
        ctx = TenantContext(user=_UserStub(user_id="u99", tenant_id="co", group_ids=["g1"]))
        token = set_tenant_context(ctx)
        try:
            tag = a.tag_for_tenant({"custom": 1})
            assert tag["tenant_id"] == "co"
            assert tag["owner_id"] == "u99"
            assert "g1" in tag["group_ids"]
            assert tag["custom"] == 1
        finally:
            reset_tenant_context(token)

    def test_assert_same_tenant_raises(self):
        class A(GroupIsolationMixin):
            pass

        a = A()
        ctx = TenantContext(user=_UserStub(user_id="u1", tenant_id="acme"))
        token = set_tenant_context(ctx)
        try:
            with pytest.raises(CrossTenantAccessError):
                a.assert_same_tenant("other_tenant")
        finally:
            reset_tenant_context(token)

    def test_assert_same_tenant_passes(self):
        class A(GroupIsolationMixin):
            pass

        a = A()
        ctx = TenantContext(user=_UserStub(user_id="u1", tenant_id="acme"))
        token = set_tenant_context(ctx)
        try:
            a.assert_same_tenant("acme")  # same — no error
            a.assert_same_tenant("")  # empty — no error
        finally:
            reset_tenant_context(token)

    def test_recall_tenant_visible_respects_visibility(self):
        """With no user context, defaults to tenant-only view."""
        from agent_system.memory.graph import GraphNode, NodeType, get_graph

        g = get_graph()
        # Public node (tenant_id=default, visibility=tenant_public)
        g.add_node(GraphNode(
            id="public-1", type=NodeType.TASK, content={},
            metadata={"tenant_id": "default", "visibility": "tenant_public"},
        ))
        # Private node in same tenant
        g.add_node(GraphNode(
            id="private-1", type=NodeType.TASK, content={},
            metadata={"tenant_id": "default", "visibility": "private"},
        ))
        # Private node in different tenant
        g.add_node(GraphNode(
            id="other-private", type=NodeType.TASK, content={},
            metadata={"tenant_id": "other", "visibility": "private"},
        ))

        class A(GroupIsolationMixin):
            pass

        a = A()
        # Without user context: falls back to tenant-only filter
        results = a.recall_tenant_visible(NodeType.TASK)
        ids = {n.id for n in results}
        assert "public-1" in ids
        assert "private-1" in ids
        assert "other-private" not in ids


# ── Combined: BaseMixin + GroupIsolationMixin ──

class TestCombinedMixins:
    def test_both_mixins_compose(self):
        from agent_system.memory.graph import GraphNode, NodeType, reset_graph

        reset_graph()

        class FullAgent(BaseMixin, GroupIsolationMixin):
            agent_name: str = "full_agent"
            agent_capabilities: list = ["isolation", "events"]
            description: str = "Agent with both mixins"

        a = FullAgent()
        assert a.has_capability("isolation")
        assert a.has_capability("events")

        # Mix both
        ctx = TenantContext(user=_UserStub(user_id="u1", tenant_id="co", group_ids=["g1"]))
        token = set_tenant_context(ctx)
        try:
            node = GraphNode(id="combined-1", type=NodeType.TASK, content={"x": 1})
            a.remember_isolated(node)

            # Both helpers work
            assert a._current_tenant_id() == "co"
            stored = a.recall_tenant_nodes(NodeType.TASK)
            assert any(n.id == "combined-1" for n in stored)
        finally:
            reset_tenant_context(token)
        reset_graph()
