"""Agent discovery endpoint."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from agent_system.api.state import get_auth_service_singleton
from agent_system.core.auth import User, require_auth

router = APIRouter(tags=["agents"])


class AgentInfo(BaseModel):
    name: str
    description: str
    capabilities: List[str]


def _agent_registry() -> dict:
    """Lazy import of agent classes (avoid heavy imports at module load)."""
    from agent_system.agents.ceo_agent import CEOAgent
    from agent_system.agents.deploy_agent import DeployAgent
    from agent_system.agents.product_agent import ProductAgent
    from agent_system.agents.tech_agent import TechAgent
    from agent_system.agents.test_agent import TestAgent
    return {
        "product": ProductAgent,
        "tech": TechAgent,
        "test": TestAgent,
        "deploy": DeployAgent,
        "ceo": CEOAgent,
    }


@router.get("/api/agents", response_model=List[AgentInfo])
async def list_agents(
    user: User = Depends(require_auth(get_auth_service_singleton())),
) -> List[AgentInfo]:
    """List available agents with capabilities."""
    agents: List[AgentInfo] = []
    for name, cls in _agent_registry().items():
        instance = cls()
        agents.append(AgentInfo(
            name=name,
            description=instance.description,
            capabilities=instance.agent_capabilities,
        ))
    return agents
