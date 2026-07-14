"""Boundary tests: memory_enabled=False skips experience feedback without memory leak.

Issue: When memory_enabled=False, verify:
- Experience recording is truly skipped
- No memory growth over time
- record_task_* functions are never called

Run: pytest tests/test_boundary_memory_disabled.py -v
"""
import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from agent_system.core.agent import SmartAgent, TaskContext, OutputSchema
from agent_system.memory.graph import get_graph, reset_graph, NodeType


class TestMemoryEnabledFalse:

    @pytest.fixture(autouse=True)
    def reset(self):
        reset_graph()
        yield
        reset_graph()

    @pytest.mark.asyncio
    async def test_memory_disabled_does_not_record_experience(self):
        """When memory_enabled=False, no EXPERIENCE or FAILURE memory nodes."""
        from pydantic import ConfigDict

        class EphemeralAgent(SmartAgent):
            agent_name: str = "ephemeral_agent"
            agent_capabilities: list = ["test"]
            description: str = "Test ephemeral agent"
            memory_enabled: bool = False
            model_config = ConfigDict(extra="allow")

            async def do_work(self, task: TaskContext) -> OutputSchema:
                return OutputSchema(
                    id=f"result-{task.task_id}",
                    type="result",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                    payload={"task": task.task_id, "extra": "data"},
                )

        agent = EphemeralAgent()
        for i in range(5):
            task = TaskContext(task_id=f"ephemeral-{i}", input=f"ephemeral task {i}")
            await agent.execute(task)

        experience_nodes = [n for n in get_graph()._nodes.values() if n.type == NodeType.EXPERIENCE]
        failure_nodes = [n for n in get_graph()._nodes.values() if n.type == NodeType.FAILURE]

        assert len(experience_nodes) == 0, f"EXPERIENCE nodes: {len(experience_nodes)}"
        assert len(failure_nodes) == 0, f"FAILURE nodes: {len(failure_nodes)}"

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
        failure_count_before = len([n for n in get_graph()._nodes.values() if n.type == NodeType.FAILURE])

        task = TaskContext(task_id="will-fail", input="this will fail")
        try:
            await agent.execute(task)
        except Exception:
            pass

        failure_count_after = len([n for n in get_graph()._nodes.values() if n.type == NodeType.FAILURE])
        assert failure_count_after == failure_count_before, "FAILURE should not increase"

    @pytest.mark.asyncio
    async def test_memory_disabled_does_not_call_memory_functions(self):
        """Verify memory functions are not called when memory is disabled."""
        from pydantic import ConfigDict

        exp_mod = "agent_system.memory.experience"

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
        with patch(f"{exp_mod}.record_task_failure") as mock_failure:
            with patch(f"{exp_mod}.record_experience") as mock_experience:
                with patch(f"{exp_mod}.record_task_complete") as mock_complete:
                    task = TaskContext(task_id="test-no-record", input="test")
                    await agent.execute(task)
                    mock_failure.assert_not_called()
                    mock_experience.assert_not_called()
                    mock_complete.assert_not_called()
