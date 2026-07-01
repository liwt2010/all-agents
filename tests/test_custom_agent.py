"""
Tests: Custom Agent platform
"""

import json
import pytest
from datetime import datetime, timezone

from agent_system.core.agent import TaskContext
from agent_system.agents.custom import (
    CustomAgent,
    CustomAgentConfig,
    CustomAgentSafety,
    CustomAgentRegistry,
    get_custom_agent_registry,
)


class TestCustomAgentConfig:
    def test_basic_config(self):
        config = CustomAgentConfig(
            id="code-reviewer",
            name="Code Reviewer",
            description="Reviews code for style",
            system_prompt="You are a code reviewer...",
            tools=["read_file", "code_search"],
        )
        assert config.id == "code-reviewer"
        assert config.safety == CustomAgentSafety.NORMAL  # default
        assert config.llm_config == {}  # default

    def test_safety_levels(self):
        assert CustomAgentSafety.STRICT.value == "strict"
        assert CustomAgentSafety.NORMAL.value == "normal"
        assert CustomAgentSafety.AUTONOMOUS.value == "autonomous"

    def test_extra_fields_allowed(self):
        """Configs support user-defined custom fields."""
        config = CustomAgentConfig(
            id="x", name="X", description="d", system_prompt="p",
            custom_field_1="hello",
        )
        assert config.custom_field_1 == "hello"


class TestCustomAgent:
    @pytest.mark.asyncio
    async def test_basic_run(self):
        config = CustomAgentConfig(
            id="my-agent",
            name="My Agent",
            description="d",
            system_prompt="You do X",
        )
        agent = CustomAgent(config)
        task = TaskContext(task_id="custom-1", input="Do X")
        output = await agent.execute(task)
        assert output.type == "custom_result"
        assert output.metadata["custom_agent_id"] == "my-agent"

    @pytest.mark.asyncio
    async def test_safety_in_system_prompt(self):
        for safety in [CustomAgentSafety.STRICT, CustomAgentSafety.NORMAL, CustomAgentSafety.AUTONOMOUS]:
            config = CustomAgentConfig(
                id=f"agent-{safety.value}",
                name="X", description="d", system_prompt="base prompt",
                safety=safety,
                tools=["read_file"],
            )
            agent = CustomAgent(config)
            prompt = agent.get_system_prompt()
            assert safety.value.upper() in prompt.upper()
            assert "read_file" in prompt

    def test_tool_filtering(self):
        from agent_system.config.settings import get_settings
        global_enabled = get_settings().tools.enabled

        if "read_file" in global_enabled:
            config = CustomAgentConfig(
                id="only-read", name="X", description="d",
                system_prompt="p",
                tools=["read_file"],
            )
            agent = CustomAgent(config)
            available = [t.name for t in agent.tool_registry.list_definitions()]
            assert "read_file" in available
        else:
            pytest.skip("read_file not enabled")

    def test_extra_tools_filtered_out(self):
        config = CustomAgentConfig(
            id="strict-agent", name="X", description="d",
            system_prompt="p",
            tools=["read_file"],
        )
        agent = CustomAgent(config)
        available = [t.name for t in agent.tool_registry.list_definitions()]
        # Either read_file is there (if globally enabled), or registry is empty
        # because the intersection filtered everything out
        assert "read_file" in available or len(available) == 0


class TestCustomAgentRegistry:
    def setup_method(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.registry = CustomAgentRegistry(storage_path=self.tmp)

    def test_register_and_get(self):
        config = CustomAgentConfig(
            id="a1", name="A1", description="d", system_prompt="p",
            tenant_id="acme",
        )
        self.registry.register(config)
        loaded = self.registry.get("a1", tenant_id="acme")
        assert loaded.id == "a1"
        assert loaded.tenant_id == "acme"

    def test_tenant_isolation(self):
        a = CustomAgentConfig(id="a", name="A", description="d", system_prompt="p", tenant_id="acme")
        b = CustomAgentConfig(id="a", name="A", description="d", system_prompt="p", tenant_id="beta")
        self.registry.register(a)
        self.registry.register(b)
        assert self.registry.get("a", "acme").tenant_id == "acme"
        assert self.registry.get("a", "beta").tenant_id == "beta"

    def test_list_for_tenant(self):
        self.registry.register(CustomAgentConfig(id="x1", name="X", description="d", system_prompt="p", tenant_id="acme"))
        self.registry.register(CustomAgentConfig(id="x2", name="X", description="d", system_prompt="p", tenant_id="acme"))
        self.registry.register(CustomAgentConfig(id="x3", name="X", description="d", system_prompt="p", tenant_id="beta"))
        acme_list = self.registry.list(tenant_id="acme")
        assert all(c.tenant_id == "acme" for c in acme_list)
        assert len(acme_list) == 2

    def test_delete(self):
        self.registry.register(CustomAgentConfig(
            id="del", name="X", description="d", system_prompt="p", tenant_id="t1",
        ))
        assert self.registry.get("del", "t1") is not None
        deleted = self.registry.delete("del", "t1")
        assert deleted is True
        assert self.registry.get("del", "t1") is None

    def test_persistence(self):
        path = self.tmp
        self.registry.register(CustomAgentConfig(
            id="persist", name="X", description="d", system_prompt="p", tenant_id="t",
        ))

        new_registry = CustomAgentRegistry(storage_path=path)
        loaded = new_registry.get("persist", "t")
        assert loaded is not None
        assert loaded.id == "persist"

    def test_instantiate_creates_runtime(self):
        self.registry.register(CustomAgentConfig(
            id="runtime", name="X", description="d", system_prompt="p", tenant_id="t",
        ))
        agent = self.registry.instantiate("runtime", "t")
        assert agent is not None
        assert agent.agent_spec.id == "runtime"
        assert agent.agent_name == "custom_runtime"

    def test_instantiate_unknown_returns_none(self):
        assert self.registry.instantiate("nonexistent", "t") is None

    def test_singleton(self):
        r1 = get_custom_agent_registry()
        r2 = get_custom_agent_registry()
        assert r1 is r2
