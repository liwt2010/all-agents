"""
Test: Iteration 2 — Plugin tools, Tech Agent, Test Agent, CEO pipeline, LLM Router
"""

import pytest
from datetime import datetime, timezone

from agent_system.tools.base import (
    Tool, ToolRegistry, ToolResult, discover_tools, register
)
from agent_system.tools.code_tools import CodeSearchTool, RunTestTool


class TestPluginTools:
    """Test plugin-based tool system"""

    def test_discovery(self):
        """Test auto-discovery finds all registered tools"""
        registry = discover_tools()
        names = registry.get_names()
        assert "read_file" in names
        assert "write_file" in names
        assert "list_files" in names
        assert "code_search" in names
        assert "run_test" in names

    def test_registry_definitions(self):
        registry = discover_tools()
        defs = registry.list_definitions()
        names = [d.name for d in defs]
        assert "read_file" in names
        assert "code_search" in names

    @pytest.mark.asyncio
    async def test_code_search_tool(self):
        tool = CodeSearchTool()
        result = await tool.execute({"pattern": "def ", "path": "src/agent_system/tools", "file_pattern": "*.py"})
        assert result.success is True
        assert result.output["count"] > 0
        assert any("def " in m["content"] for m in result.output["matches"])

    @pytest.mark.asyncio
    async def test_run_test_tool(self):
        tool = RunTestTool()
        result = await tool.execute({"command": "echo test_ok"})
        assert result.success is True


class TestTechAgent:
    """Test Tech Agent"""

    @pytest.mark.asyncio
    async def test_mock_code_generation(self):
        from agent_system.agents.tech_agent import TechAgent
        agent = TechAgent()
        task = TaskContext(
            task_id="test-code",
            input="Implement a hello world function",
        )
        output = await agent.execute(task)
        assert output.type == "code"
        assert output.created_by == "tech_agent"
        payload = output.payload
        assert "files" in payload or "raw_output" in payload
        assert len(output.next_steps) > 0

    @pytest.mark.asyncio
    async def test_with_upstream_prd(self):
        from agent_system.agents.tech_agent import TechAgent
        from agent_system.agents.product_agent import ProductAgent

        # First get PRD
        product = ProductAgent()
        prd_task = TaskContext(task_id="test-prd-up", input="File upload feature")
        prd_output = await product.execute(prd_task)

        # Then generate code with PRD context
        agent = TechAgent()
        task = TaskContext(
            task_id="test-code-up",
            input="Implement the code based on the PRD",
            upstream_output=prd_output.model_dump(mode="json"),
        )
        output = await agent.execute(task)
        assert output.type == "code"


class TestTestAgent:
    """Test Test Agent"""

    @pytest.mark.asyncio
    async def test_mock_test_generation(self):
        from agent_system.agents.test_agent import TestAgent
        agent = TestAgent()
        task = TaskContext(
            task_id="test-test",
            input="Generate tests for a login function",
        )
        output = await agent.execute(task)
        assert output.type == "test_report"
        assert output.created_by == "test_agent"
        payload = output.payload
        assert "test_files" in payload or "raw_output" in payload

    @pytest.mark.asyncio
    async def test_with_upstream_code(self):
        from agent_system.agents.test_agent import TestAgent

        agent = TestAgent()
        task = TaskContext(
            task_id="test-test-up",
            input="Generate tests for the code",
            upstream_output={"payload": {"files": [{"path": "main.py", "language": "python"}]}},
        )
        output = await agent.execute(task)
        assert output.type == "test_report"


class TestCEOAgent:
    """Test CEO Agent and pipeline"""

    @pytest.mark.asyncio
    async def test_ceo_pipeline_mock(self):
        """Test full pipeline in mock mode"""
        from agent_system.agents.ceo_agent import CEOAgent

        agent = CEOAgent()
        task = TaskContext(
            task_id="test-pipeline",
            input="Build a simple calculator feature",
            max_retries=1,
        )
        output = await agent.execute(task)

        assert output.type == "pipeline_result"
        payload = output.payload
        assert payload["pipeline_status"] in ("completed", "failed")
        if payload["pipeline_status"] == "completed":
            steps = payload["steps"]
            assert len(steps) == 4  # product -> tech -> test -> deploy
            assert steps[0]["agent"] == "product_agent"
            assert steps[1]["agent"] == "tech_agent"
            assert steps[2]["agent"] == "test_agent"
            assert steps[3]["agent"] == "deploy_agent"

    @pytest.mark.asyncio
    async def test_ceo_metadata(self):
        """Test pipeline metadata structure"""
        from agent_system.agents.ceo_agent import CEOAgent

        agent = CEOAgent()
        task = TaskContext(
            task_id="test-pipeline-2",
            input="User login with JWT",
            max_retries=1,
        )
        output = await agent.execute(task)

        assert output.created_by == "ceo_agent"
        assert output.schema_version == "1.0"
        assert "pipeline_status" in output.payload
        assert "steps" in output.payload
        assert "summary" in output.payload


class TestLLMRouter:
    """Test LLM Router"""

    def test_complexity_estimation(self):
        from agent_system.core.llm_router import LLMRouter
        router = LLMRouter()

        assert router.estimate_complexity("hello") == "simple"
        assert router.estimate_complexity("short") == "simple"
        assert router.estimate_complexity("Design a complete end-to-end secure authentication system") == "complex"

    def test_get_config(self):
        from agent_system.core.llm_router import LLMRouter
        router = LLMRouter()

        config = router.get_config("product_agent")
        assert config is not None
        assert "sonnet" in config.model or "haiku" in config.model

        # Simple task routing
        simple_config = router.get_config("test_agent", task_complexity="simple")
        assert simple_config is not None


class TestSmartAgentEvents:
    """Test SmartAgent event bus"""

    @pytest.mark.asyncio
    async def test_event_bus_publish(self):
        from agent_system.core.agent import event_bus, EventType, AgentEvent

        received = []
        async def handler(event):
            received.append(event)

        event_bus.subscribe(EventType.TASK_STARTED, handler)
        await event_bus.publish(AgentEvent(
            event_type=EventType.TASK_STARTED,
            agent_name="test",
            task_id="t1",
        ))
        assert len(received) == 1
        assert received[0].event_type == EventType.TASK_STARTED
        event_bus.unsubscribe(EventType.TASK_STARTED, handler)

    def test_task_context(self):
        from agent_system.core.agent import TaskContext
        ctx = TaskContext(task_id="t1", input="hello")
        assert ctx.retry_count == 0
        assert ctx.max_retries == 3


# Re-import for test utility
from agent_system.core.agent import TaskContext
