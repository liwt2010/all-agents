"""
Custom Agent platform — base classes (PR 8).

CustomAgentConfig: Pydantic schema for user-defined agents.
CustomAgentSafety: enum of safety levels.
CustomAgent: SmartAgent subclass driven by a CustomAgentConfig.

Design: docs/CUSTOM_AGENT.md
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from agent_system.config.settings import get_settings
from agent_system.core.agent import SmartAgent, TaskContext
from agent_system.core.registry import register_agent
from agent_system.core.schema import OutputSchema
from agent_system.tools.base import ToolRegistry, discover_tools, filter_registry


class CustomAgentSafety(str, Enum):
    """How much autonomy does the agent have?"""
    STRICT = "strict"           # Input validated strictly; tool calls need human approval
    NORMAL = "normal"           # Default: same as built-in agents
    AUTONOMOUS = "autonomous"   # No human review; full self-direction


class CustomAgentConfig(BaseModel):
    """Schema for a user-defined custom agent.

    Extra fields are allowed so users can add custom metadata (e.g. team,
    ticket-link, owner) without forking the schema.
    """
    id: str
    name: str
    description: str
    system_prompt: str
    tools: List[str] = Field(default_factory=list)
    safety: CustomAgentSafety = CustomAgentSafety.NORMAL
    llm_config: Dict[str, Any] = Field(default_factory=dict)
    tenant_id: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class CustomAgent(SmartAgent):
    """A SmartAgent whose behavior is driven by a CustomAgentConfig.

    Inherits all SmartAgent infrastructure (retry, checkpoint, event bus,
    4-way escalation resolver) but supplies system_prompt, capabilities,
    and tool subset from the config.
    """

    def __init__(self, config: CustomAgentConfig):
        # Filter tools against global enabled set — intersection only.
        settings = get_settings()
        enabled_tools = list(settings.tools.enabled or [])
        requested_tools = list(config.tools or [])
        if requested_tools:
            actual_tools = [t for t in requested_tools if t in enabled_tools]
        else:
            actual_tools = enabled_tools

        base_registry = discover_tools()
        filtered = filter_registry(base_registry, actual_tools) if actual_tools else ToolRegistry()

        # Let Pydantic initialize all fields first (runs default_factory for llm_router etc.)
        super().__init__()

        # Override dynamic fields via object.__setattr__ to bypass Pydantic v2's
        # setattr validation (which forbids setting undeclared fields).
        # llm_router is NOT overridden — let Pydantic's default_factory keep its instance.
        object.__setattr__(self, "agent_spec", config)
        object.__setattr__(self, "agent_name", f"custom_{config.id}")
        object.__setattr__(self, "agent_capabilities", [config.description])
        object.__setattr__(self, "description", config.description)
        object.__setattr__(self, "tool_registry", filtered)

    @property
    def config(self) -> CustomAgentConfig:
        return self.agent_spec

    def get_system_prompt(self) -> str:
        """Build a system prompt that includes the user's base prompt plus safety annotation and tool list."""
        tools_desc = "\n".join(
            f"  - {t.name}: {t.description}"
            for t in self.tool_registry.list_definitions()
        )
        safety_line = f"Safety level: {self.agent_spec.safety.value.upper()}"
        return f"""You are {self.agent_name}, a custom agent.

{self.agent_spec.description}

{safety_line}

Base instructions:
{self.agent_spec.system_prompt}

Available tools:
{tools_desc if tools_desc else "  (none — operate using text only)"}

Requirements:
1. Operate within your safety level constraints
2. Use only the tools listed above
3. Output must conform to the standard schema (id, type, created_at, schema_version, payload, next_steps)

Begin."""

    async def do_work(self, task: TaskContext) -> OutputSchema:
        """Execute the custom agent's task via the LLM Router.

        For now we use mock-mode (deterministic output) unless a real LLM is
        configured. The shape matches what a real LLM would return so the
        system-level pipeline (event bus, checkpoint, metrics) works end-to-end.
        """
        from datetime import datetime, timezone
        from agent_system.core.schema import NextStep

        router = self.llm_router
        config_obj = router.get_config(self.agent_name)
        system_prompt = self.get_system_prompt()

        # Try real LLM; fall back to mock if not configured
        try:
            if hasattr(router, "is_mock_mode") and not router.is_mock_mode:
                text, _usage = await router.call_llm(
                    config_obj, system_prompt,
                    [{"role": "user", "content": task.input}],
                )
                import json as _json
                try:
                    payload = _json.loads(text)
                except Exception:
                    payload = {"raw": text}
            else:
                raise RuntimeError("mock mode")
        except Exception:
            # Mock response: deterministic per agent
            payload = {
                "agent_id": self.agent_spec.id,
                "agent_name": self.agent_name,
                "result": f"[mock:{self.agent_spec.id}] processed: {task.input[:100]}",
                "safety_level": self.agent_spec.safety.value,
                "tools_available": [t.name for t in self.tool_registry.list_definitions()],
            }

        return OutputSchema(
            id=f"custom-{self.agent_spec.id}-{task.task_id}",
            type="custom_result",
            created_at=datetime.now(timezone.utc),
            created_by=self.agent_name,
            payload=payload,
            metadata={
                "custom_agent_id": self.agent_spec.id,
                "safety_level": self.agent_spec.safety.value,
                "tools_count": len(self.tool_registry.list_definitions()),
            },
            next_steps=[],
        )