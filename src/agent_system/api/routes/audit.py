"""Audit log query endpoint - admin only."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from agent_system.api.state import get_auth_service_singleton
from agent_system.core.auth import User, require_auth

router = APIRouter(tags=["audit"])


@router.get("/api/audit/query")
async def query_audit(
    user_id: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    request_id: str | None = None,
    task_id: str | None = None,
    limit: int = Query(default=100, le=1000),
    user: User = Depends(require_auth(get_auth_service_singleton())),
) -> dict[str, Any]:
    """Query audit log (authenticated). Returns up to `limit` matching entries.

    The `task_id` filter (v0.6.0) matches entries with the explicit
    `task_id` field set, falling back to legacy entries that wrote
    `resource_type="task"` + `resource_id=task_id`.
    """
    from agent_system.core.audit_logger import get_audit_logger
    audit = get_audit_logger()
    entries = audit.query_from_disk(
        user_id=user_id,
        action=action,
        outcome=outcome,
        start_date=start_date,
        end_date=end_date,
        request_id=request_id,
        task_id=task_id,
        limit=limit,
    )
    return {
        "count": len(entries),
        "entries": [e.model_dump(mode="json") for e in entries],
    }
