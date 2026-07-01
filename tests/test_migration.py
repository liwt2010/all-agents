"""
Tests: Data migration engine
"""

import pytest
from datetime import datetime, timezone

from agent_system.memory.graph import (
    MultiLinkGraph,
    GraphNode,
    NodeType,
    reset_graph,
)
from agent_system.migration.engine import (
    MigrationEngine,
    MigrationConfig,
    MigrationResult,
    MigrationStatus,
    MigrationTemplate,
)


@pytest.fixture
def fresh_graph():
    g = MultiLinkGraph()
    # Seed with sample data
    g.add_node(GraphNode(
        id="t-1", type=NodeType.TASK,
        content={"x": 1}, metadata={"tenant_id": "acme"},
    ))
    g.add_node(GraphNode(
        id="t-2", type=NodeType.TASK,
        content={"x": 2}, metadata={"tenant_id": "acme"},
    ))
    g.add_node(GraphNode(
        id="t-other", type=NodeType.TASK,
        content={"x": 99}, metadata={"tenant_id": "beta"},
    ))
    g.add_node(GraphNode(
        id="exp-1", type=NodeType.EXPERIENCE,
        content={"y": 1}, metadata={"tenant_id": "acme"},
    ))
    return g


class TestMigrationConfig:
    def test_basic_config(self):
        c = MigrationConfig(
            id="m1", template=MigrationTemplate.FULL_TENANT_EXPORT,
            source_tenant_id="acme", requested_by="alice",
        )
        assert c.approved_by == ""  # Default
        assert c.dry_run is False
        assert c.batch_size == 100  # Default

    def test_extra_fields(self):
        c = MigrationConfig(
            id="m1", template=MigrationTemplate.FULL_TENANT_EXPORT,
            source_tenant_id="acme",
            custom_field="hello",
        )
        assert c.custom_field == "hello"


class TestMigrationEngine:
    def test_pre_check_unapproved(self):
        engine = MigrationEngine()
        config = MigrationConfig(
            id="m1", template=MigrationTemplate.FULL_TENANT_EXPORT,
            source_tenant_id="acme",
            require_human_approval=True,
        )
        import asyncio
        ok, reason = asyncio.run(engine.pre_check(config))
        assert ok is False
        assert "approval" in reason.lower()

    def test_pre_check_approved(self):
        engine = MigrationEngine()
        config = MigrationConfig(
            id="m1", template=MigrationTemplate.FULL_TENANT_EXPORT,
            source_tenant_id="acme",
            require_human_approval=True,
            approved_by="cto",
        )
        import asyncio
        ok, _ = asyncio.run(engine.pre_check(config))
        assert ok is True

    def test_pre_check_invalid_batch_size(self):
        engine = MigrationEngine()
        config = MigrationConfig(
            id="m1", template=MigrationTemplate.FULL_TENANT_EXPORT,
            source_tenant_id="acme",
            require_human_approval=False,
            batch_size=0,
        )
        import asyncio
        ok, _ = asyncio.run(engine.pre_check(config))
        assert ok is False

    def test_select_nodes_filters_by_tenant(self, fresh_graph):
        engine = MigrationEngine(graph=fresh_graph)
        config = MigrationConfig(
            id="m1", template=MigrationTemplate.FULL_TENANT_EXPORT,
            source_tenant_id="acme",
            require_human_approval=False,
        )
        selected = engine.select_nodes(config)
        ids = {n.id for n in selected}
        assert "t-1" in ids
        assert "t-2" in ids
        assert "exp-1" in ids
        assert "t-other" not in ids  # beta tenant

    def test_select_nodes_by_type(self, fresh_graph):
        engine = MigrationEngine(graph=fresh_graph)
        config = MigrationConfig(
            id="m1", template=MigrationTemplate.FULL_TENANT_EXPORT,
            source_tenant_id="acme",
            require_human_approval=False,
            node_types=["task"],  # only tasks
        )
        selected = engine.select_nodes(config)
        assert all(n.type == NodeType.TASK for n in selected)
        assert not any(n.type == NodeType.EXPERIENCE for n in selected)

    def test_checksum_stable(self, fresh_graph):
        engine = MigrationEngine(graph=fresh_graph)
        config = MigrationConfig(
            id="m1", template=MigrationTemplate.FULL_TENANT_EXPORT,
            source_tenant_id="acme",
            require_human_approval=False,
        )
        selected = engine.select_nodes(config)
        c1 = engine.checksum(selected)
        c2 = engine.checksum(selected)
        assert c1 == c2
        assert len(c1) == 64  # SHA-256

    def test_run_dry_run(self, fresh_graph):
        engine = MigrationEngine(graph=fresh_graph)
        config = MigrationConfig(
            id="m-dry", template=MigrationTemplate.FULL_TENANT_EXPORT,
            source_tenant_id="acme",
            require_human_approval=False,
            dry_run=True,
        )
        import asyncio
        result = asyncio.run(engine.run(config))
        assert result.status == MigrationStatus.COMPLETED
        # Dry run should not add new nodes
        assert "acme::t-1" not in fresh_graph._nodes

    @pytest.mark.asyncio
    async def test_run_full_migration(self, fresh_graph):
        engine = MigrationEngine(graph=fresh_graph)
        config = MigrationConfig(
            id="m-full", template=MigrationTemplate.FULL_TENANT_EXPORT,
            source_tenant_id="acme",
            target_tenant_id="beta",
            require_human_approval=False,
        )
        result = await engine.run(config)
        assert result.status == MigrationStatus.COMPLETED
        assert result.total_nodes == 3
        assert result.processed_nodes == 3
        # Source nodes remain
        assert "t-1" in fresh_graph._nodes
        # Target nodes are prefixed
        assert "beta::t-1" in fresh_graph._nodes
        assert "beta::t-2" in fresh_graph._nodes
        # The target is in beta tenant
        target_node = fresh_graph._nodes["beta::t-1"]
        assert target_node.metadata["tenant_id"] == "beta"

    @pytest.mark.asyncio
    async def test_run_exceeds_max(self, fresh_graph):
        engine = MigrationEngine(graph=fresh_graph)
        config = MigrationConfig(
            id="m-big", template=MigrationTemplate.FULL_TENANT_EXPORT,
            source_tenant_id="acme",
            require_human_approval=False,
            max_total_nodes=1,  # artificially low
        )
        result = await engine.run(config)
        assert result.status == MigrationStatus.FAILED
        assert "max_total_nodes" in result.error

    def test_audit_log(self, fresh_graph):
        engine = MigrationEngine(graph=fresh_graph)
        config = MigrationConfig(
            id="m-aud", template=MigrationTemplate.FULL_TENANT_EXPORT,
            source_tenant_id="acme",
            require_human_approval=False,
            dry_run=True,
        )
        import asyncio
        asyncio.run(engine.run(config))
        log = engine.get_audit_log()
        assert len(log) >= 1
        assert log[-1]["config_id"] == "m-aud"
        assert log[-1]["outcome"] == "dry_run"

    @pytest.mark.asyncio
    async def test_progress_pct(self, fresh_graph):
        engine = MigrationEngine(graph=fresh_graph)
        config = MigrationConfig(
            id="m-prog", template=MigrationTemplate.FULL_TENANT_EXPORT,
            source_tenant_id="acme",
            target_tenant_id="beta",
            require_human_approval=False,
        )
        result = await engine.run(config)
        assert result.progress_pct == 1.0  # All done

    @pytest.mark.asyncio
    async def test_tenant_to_tenant(self, fresh_graph):
        engine = MigrationEngine(graph=fresh_graph)
        config = MigrationConfig(
            id="m-t2t", template=MigrationTemplate.TENANT_TO_TENANT,
            source_tenant_id="acme",
            target_tenant_id="gamma",
            require_human_approval=False,
        )
        result = await engine.run(config)
        assert result.status == MigrationStatus.COMPLETED
        assert "gamma::t-1" in fresh_graph._nodes
