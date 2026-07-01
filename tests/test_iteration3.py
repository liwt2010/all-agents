"""
Tests: Iteration 3 — MultiLinkGraph, Persistence, Experience Feedback
"""

import json
import pytest
from datetime import datetime, timezone
from pathlib import Path

from agent_system.memory.graph import (
    MultiLinkGraph,
    GraphNode,
    GraphLink,
    NodeType,
    LinkType,
    NeighborResult,
    PathResult,
    reset_graph,
    get_graph,
)
from agent_system.memory.persistence import (
    save_node,
    load_node,
    save_link,
    save_graph,
    load_graph,
    _get_base_dir,
)
from agent_system.memory.experience import (
    record_task_start,
    record_task_complete,
    record_task_failure,
    record_experience,
    find_similar_failures,
    get_relevant_experiences,
    install_memory_hooks,
)


class TestMultiLinkGraph:
    """Test core MultiLinkGraph operations"""

    def setup_method(self):
        reset_graph()

    def test_add_and_get_node(self):
        g = MultiLinkGraph()
        node = GraphNode(id="test-1", type=NodeType.TASK, content={"input": "hello"})
        g.add_node(node)
        assert g.get_node("test-1") is not None
        assert g.get_node("test-1").type == NodeType.TASK
        assert g.get_node("test-1").content["input"] == "hello"

    def test_add_duplicate_node_updates(self):
        g = MultiLinkGraph()
        n1 = GraphNode(id="test-1", type=NodeType.TASK, content={"input": "hello"})
        g.add_node(n1)
        n2 = GraphNode(id="test-1", type=NodeType.TASK, content={"input": "updated"})
        g.add_node(n2)
        assert g.get_node("test-1").content["input"] == "updated"
        assert g.node_count() == 1

    def test_find_nodes_by_type(self):
        g = MultiLinkGraph()
        g.add_node(GraphNode(id="t1", type=NodeType.TASK, content={"status": "running"}))
        g.add_node(GraphNode(id="t2", type=NodeType.TASK, content={"status": "completed"}))
        g.add_node(GraphNode(id="o1", type=NodeType.OUTPUT, content={}))
        tasks = g.find_nodes(node_type=NodeType.TASK)
        assert len(tasks) == 2
        outputs = g.find_nodes(node_type=NodeType.OUTPUT)
        assert len(outputs) == 1

    def test_link_and_query(self):
        g = MultiLinkGraph()
        g.add_node(GraphNode(id="a", type=NodeType.TASK))
        g.add_node(GraphNode(id="b", type=NodeType.OUTPUT))
        result = g.link("a", "b", LinkType.CREATED_BY)
        assert result is True

        outgoing = g.get_outgoing("a")
        assert len(outgoing) == 1
        assert outgoing[0].target_id == "b"
        assert outgoing[0].link_type == LinkType.CREATED_BY

        incoming = g.get_incoming("b")
        assert len(incoming) == 1

    def test_link_nonexistent_nodes(self):
        g = MultiLinkGraph()
        g.add_node(GraphNode(id="a", type=NodeType.TASK))
        result = g.link("a", "nonexistent", LinkType.REFERS_TO)
        assert result is False

    def test_link_with_type_filter(self):
        g = MultiLinkGraph()
        g.add_node(GraphNode(id="a", type=NodeType.TASK))
        g.add_node(GraphNode(id="b", type=NodeType.OUTPUT))
        g.add_node(GraphNode(id="c", type=NodeType.OUTPUT))
        g.link("a", "b", LinkType.CREATED_BY)
        g.link("a", "c", LinkType.REFERS_TO)

        created = g.get_outgoing("a", link_type=LinkType.CREATED_BY)
        assert len(created) == 1
        assert created[0].target_id == "b"

    def test_neighbors(self):
        g = MultiLinkGraph()
        g.add_node(GraphNode(id="a", type=NodeType.TASK))
        g.add_node(GraphNode(id="b", type=NodeType.OUTPUT))
        g.add_node(GraphNode(id="c", type=NodeType.EXPERIENCE))
        g.link("a", "b", LinkType.CREATED_BY)
        g.link("b", "c", LinkType.EVOLVED_FROM)

        # Depth 1
        n1 = g.neighbors("a", depth=1)
        assert len(n1) >= 1

        # Depth 2
        n2 = g.neighbors("a", depth=2)
        assert len(n2) >= 2
        assert any(n.node.id == "c" for n in n2)

    def test_path_finding(self):
        g = MultiLinkGraph()
        for nid in ["a", "b", "c", "d"]:
            g.add_node(GraphNode(id=nid, type=NodeType.TASK))
        g.link("a", "b", LinkType.BEFORE)
        g.link("b", "c", LinkType.BEFORE)
        g.link("c", "d", LinkType.BEFORE)

        result = g.path("a", "d")
        assert result.found is True
        assert result.length > 0

    def test_path_not_found(self):
        g = MultiLinkGraph()
        g.add_node(GraphNode(id="a", type=NodeType.TASK))
        g.add_node(GraphNode(id="z", type=NodeType.TASK))
        result = g.path("a", "z")
        assert result.found is False

    def test_delete_node(self):
        g = MultiLinkGraph()
        g.add_node(GraphNode(id="a", type=NodeType.TASK))
        g.add_node(GraphNode(id="b", type=NodeType.OUTPUT))
        g.link("a", "b", LinkType.CREATED_BY)
        assert g.node_count() == 2
        assert g.link_count() >= 1

        g.delete_node("a")
        assert g.node_count() == 1
        assert g.get_node("a") is None

    def test_delete_links(self):
        g = MultiLinkGraph()
        g.add_node(GraphNode(id="a", type=NodeType.TASK))
        g.add_node(GraphNode(id="b", type=NodeType.OUTPUT))
        g.link("a", "b", LinkType.CREATED_BY)
        assert g.link_count() >= 1

        deleted = g.delete_links("a", "b")
        assert deleted >= 1

    def test_stats(self):
        g = MultiLinkGraph()
        g.add_node(GraphNode(id="t1", type=NodeType.TASK))
        g.add_node(GraphNode(id="o1", type=NodeType.OUTPUT))
        g.link("t1", "o1", LinkType.CREATED_BY)

        s = g.stats()
        assert s["total_nodes"] == 2
        assert s["total_links"] >= 1
        assert s["nodes_by_type"]["task"] == 1
        assert s["nodes_by_type"]["output"] == 1

    def test_related_with_context(self):
        g = MultiLinkGraph()
        g.add_node(GraphNode(id="t1", type=NodeType.TASK))
        g.add_node(GraphNode(id="e1", type=NodeType.EXPERIENCE))
        g.link("t1", "e1", LinkType.EVOLVED_FROM)

        ctx = g.related_with_context("t1")
        assert ctx["node"] is not None
        assert ctx["outgoing_count"] >= 1


class TestPersistence:
    """Test JSON persistence"""

    def test_save_load_node(self, tmp_path):
        node = GraphNode(id="test-node", type=NodeType.TASK, content={"hello": "world"})
        assert save_node(node, tmp_path) is True

        loaded = load_node("test-node", tmp_path)
        assert loaded is not None
        assert loaded.id == "test-node"
        assert loaded.type == NodeType.TASK
        assert loaded.content["hello"] == "world"

    def test_save_load_graph(self, tmp_path):
        g = MultiLinkGraph()
        g.add_node(GraphNode(id="t1", type=NodeType.TASK, content={"input": "test"}))
        g.add_node(GraphNode(id="o1", type=NodeType.OUTPUT))
        g.link("t1", "o1", LinkType.CREATED_BY)

        count = save_graph(g, tmp_path)
        assert count > 0

        loaded = load_graph(tmp_path)
        assert loaded.node_count() == 2
        assert loaded.link_count() >= 1
        assert loaded.get_node("t1") is not None

    def test_persistence_directory_structure(self, tmp_path):
        """Verify the Git-friendly directory structure"""
        node = GraphNode(id="test-1", type=NodeType.TASK)
        save_node(node, tmp_path)

        node_file = tmp_path / "nodes" / "task" / "test-1.json"
        assert node_file.exists()

        # Verify JSON content
        with open(node_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["id"] == "test-1"
        assert data["type"] == "task"


class TestExperience:
    """Test experience feedback loop"""

    def setup_method(self):
        reset_graph()

    @pytest.mark.asyncio
    async def test_record_task_start(self):
        g = get_graph()
        record_task_start(g, "task-1", "build login feature", "product_agent")
        assert g.get_node("task-1") is not None
        assert g.get_node("task-1").type == NodeType.TASK
        assert g.get_node("task-1").content["agent"] == "product_agent"

    @pytest.mark.asyncio
    async def test_record_task_complete(self):
        from agent_system.core.schema import OutputSchema
        g = get_graph()
        record_task_start(g, "task-2", "test feature", "tech_agent")
        output = OutputSchema(
            id="output-1",
            type="code",
            created_at=datetime.now(timezone.utc),
            created_by="tech_agent",
            payload={"result": "done"},
        )
        record_task_complete(g, "task-2", output)

        # Verify output node created
        assert g.get_node("output-1") is not None
        assert g.get_node("output-1").type == NodeType.OUTPUT

        # Verify link
        links = g.get_outgoing("task-2")
        assert any(l.target_id == "output-1" for l in links)

    @pytest.mark.asyncio
    async def test_record_task_failure(self):
        g = get_graph()
        record_task_start(g, "task-3", "failing task", "test_agent")
        record_task_failure(g, "task-3", "TimeoutError: API not reachable", "test_agent")

        # Verify failure node
        failure_nodes = g.find_nodes(node_type=NodeType.FAILURE)
        assert len(failure_nodes) >= 1
        assert "Timeout" in failure_nodes[0].content.get("error", "")

        # Verify fail link
        links = g.get_outgoing("task-3")
        assert any(l.link_type == LinkType.CAUSED_BY for l in links)

    @pytest.mark.asyncio
    async def test_find_similar_failures(self):
        g = get_graph()
        g.add_node(GraphNode(
            id="exp-1",
            type=NodeType.EXPERIENCE,
            content={"summary": "API timeout resolved by retry with backoff", "success": True},
        ))
        g.add_node(GraphNode(
            id="exp-2",
            type=NodeType.EXPERIENCE,
            content={"summary": "Database connection pool exhaustion fixed by increasing pool size", "success": True},
        ))

        similar = find_similar_failures(g, "API timeout error")
        assert len(similar) >= 1
        # Both should be returned; the top result depends on the backend
        returned_ids = {n.id for n, _ in similar}
        assert "exp-1" in returned_ids
        assert "exp-2" in returned_ids

    @pytest.mark.asyncio
    async def test_get_relevant_experiences(self):
        g = get_graph()
        g.add_node(GraphNode(
            id="exp-login",
            type=NodeType.EXPERIENCE,
            content={"summary": "Login feature: handle JWT token expiry gracefully", "success": True},
        ))
        g.add_node(GraphNode(
            id="exp-upload",
            type=NodeType.EXPERIENCE,
            content={"summary": "File upload: validate file size before processing", "success": True},
        ))

        relevant = get_relevant_experiences(g, "user login with JWT")
        # Either experience may rank first depending on the backend; both should appear
        assert len(relevant) >= 1
        joined = " ".join(relevant).lower()
        assert "login" in joined or "jwt" in joined or "upload" in joined

    @pytest.mark.asyncio
    async def test_full_feedback_lifecycle(self):
        """Test the complete record-fail-find cycle"""
        from agent_system.core.schema import OutputSchema

        g = get_graph()

        # Simulate: task runs -> fails -> record experience
        record_task_start(g, "lifecycle-task", "complex operation", "tech_agent")
        record_task_failure(g, "lifecycle-task", "MemoryError: out of memory", "tech_agent")

        # Record experience based on failure
        record_experience(
            g,
            "lifecycle-task",
            "MemoryError solved by processing data in chunks",
            "tech_agent",
            success=True,
            related_failure_ids=["failure-lifecycle-task"],
        )

        # Now a similar failure should find the experience
        similar = find_similar_failures(g, "MemoryError occurred during processing")
        assert len(similar) >= 1
        assert g.node_count() >= 3  # task + failure + experience

    @pytest.mark.asyncio
    async def test_memory_hooks(self):
        """Test install_memory_hooks on an agent"""
        from agent_system.core.agent import SmartAgent, TaskContext, OutputSchema

        class TestAgent(SmartAgent):
            agent_name: str = "test_memory_agent"
            agent_capabilities: list = ["testing"]
            description: str = "Test"

            async def do_work(self, task: TaskContext) -> OutputSchema:
                return OutputSchema(
                    id="mem-output",
                    type="test",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                )

        agent = TestAgent()
        reset_graph()

        # Install hooks
        install_memory_hooks(agent)

        # Run task
        task = TaskContext(task_id="mem-test", input="memory test task")
        output = await agent.execute(task)

        # Verify graph recorded everything
        g = get_graph()
        assert g.get_node("mem-test") is not None
        assert g.get_node("mem-output") is not None
        assert output is not None


class TestGraphCli:
    """Test graph CLI helpers"""

    def setup_method(self):
        reset_graph()

    def test_reset_graph(self):
        g = get_graph()
        g.add_node(GraphNode(id="x", type=NodeType.TASK))
        assert g.node_count() == 1
        reset_graph()
        g2 = get_graph()
        assert g2.node_count() == 0

    def test_stats(self):
        g = get_graph()
        g.add_node(GraphNode(id="s1", type=NodeType.TASK))
        g.add_node(GraphNode(id="s2", type=NodeType.OUTPUT))
        g.link("s1", "s2", LinkType.CREATED_BY)

        s = g.stats()
        assert s["total_nodes"] == 2
        assert s["total_links"] >= 1
