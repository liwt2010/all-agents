"""
Test: Schema, Tools, Product Agent
"""

import pytest
from datetime import datetime, timezone

from agent_system.core.schema import OutputSchema, SchemaValidator, NextStep
from agent_system.tools.registry import (
    ReadFileTool,
    WriteFileTool,
    ListFilesTool,
    ToolRegistry,
    create_default_registry,
)
from agent_system.core.agent import SmartAgent, TaskContext, EventType, event_bus, AgentEvent


class TestOutputSchema:
    """Test output schema validation"""

    def test_valid_output(self):
        output = OutputSchema(
            id="test-001",
            type="requirement",
            created_at=datetime.now(timezone.utc),
            created_by="test_agent",
            payload={"title": "Test PRD"},
        )
        assert output.id == "test-001"
        assert output.type == "requirement"
        assert output.schema_version == "1.0"

    def test_schema_validation(self):
        v = SchemaValidator()
        output = OutputSchema(
            id="test-002",
            type="requirement",
            created_at=datetime.now(timezone.utc),
            created_by="test_agent",
            payload={},
        )
        result = v.validate(output)
        assert result.valid is True

    def test_id_generation(self):
        tid = OutputSchema.generate_id("prd")
        assert tid.startswith("prd-")
        assert len(tid) > 10

    def test_next_steps(self):
        output = OutputSchema(
            id="test-003",
            type="requirement",
            created_at=datetime.now(timezone.utc),
            created_by="test_agent",
            next_steps=[NextStep(action="tech_estimate", agent="tech_agent")],
        )
        assert len(output.next_steps) == 1
        assert output.next_steps[0].action == "tech_estimate"


class TestToolRegistry:
    """Test tool registry"""

    @pytest.mark.asyncio
    async def test_read_file(self):
        tool = ReadFileTool()
        result = await tool.execute({"path": "pyproject.toml"})
        assert result.success is True
        assert "[project]" in result.output

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self):
        tool = ReadFileTool()
        result = await tool.execute({"path": "nonexistent.txt"})
        assert result.success is False
        assert "不存在" in result.error or "exist" in result.error

    @pytest.mark.asyncio
    async def test_write_file(self, tmp_path, monkeypatch):
        # Sandbox now restricts to configured roots (data/tmp/cwd).
        # Use a path inside the configured roots.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        tool = WriteFileTool()
        f = tmp_path / "data" / "test.txt"
        result = await tool.execute({"path": str(f), "content": "hello"})
        assert result.success is True
        assert f.read_text() == "hello"

    @pytest.mark.asyncio
    async def test_list_files(self):
        tool = ListFilesTool()
        result = await tool.execute({"path": "."})
        assert result.success is True
        assert "pyproject.toml" in result.output["files"]

    @pytest.mark.asyncio
    async def test_registry(self):
        registry = create_default_registry()
        names = registry.get_names()
        assert "read_file" in names
        assert "write_file" in names
        assert "list_files" in names

        tools = registry.list_definitions()
        assert len(tools) >= 3

        result = await registry.execute("read_file", {"path": "pyproject.toml"})
        assert result.success is True


class TestSmartAgent:
    """Test SmartAgent base class"""

    @pytest.mark.asyncio
    async def test_execute_success(self):
        """Test basic execute flow"""

        class TestAgent(SmartAgent):
            agent_name: str = "test_agent"
            agent_capabilities: list = ["testing"]
            description: str = "Test Agent"

            async def do_work(self, task: TaskContext) -> OutputSchema:
                return OutputSchema(
                    id="test-output",
                    type="test",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                    payload={"result": task.input},
                )

        agent = TestAgent()
        task = TaskContext(task_id="test-1", input="hello world")
        output = await agent.execute(task)

        assert output.id == "test-output"
        assert output.payload["result"] == "hello world"
        assert output.created_by == "test_agent"

    @pytest.mark.asyncio
    async def test_execute_retry_on_failure(self):
        """Test retry mechanism"""

        call_count = 0

        class FlakyAgent(SmartAgent):
            agent_name: str = "flaky_agent"
            agent_capabilities: list = ["testing"]
            description: str = "Flaky Agent"

            async def do_work(self, task: TaskContext) -> OutputSchema:
                nonlocal call_count
                call_count += 1
                if call_count < 2:
                    raise TimeoutError("API timeout")
                return OutputSchema(
                    id="test-output",
                    type="test",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                    payload={"result": "success"},
                )

        agent = FlakyAgent(max_retries=3)
        task = TaskContext(task_id="test-2", input="test")
        output = await agent.execute(task)

        assert output.payload["result"] == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_event_publishing(self):
        """Test event publishing"""

        events = []

        async def collector(event: AgentEvent):
            events.append(event)

        event_bus.subscribe(EventType.TASK_STARTED, collector)
        event_bus.subscribe(EventType.TASK_COMPLETED, collector)

        class TestAgent(SmartAgent):
            agent_name: str = "event_agent"
            agent_capabilities: list = ["testing"]
            description: str = "Event Agent"

            async def do_work(self, task: TaskContext) -> OutputSchema:
                return OutputSchema(
                    id="test-event",
                    type="test",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                )

        agent = TestAgent()
        task = TaskContext(task_id="test-3", input="test event")
        await agent.execute(task)

        assert len(events) == 2
        assert events[0].event_type == EventType.TASK_STARTED
        assert events[1].event_type == EventType.TASK_COMPLETED

        event_bus.unsubscribe(EventType.TASK_STARTED, collector)
        event_bus.unsubscribe(EventType.TASK_COMPLETED, collector)


class TestProductAgent:
    """Test Product Agent"""

    @pytest.mark.asyncio
    async def test_mock_prd(self):
        from agent_system.agents.product_agent import ProductAgent

        agent = ProductAgent()
        task = TaskContext(task_id="test-prd", input="用户登录功能")

        output = await agent.execute(task)

        assert output.type == "requirement"
        assert output.created_by == "product_agent"
        assert "features" in output.payload
        assert len(output.payload["features"]) > 0
        assert len(output.next_steps) > 0

    @pytest.mark.asyncio
    async def test_mock_prd_structure(self):
        from agent_system.agents.product_agent import ProductAgent

        agent = ProductAgent()
        task = TaskContext(task_id="test-prd-2", input="文件上传功能")

        output = await agent.execute(task)

        for feature in output.payload.get("features", []):
            assert "name" in feature
            assert "description" in feature
            assert "priority" in feature
            assert "acceptance_criteria" in feature


class TestGraph:
    """Test LangGraph orchestration"""

    def test_graph_creation(self):
        from agent_system.agents.product_agent import ProductAgent
        from agent_system.core.graph import create_graph, run_agent_sync

        agent = ProductAgent()
        graph = create_graph(agent)
        assert graph is not None

    def test_run_agent_sync(self):
        from agent_system.core.graph import run_agent_sync
        from agent_system.agents.product_agent import ProductAgent

        agent = ProductAgent()
        result = run_agent_sync(agent, "写一个搜索功能")

        assert result["status"] in ("completed", "failed")
        if result["status"] == "completed":
            assert "output" in result
            assert result["output"]["type"] == "requirement"
