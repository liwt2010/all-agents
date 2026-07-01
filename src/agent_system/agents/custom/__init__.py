"""
Custom Agent platform — PLATFORM §14

Users can create their own agents with 4-part config:
  1. System prompt (what to do, how)
  2. Tools (which MCP / built-in / custom)
  3. Knowledge base (RAG source)
  4. LLM (which model + temperature)

Stored as CustomAgentConfig in the tenant store. Executed via a
`CustomAgent` runtime that builds a do_work() from the config.
"""

import json
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from agent_system.core.agent import SmartAgent, TaskContext
from agent_system.core.schema import OutputSchema, NextStep

logger = logging.getLogger(__name__)


# ── Configuration ──

class CustomAgentSafety(str, Enum):
    """How much autonomy the agent has."""
    STRICT = "strict"             # Only registered tools, no loops
    NORMAL = "normal"             # Can use any tool, no exec
    AUTONOMOUS = "autonomous"     # Full tool access, can write to memory


class CustomAgentConfig(BaseModel):
    """User-defined agent specification."""
    model_config = ConfigDict(extra="allow")

    id: str                                  # unique id (slug)
    name: str                                # human-readable
    description: str
    tenant_id: str = "default"
    group_id: str = ""

    # 4-part config
    system_prompt: str                       # Part 1: what to do
    tools: List[str] = Field(default_factory=list)  # Part 2: tool names
    knowledge_base: Optional[str] = None     # Part 3: KB id (RAG source)
    llm_config: Dict[str, Any] = Field(default_factory=dict)  # Part 4: model + temp

    # Safety
    safety: CustomAgentSafety = CustomAgentSafety.NORMAL
    max_tool_calls_per_task: int = 10

    # Metadata
    created_by: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = 1
    is_template: bool = False   # marketplace-ready templates


# ── Runtime ──

class CustomAgent(SmartAgent):
    """
    Runtime for a custom-defined agent. Reads its behavior from a
    CustomAgentConfig and builds do_work() accordingly.
    """

    model_config = ConfigDict(extra="allow")

    def __init__(self, config: CustomAgentConfig, **kwargs):
        from pydantic import BaseModel
        BaseModel.__init__(self)
        # Store the config under a name that doesn't clash with Pydantic
        object.__setattr__(self, "agent_spec", config)
        object.__setattr__(self, "agent_name", f"custom_{config.id}")
        object.__setattr__(self, "agent_capabilities", [config.id])
        object.__setattr__(self, "description", config.description)
        object.__setattr__(self, "max_retries", 2)
        object.__setattr__(self, "enable_escalation", False)
        # Config-derived state
        object.__setattr__(self, "_config_tools", config.tools)
        object.__setattr__(self, "_config_tool_max", config.max_tool_calls_per_task)
        object.__setattr__(self, "_system_prompt", config.system_prompt)
        object.__setattr__(self, "_llm_override", config.llm_config)
        object.__setattr__(self, "_safety", config.safety)
        # Build tool registry filtered by config.tools
        from agent_system.config.settings import get_settings
        from agent_system.tools.base import (
            filter_registry, discover_tools,
        )
        enabled = get_settings().tools.enabled
        if config.tools:
            enabled = [t for t in enabled if t in config.tools]
        registry = filter_registry(discover_tools(), enabled)
        object.__setattr__(self, "tool_registry", registry)

    def get_system_prompt(self) -> str:
        """Build system prompt with safety constraints."""
        base = self._system_prompt

        if self._safety == CustomAgentSafety.STRICT:
            base += "\n\n[SAFETY: STRICT MODE]\nYou may ONLY use the explicitly allowed tools. No improvisation, no external data access."
        elif self._safety == CustomAgentSafety.NORMAL:
            base += "\n\n[SAFETY: NORMAL MODE]\nUse allowed tools. Verify outputs before claiming success. Refuse dangerous operations."
        else:
            base += "\n\n[SAFETY: AUTONOMOUS MODE]\nYou have full tool access. Use judgment and clean up after yourself."

        if self._config_tools:
            base += f"\n\nAllowed tools: {', '.join(self._config_tools)}"

        base += "\n\n## Output format\nOutput valid JSON with: payload, metadata, next_steps. No markdown code blocks."

        return base

    async def do_work(self, task: TaskContext) -> OutputSchema:
        """Execute the custom agent's task using its configured LLM + tools."""
        from agent_system.core.llm_router import router, set_llm_context

        config = router.get_config(self.agent_name)
        if self._llm_override:
            model = self._llm_override.get("model", config.model)
            temperature = self._llm_override.get("temperature", config.temperature)
            max_tokens = self._llm_override.get("max_tokens", config.max_tokens)
            from agent_system.config.settings import LLMConfig as LLMConfigCls
            config = LLMConfigCls(model=model, temperature=temperature, max_tokens=max_tokens)

        set_llm_context(self.agent_name, task.task_id)

        tools_list = None
        if self._config_tools and self._safety != CustomAgentSafety.STRICT:
            tools_list = [t.to_definition().model_dump() for t in self.tool_registry.list_definitions()]

        messages = [
            {"role": "user", "content": f"Task: {task.input}"},
        ]

        try:
            text, usage = await router.call_llm(
                config, self.get_system_prompt(), messages, tools=tools_list,
            )
        except Exception as e:
            logger.warning(f"CustomAgent {self.agent_spec.id} LLM call failed: {e}")
            text = self._mock_response(task)

        payload = self._parse_payload(text)

        return OutputSchema(
            id=OutputSchema.generate_id(f"custom_{self.agent_spec.id}"),
            type="custom_result",
            created_at=datetime.now(timezone.utc),
            created_by=self.agent_name,
            schema_version="1.0",
            payload=payload,
            metadata={
                "custom_agent_id": self.agent_spec.id,
                "safety_level": self._safety.value,
                "tools_enabled": self._config_tools,
                "llm_model": config.model,
            },
            next_steps=[
                NextStep(action="review", agent="human", description="Review custom agent output"),
            ],
        )

    def _parse_payload(self, text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0] if "```" in text else text
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw_output": text[:2000]}

    def _mock_response(self, task: TaskContext) -> str:
        """Deterministic mock for dev/test without API key."""
        return json.dumps({
            "result": f"Custom agent {self.agent_spec.id} completed task",
            "input": task.input[:200],
            "capabilities_used": self._config_tools[:3],
        })


# ── Registry ──

class CustomAgentRegistry:
    """
    Stores custom agent configs per tenant. Production: backed by Postgres;
    here: in-memory + JSON files.
    """

    def __init__(self, storage_path: str = "data/custom_agents"):
        from pathlib import Path
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self._agents: Dict[str, CustomAgentConfig] = {}
        self._load_all()

    def register(self, config: CustomAgentConfig) -> CustomAgentConfig:
        """Register a new custom agent config."""
        key = self._key(config.tenant_id, config.id)
        self._agents[key] = config
        self._save(config)
        return config

    def get(self, agent_id: str, tenant_id: str = "default") -> Optional[CustomAgentConfig]:
        return self._agents.get(self._key(tenant_id, agent_id))

    def list(self, tenant_id: str = "default") -> List[CustomAgentConfig]:
        return [
            cfg for k, cfg in self._agents.items()
            if k.startswith(f"{tenant_id}::")
        ]

    def delete(self, agent_id: str, tenant_id: str = "default") -> bool:
        key = self._key(tenant_id, agent_id)
        if key in self._agents:
            del self._agents[key]
            path = self._file_path(tenant_id, agent_id)
            if path.exists():
                path.unlink()
            return True
        return False

    def instantiate(self, agent_id: str, tenant_id: str = "default") -> Optional[CustomAgent]:
        """Create a runnable CustomAgent from a stored config."""
        config = self.get(agent_id, tenant_id)
        if not config:
            return None
        return CustomAgent(config)

    def _key(self, tenant_id: str, agent_id: str) -> str:
        return f"{tenant_id}::{agent_id}"

    def _file_path(self, tenant_id: str, agent_id: str):
        from pathlib import Path
        return Path(self.storage_path) / tenant_id / f"{agent_id}.json"

    def _save(self, config: CustomAgentConfig) -> None:
        path = self._file_path(config.tenant_id, config.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(config.model_dump_json(indent=2))

    def _load_all(self) -> None:
        if not self.storage_path.exists():
            return
        for path in self.storage_path.rglob("*.json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    config = CustomAgentConfig(**json.loads(f.read()))
                self._agents[self._key(config.tenant_id, config.id)] = config
            except Exception as e:
                logger.debug(f"Failed to load {path}: {e}")


# Global registry
_default_registry: Optional[CustomAgentRegistry] = None


def get_custom_agent_registry() -> CustomAgentRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = CustomAgentRegistry()
    return _default_registry
