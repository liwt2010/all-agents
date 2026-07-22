"""Custom Agent API endpoints (PR v0.3.0).

Exposes the CustomAgentRegistry over HTTP so tenants can list,
invoke, and (admin) upload custom agent configs without touching
the filesystem.

Routes:
  GET    /api/custom-agents                 List configs for the caller's tenant
  GET    /api/custom-agents/{agent_id}      Show one config
  POST   /api/custom-agents/{agent_id}/run  Invoke via LLM Router (mock or real)
  POST   /api/custom-agents:upload         (admin) register a YAML from a string
  DELETE /api/custom-agents/{agent_id}      (admin) remove

Auth:
  All routes require a Bearer JWT (multi-tenant aware). The
  `tenant_id` claim scopes both listing and invocation: a tenant
  cannot see or run another tenant's agents.

  The upload + delete routes additionally require the caller to
  hold the `admin` global role (see User.global_role).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from agent_system.agents.custom import (
    CustomAgentConfig,
    CustomAgentLoadError,
    CustomAgentRegistry,
    get_custom_agent_registry,
)
from agent_system.api.state import get_auth_service_singleton
from agent_system.core.auth import User, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["custom_agents"])


class CustomAgentInfo(BaseModel):
    """Public view of a CustomAgentConfig — hides system_prompt until
    the caller has invoked the agent (operators don't need to see it
    in lists, but they do when editing)."""
    id: str
    name: str
    description: str
    capabilities: list[str]
    tools: list[str]
    safety: str
    tenant_id: str | None = None

    @classmethod
    def from_config(cls, cfg: CustomAgentConfig) -> "CustomAgentInfo":
        return cls(
            id=cfg.id,
            name=cfg.name,
            description=cfg.description,
            capabilities=[cfg.description],
            tools=list(cfg.tools or []),
            safety=cfg.safety.value,
            tenant_id=cfg.tenant_id,
        )


class CustomAgentDetail(CustomAgentInfo):
    system_prompt: str
    llm_config: dict[str, Any] = Field(default_factory=dict)


class RunRequest(BaseModel):
    input: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunResponse(BaseModel):
    agent_id: str
    status: str
    output: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class UploadRequest(BaseModel):
    """Submit a custom agent as an inline YAML string."""
    yaml: str = Field(..., description="YAML source for the agent config")


# ── Helpers ──

def _registry() -> CustomAgentRegistry:
    return get_custom_agent_registry()


def _require_admin(user: User) -> None:
    if getattr(user, "global_role", None) not in ("tenant_admin", "platform_admin", "super_admin"):
        raise HTTPException(
            status_code=403,
            detail="admin role required for this operation",
        )


# ── Routes ──

@router.get("/api/custom-agents", response_model=list[CustomAgentInfo])
async def list_custom_agents(
    user: User = Depends(require_auth(get_auth_service_singleton())),
) -> list[CustomAgentInfo]:
    """List all custom agents registered for the caller's tenant."""
    return [
        CustomAgentInfo.from_config(c)
        for c in _registry().list(tenant_id=user.tenant_id)
    ]


@router.get("/api/custom-agents/{agent_id}", response_model=CustomAgentDetail)
async def get_custom_agent(
    agent_id: str,
    user: User = Depends(require_auth(get_auth_service_singleton())),
) -> CustomAgentDetail:
    cfg = _registry().get(agent_id, tenant_id=user.tenant_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"agent not found: {agent_id}")
    return CustomAgentDetail(
        id=cfg.id,
        name=cfg.name,
        description=cfg.description,
        capabilities=[cfg.description],
        tools=list(cfg.tools or []),
        safety=cfg.safety.value,
        tenant_id=cfg.tenant_id,
        system_prompt=cfg.system_prompt,
        llm_config=cfg.llm_config,
    )


@router.post("/api/custom-agents/{agent_id}/run", response_model=RunResponse)
async def run_custom_agent(
    agent_id: str,
    req: RunRequest,
    user: User = Depends(require_auth(get_auth_service_singleton())),
) -> RunResponse:
    """Invoke a custom agent end-to-end. Returns the agent's output payload."""
    instance = _registry().instantiate(agent_id, tenant_id=user.tenant_id)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"agent not found: {agent_id}")

    from agent_system.core.agent import TaskContext

    task = TaskContext(
        task_id=f"custom-{user.tenant_id}-{agent_id}-{req.input[:32]}",
        input=req.input,
        config={"max_retries": 1},
        metadata={
            "user_id": user.id,
            "tenant_id": user.tenant_id,
            "agent_id": agent_id,
            "custom_metadata": req.metadata,
        },
    )
    try:
        output = await instance.do_work(task)
    except Exception as e:
        logger.exception(f"Custom agent {agent_id} failed: {e}")
        raise HTTPException(status_code=500, detail=f"agent failed: {e}")

    return RunResponse(
        agent_id=agent_id,
        status=output.type,
        output=output.payload,
        metadata={
            "output_id": output.id,
            "safety_level": instance.config.safety.value,
            "tools_used": [t.name for t in instance.tool_registry.list_definitions()],
        },
    )


@router.post("/api/custom-agents:upload", response_model=CustomAgentDetail)
async def upload_custom_agent(
    req: UploadRequest,
    user: User = Depends(require_auth(get_auth_service_singleton())),
) -> CustomAgentDetail:
    """(admin) Upload a YAML definition as a string. Validates + persists."""
    _require_admin(user)
    # Sandbox the YAML parse: import lazily so the failure mode is
    # clear and we don't take a hard dep at import time.
    try:
        from agent_system.agents.custom.loader import load_from_yaml_file
        import yaml as _yaml
        data = _yaml.safe_load(req.yaml)
        if not isinstance(data, dict):
            raise CustomAgentLoadError(
                Path := __import__("pathlib").Path("<upload>"),
                f"top-level must be a mapping, got {type(data).__name__}",
            )
        cfg = CustomAgentConfig(**data)
    except CustomAgentLoadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid YAML or schema: {e}")

    # Force tenant_id from the JWT — never trust client-supplied tenant
    cfg.tenant_id = user.tenant_id
    _registry().register(cfg)
    return CustomAgentDetail(
        id=cfg.id, name=cfg.name, description=cfg.description,
        capabilities=[cfg.description], tools=list(cfg.tools or []),
        safety=cfg.safety.value, tenant_id=cfg.tenant_id,
        system_prompt=cfg.system_prompt, llm_config=cfg.llm_config,
    )


@router.delete("/api/custom-agents/{agent_id}", status_code=204)
async def delete_custom_agent(
    agent_id: str,
    user: User = Depends(require_auth(get_auth_service_singleton())),
) -> None:
    """(admin) Remove a custom agent from the registry."""
    _require_admin(user)
    if not _registry().delete(agent_id, tenant_id=user.tenant_id):
        raise HTTPException(status_code=404, detail=f"agent not found: {agent_id}")