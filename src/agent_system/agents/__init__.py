"""
Agent package — exposes the 9 built-in agents.

Importing this package triggers @register_agent decorators on each agent class
via `discover_agents()`. After import, `agent_registry` contains all 9 agents.

Add a new agent by dropping `agents/<name>_agent.py` with an `@register_agent`-decorated
class — no changes needed here.
"""

from agent_system.core.registry import (
    AgentRegistry,
    agent_registry,
    discover_agents,
    register_agent,
)

# Auto-discover and register all agents in this directory on import.
discover_agents()

# Re-export concrete agent classes for convenience.
from agent_system.agents.product_agent import ProductAgent
from agent_system.agents.tech_agent import TechAgent
from agent_system.agents.test_agent import TestAgent
from agent_system.agents.ceo_agent import CEOAgent
from agent_system.agents.deploy_agent import DeployAgent
from agent_system.agents.devops_agent import DevOpsAgent
from agent_system.agents.docs_agent import DocsAgent
from agent_system.agents.review_agent import ReviewAgent
from agent_system.agents.security_agent import SecurityAgent

__all__ = [
    "AgentRegistry",
    "agent_registry",
    "discover_agents",
    "register_agent",
    "ProductAgent",
    "TechAgent",
    "TestAgent",
    "CEOAgent",
    "DeployAgent",
    "DevOpsAgent",
    "DocsAgent",
    "ReviewAgent",
    "SecurityAgent",
]