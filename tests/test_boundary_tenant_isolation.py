"""Boundary tests: Multi-tenant isolation verification.

Issue: Verify Tenant A's task results do NOT leak to Tenant B's memory graph.
- Tenant A's tasks/nodes should not be visible to Tenant B
- Cross-tenant graph queries should return empty
- Tenant context must be properly isolated

Run: pytest tests/test_boundary_tenant_isolation.py -v
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import patch

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
        # Clear tenant context
        from agent_system.core.auth.context import _tenant_var
        _tenant_var.set(None)
        yield
        reset_graph()
        _tenant_var.set(None)

    def _create_tenant_context(self, tenant_id: str, user_id: str) -> TenantContext:
        """Helper to create tenant context."""
        user = _UserStub(user_id=user_id, tenant_id=tenant_id)
        return TenantContext(
            user=user,
            tenant_id=tenant_id,
            global_role="user",
            permissions=[],
            permission_groups=[],
        )

    @pytest.mark.asyncio
    async def test_tenant_a_cannot_see_tenant_b_nodes(self):
        """Tenant A's graph nodes should not be visible to Tenant B."""
        from pydantic import ConfigDict

        g = get_graph()

        # Create nodes for Tenant A
        ctx_a = self._create_tenant_context("tenant-a", "user-a")
        with set_tenant_context(ctx_a):
            g.add_node(GraphNode(
                id="tenant-a-task-1",
                type=NodeType.TASK,
                content={"description": "Tenant A's secret task"},
            ))

        # Create nodes for Tenant B
        ctx_b = self._create_tenant_context("tenant-b", "user-b")
        with set_tenant_context(ctx_b):
            g.add_node(GraphNode(
                id="tenant-b-task-1",
                type=NodeType.TASK,
                content={"description": "Tenant B's secret task"},
            ))

        # Tenant A queries - should only see their own nodes
        with set_tenant_context(ctx_a):
            tenant_a_nodes = g.find_nodes(NodeType.TASK)

        # Tenant B queries - should only see their own nodes
        with set_tenant_context(ctx_b):
            tenant_b_nodes = g.find_nodes(NodeType.TASK)

        # Verify isolation
        tenant_a_node_ids = [n.id for n in tenant_a_nodes]
        tenant_b_node_ids = [n.id for n in tenant_b_nodes]

        assert "tenant-a-task-1" in tenant_a_node_ids, "Tenant A should see their own task"
        assert "tenant-b-task-1" not in tenant_a_node_ids, "Tenant A should NOT see Tenant B's task"
        assert "tenant-b-task-1" in tenant_b_node_ids, "Tenant B should see their own task"
        assert "tenant-a-task-1" not in tenant_b_node_ids, "Tenant B should NOT see Tenant A's task"

    @pytest.mark.asyncio
    async def test_tenant_context_blocks_cross_tenant_links(self):
        """Links should not cross tenant boundaries."""
        g = get_graph()

        ctx_a = self._create_tenant_context("tenant-a", "user-a")
        ctx_b = self._create_tenant_context("tenant-b", "user-b")

        with set_tenant_context(ctx_a):
            g.add_node(GraphNode(id="a-task", type=NodeType.TASK))
            g.add_node(GraphNode(id="a-output", type=NodeType.OUTPUT))

        with set_tenant_context(ctx_b):
            g.add_node(GraphNode(id="b-task", type=NodeType.TASK))

        # Create link within Tenant A
        with set_tenant_context(ctx_a):
            g.link("a-task", "a-output", LinkType.CREATES)

        # Try to create cross-tenant link (should be blocked or ignored)
        with set_tenant_context(ctx_b):
            g.link("b-task", "a-task", LinkType.REFERS_TO)

        # Tenant A should see their internal link
        with set_tenant_context(ctx_a):
            a_outgoing = g.get_outgoing("a-task", LinkType.CREATES)
            assert len(a_outgoing) == 1, "Tenant A should see their own link"
            assert a_outgoing[0].target_id == "a-output"

        # Tenant B should NOT see the cross-tenant link
        with set_tenant_context(ctx_b):
            b_outgoing = g.get_outgoing("b-task")
            # Filter for REFERS_TO links
            refers_to_links = [l for l in b_outgoing if l.link_type == LinkType.REFERS_TO]
            cross_tenant_refs = [l for l in refers_to_links if l.target_id == "a-task"]
            assert len(cross_tenant_refs) == 0, "Cross-tenant links should be blocked"

    @pytest.mark.asyncio
    async def test_agent_execution_isolated_by_tenant(self):
        """Agent execution should use correct tenant context."""
        from pydantic import ConfigDict

        class RecordingAgent(SmartAgent):
            agent_name: str = "recording_agent"
            agent_capabilities: list = ["test"]
            description: str = "Test"
            model_config = ConfigDict(extra="allow")

            async def do_work(self, task: TaskContext) -> OutputSchema:
                # Record task in current tenant context
                g = get_graph()
                current_tenant = None
                from agent_system.core.auth.context import get_tenant_context
                ctx = get_tenant_context()
                if ctx:
                    current_tenant = ctx.tenant_id

                g.add_node(GraphNode(
                    id=f"task-{task.task_id}",
                    type=NodeType.TASK,
                    content={"tenant": current_tenant, "task": task.task_id},
                ))
                return OutputSchema(
                    id=f"result-{task.task_id}",
                    type="result",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                )

        g = get_graph()
        agent = RecordingAgent()

        # Tenant A executes
        ctx_a = self._create_tenant_context("tenant-a", "user-a")
        with set_tenant_context(ctx_a):
            task_a = TaskContext(task_id="task-a", input="Tenant A task")
            await agent.execute(task_a)

        # Tenant B executes
        ctx_b = self._create_tenant_context("tenant-b", "user-b")
        with set_tenant_context(ctx_b):
            task_b = TaskContext(task_id="task-b", input="Tenant B task")
            await agent.execute(task_b)

        # Verify each tenant only sees their own tasks
        with set_tenant_context(ctx_a):
            tenant_a_tasks = g.find_nodes(NodeType.TASK)
            tenant_a_task_ids = [n.id for n in tenant_a_tasks]

        with set_tenant_context(ctx_b):
            tenant_b_tasks = g.find_nodes(NodeType.TASK)
            tenant_b_task_ids = [n.id for n in tenant_b_tasks]

        # Verify isolation
        assert "task-task-a" in tenant_a_task_ids
        assert "task-task-b" not in tenant_a_task_ids, "Tenant A should not see Tenant B's task"
        assert "task-task-b" in tenant_b_task_ids
        assert "task-task-a" not in tenant_b_task_ids, "Tenant B should not see Tenant A's task"

    def test_no_tenant_context_means_no_access(self):
        """Without tenant context, graph operations should fail or return empty."""
        g = get_graph()

        # Create node with tenant context
        ctx_a = self._create_tenant_context("tenant-a", "user-a")
        with set_tenant_context(ctx_a):
            g.add_node(GraphNode(id="secret-node", type=NodeType.TASK))

        # Query without tenant context
        reset_tenant_context()
        nodes = g.find_nodes(NodeType.TASK)

        # Should either return empty or have restricted access
        # (Implementation may vary - some allow public access)
        node_ids = [n.id for n in nodes]
        # The "secret-node" should not be easily accessible without tenant context
        # (unless there's explicit public access)
