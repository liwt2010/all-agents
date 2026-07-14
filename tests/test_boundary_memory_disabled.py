"""Boundary tests: memory_enabled=False skips experience feedback without memory leak.

Issue: When memory_enabled=False, verify:
- Experience recording is truly skipped
- No graph nodes are created
- No memory growth over time

Run: pytest tests/test_boundary_memory_disabled.py -v
"""
import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from agent_system.core.agent import SmartAgent, TaskContext, OutputSchema
from agent_system.memory.graph import get_graph, reset_graph, GraphNode, NodeType


class TestMemoryEnabledFalse:
    """Verify memory_enabled=False truly disables all memory operations."""

    @pytest.fixture(autouse=True)
    def reset(self):
        """Reset graph before each test."""
        reset_graph()
        yield
        reset_graph()

    @pytest.mark.asyncio
    async def test_memory_disabled_skips_experience_recording(self):
        """When memory_enabled=False, no experience nodes should be created."""
        from pydantic import ConfigDict

        node_count_before = len(get_graph()._nodes)

        class EphemeralAgent(SmartAgent):
            agent_name: str = "ephemeral_agent"
            agent_capabilities: list = ["test"]
            description: str = "Test ephemeral agent"
            memory_enabled: bool = False  # Disable memory!
            model_config = ConfigDict(extra="allow")

            async def do_work(self, task: TaskContext) -> OutputSchema:
                return OutputSchema(
                    id=f"result-{task.task_id}",
                    type="result",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                    payload={"task": task.task_id},
                )

        agent = EphemeralAgent()

        # Execute multiple tasks
        for i in range(5):
            task = TaskContext(
                task_id=f"ephemeral-{i}",
                input=f"ephemeral task {i}",
            )
            result = await agent.execute(task)
            assert result is not None

        # Verify: No new nodes should be created in the graph
        node_count_after = len(get_graph()._nodes)
        new_nodes = node_count_after - node_count_before

        # Only TASK nodes might be created (for task tracking), but no EXPERIENCE nodes
        experience_nodes = [
            n for n in get_graph()._nodes.values()
            if n.type == NodeType.EXPERIENCE
        ]
        failure_nodes = [
            n for n in get_graph()._nodes.values()
            if n.type == NodeType.FAILURE
        ]

        assert len(experience_nodes) == 0, "EXPERIENCE nodes should not be created when memory_enabled=False"
        assert len(failure_nodes) == 0, "FAILURE nodes should not be created when memory_enabled=False"
        assert new_nodes <= 5, f"Too many nodes created: {new_nodes} (expected <= 5 for task tracking)"

    @pytest.mark.asyncio
    async def test_memory_disabled_with_failures(self):
        """Failures should not be recorded when memory_enabled=False."""
        from pydantic import ConfigDict

        class AlwaysFailingAgent(SmartAgent):
            agent_name: str = "failing_agent"
            agent_capabilities: list = ["test"]
            description: str = "Test"
            memory_enabled: bool = False
            model_config = ConfigDict(extra="allow")

            async def do_work(self, task: TaskContext) -> OutputSchema:
                raise RuntimeError("Simulated failure")

        agent = AlwaysFailingAgent()
        resolver = agent._resolver if hasattr(agent, "_resolver") else None

        failure_count_before = len([
            n for n in get_graph()._nodes.values()
            if n.type == NodeType.FAILURE
        ])

        # Execute and expect failure
        task = TaskContext(task_id="will-fail", input="this will fail")
        try:
            await agent.execute(task)
        except Exception:
            pass  # Expected

        failure_count_after = len([
            n for n in get_graph()._nodes.values()
            if n.type == NodeType.FAILURE
        ])

        # No additional failure nodes should be created
        assert failure_count_after == failure_count_before, \
            "FAILURE nodes should not be created when memory_enabled=False"

    @pytest.mark.asyncio
    async def test_memory_disabled_does_not_call_record_task_failure(self):
        """Verify record_task_failure is not called."""
        from pydantic import ConfigDict

        class TestAgent(SmartAgent):
            agent_name: str = "test_agent"
            agent_capabilities: list = ["test"]
            description: str = "Test"
            memory_enabled: bool = False
            model_config = ConfigDict(extra="allow")

            async def do_work(self, task: TaskContext) -> OutputSchema:
                return OutputSchema(
                    id=f"result-{task.task_id}",
                    type="result",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                )

        agent = TestAgent()

        # Patch the memory recording function
        with patch("agent_system.core.agent.record_task_failure") as mock_record:
            with patch("agent_system.core.agent.record_experience") as mock_experience:
                with patch("agent_system.core.agent.record_task_success") as mock_success:
                    task = TaskContext(task_id="test-no-record", input="test")
                    await agent.execute(task)

                    # Verify: No memory recording functions should be called
                    mock_record.assert_not_called()
                    mock_experience.assert_not_called()
                    mock_success.assert_not_called()


class TestMemoryEnabledTrue:
    """Baseline: Verify memory_enabled=True still works correctly."""

    @pytest.fixture(autouse=True)
    def reset(self):
        reset_graph()
        yield
        reset_graph()

    @pytest.mark.asyncio
    async def test_memory_enabled_records_experience(self):
        """When memory_enabled=True, experience should be recorded."""
        from pydantic import ConfigDict

        class PersistentAgent(SmartAgent):
            agent_name: str = "persistent_agent"
            agent_capabilities: list = ["test"]
            description: str = "Test"
            memory_enabled: bool = True  # Enable memory
            model_config = ConfigDict(extra="allow")

            async def do_work(self, task: TaskContext) -> OutputSchema:
                return OutputSchema(
                    id=f"result-{task.task_id}",
                    type="result",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                )

        agent = PersistentAgent()

        task = TaskContext(task_id="test-memory", input="test with memory")
        await agent.execute(task)

        # With memory enabled, TASK nodes should be created
        task_nodes = [
            n for n in get_graph()._nodes.values()
            if n.type == NodeType.TASK
        ]
        # At minimum, we should have task tracking nodes
        assert len(get_graph()._nodes) >= 1, "Nodes should be created when memory_enabled=True"
