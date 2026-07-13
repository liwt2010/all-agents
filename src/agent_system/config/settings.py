"""
配置加载模块
从 settings.yaml 加载系统配置
"""

from pathlib import Path
from typing import Any, Dict, Optional
import os
import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class LLMConfig(BaseModel):
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    temperature: float = 0.7


class AgentLLMConfig(BaseModel):
    default: LLMConfig = LLMConfig()
    fast: LLMConfig = LLMConfig(model="claude-haiku-4-5-20251001", max_tokens=2048, temperature=0.3)
    agents: dict[str, LLMConfig] = {}


class ToolConfig(BaseModel):
    enabled: list[str] = []
    config: dict[str, dict[str, Any]] = {}


class GraphConfig(BaseModel):
    max_retries: int = 3
    timeout: dict[str, int] = {
        "quick": 60,
        "standard": 300,
        "complex": 1800,
        "long": 7200,
        "batch": 86400,
    }


class MemoryConfig(BaseModel):
    graph_dir: str = "data/graph"
    node_types: list[str] = [
        "task", "output", "failure", "experience",
        "tool", "user", "prompt", "schema",
    ]
    link_types: list[str] = [
        "refers_to", "caused_by", "created_by", "part_of",
        "before", "after", "evolved_from", "supersedes",
        "validated_by", "tested_by",
    ]


class SystemConfig(BaseSettings):
    system: dict[str, str] = {
        "name": "Agent System",
        "version": "0.1.0",
        "environment": "development",
    }
    llm: AgentLLMConfig = AgentLLMConfig()
    mcp_servers: dict[str, dict[str, Any]] = {}
    tools: ToolConfig = ToolConfig()
    graph: GraphConfig = GraphConfig()
    memory: MemoryConfig = MemoryConfig()

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def from_yaml(cls, path: str | None = None) -> "SystemConfig":
        """从 YAML 文件加载配置"""
        if path is None:
            path = os.environ.get(
                "AGENT_CONFIG_PATH",
                str(Path(__file__).parent / "settings.yaml"),
            )
        config_path = Path(path)
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return cls(**data)
        return cls()

    def get_llm_config(self, agent_name: str) -> LLMConfig:
        """获取指定 Agent 的 LLM 配置"""
        return self.llm.agents.get(agent_name, self.llm.default)


# 全局单例
_settings: SystemConfig | None = None


def get_settings() -> SystemConfig:
    global _settings
    if _settings is None:
        _settings = SystemConfig.from_yaml()
    return _settings


def reload_settings() -> SystemConfig:
    global _settings
    _settings = SystemConfig.from_yaml()
    return _settings
