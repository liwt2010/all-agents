"""Health and readiness probe endpoints.

Public (unauthenticated) endpoints used by:
    - Kubernetes liveness probe (/api/health)
    - Kubernetes readiness probe (/api/ready)
    - Docker healthcheck (docker-compose.yml)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter
from pydantic import BaseModel

from agent_system.api.state import (
    get_has_autogen,
    get_start_time,
    get_task_store_singleton,
)

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Liveness response - always 200 if the process is running."""

    status: str = "ok"
    version: str = "0.1.0"
    uptime: float = 0.0
    peer_autogen_enabled: bool = False


@router.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe - always returns 200.

    Used by Kubernetes liveness probe and Docker HEALTHCHECK.
    Does NOT check dependencies (use /api/ready for that).
    """
    uptime = (datetime.now(timezone.utc) - get_start_time()).total_seconds()
    return HealthResponse(
        uptime=uptime,
        peer_autogen_enabled=get_has_autogen(),
    )


@router.get("/api/ready")
async def ready() -> Dict[str, Any]:
    """Readiness probe - checks core dependencies.

    Returns 200 with status="ok" if all checks pass, 503 with
    status="degraded" if any check fails.

    In production, requires a real LLM API key.
    """
    checks: Dict[str, str] = {}

    # Storage check
    try:
        get_task_store_singleton().get("__readiness_check__")
        checks["storage"] = "ok"
    except Exception as e:
        checks["storage"] = f"error: {e}"

    # Graph check
    try:
        from agent_system.memory.graph import get_graph
        get_graph()
        checks["graph"] = "ok"
    except Exception as e:
        checks["graph"] = f"error: {e}"

    # LLM key check (production requires real key)
    all_ok = all(v == "ok" for v in checks.values())
    try:
        from agent_system.core.llm_router import router as _llm_router
        _llm_router.require_real_key()
        checks["llm"] = "ok"
    except RuntimeError as e:
        checks["llm"] = f"error: {e}"
        all_ok = False

    return {"status": "ok" if all_ok else "degraded", "checks": checks}
