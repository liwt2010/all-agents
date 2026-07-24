"""Task endpoints - submit, retrieve, list, live progress, WebSocket.

All endpoints (except WebSocket) require Bearer JWT auth.
WebSocket uses token in query param (browsers cannot set headers on WS).

Endpoints:
    POST   /api/tasks                       Submit a new task
    GET    /api/tasks/{task_id}             Get task result (tenant-isolated)
    GET    /api/tasks                       List recent tasks (tenant-isolated)
    GET    /api/tasks/{task_id}/progress    Get live progress (tenant-isolated)
    WS     /api/ws/{task_id}                Real-time progress stream
"""
from __future__ import annotations

import asyncio
import json
import logging
import time as _time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel

from agent_system.api.state import (
    get_audit_logger_singleton,
    get_auth_service_singleton,
    get_checkpoint_tracker_singleton,
    get_in_flight_tasks,
    get_sanitizer_singleton,
    get_task_store_singleton,
    get_ws_connections,
)
from agent_system.core.access_control import (
    AccessControl,
    Resource,
    SpaceVisibility,
    UserContext,
)
from agent_system.core.audit_logger import AuditLogEntry
from agent_system.core.auth import User, require_auth
from agent_system.core.checkpoint_tracker import LiveProgress
from agent_system.core.event_bus import EventCategory, event_bus, make_event
from agent_system.storage.task_store import TaskRecord

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"])

# ── v0.6.0 AccessControl wiring ──

# Single shared instance — AccessControl is stateless.
_acl = AccessControl()


def _to_user_ctx(user: User) -> UserContext:
    """Map API User → AccessControl UserContext."""
    return UserContext(
        user_id=user.id,
        tenant_id=user.tenant_id,
        global_role=user.global_role.value if hasattr(user.global_role, "value") else str(user.global_role),
        perm_group_ids=list(getattr(user, "perm_group_ids", []) or []),
        group_ids=list(getattr(user, "group_ids", []) or []),
        project_ids=list(getattr(user, "project_ids", []) or []),
        is_agent=bool(getattr(user, "is_agent", False)),
    )


def _record_to_resource(record: TaskRecord) -> Resource:
    """Map TaskRecord → AccessControl Resource, including visibility flags
    stored on the record (parsed from the SpaceVisibility string)."""
    metadata = dict(record.metadata or {})
    perm_groups = metadata.pop("_perm_group_ids", None) or []
    groups = metadata.pop("_group_ids", None) or []
    projects = metadata.pop("_project_ids", None) or []
    shared_with = metadata.pop("_shared_with", None) or []
    return Resource(
        id=record.id,
        type="task",
        tenant_id=record.tenant_id,
        owner_id=record.owner_id or record.user_id,
        visibility=SpaceVisibility(record.visibility or "private"),
        perm_group_ids=list(perm_groups),
        group_ids=list(groups),
        project_ids=list(projects),
        shared_with=list(shared_with),
        metadata=metadata,
    )


def _ensure_can_read(user: User, record: TaskRecord) -> None:
    """403 if the user can't see this task via AccessControl rules."""
    if not _acl.can_read(_to_user_ctx(user), _record_to_resource(record)):
        raise HTTPException(status_code=403, detail="Access denied")


def _ensure_can_write(user: User, record: TaskRecord) -> None:
    """403 if the user can't modify this task via AccessControl rules."""
    if not _acl.can_write(_to_user_ctx(user), _record_to_resource(record)):
        raise HTTPException(status_code=403, detail="Access denied")


# ---------------------------------------------------------------------------
# Agent registry (small enough to keep here; could move to config later)
# ---------------------------------------------------------------------------
def _build_agent_registry() -> dict[str, type]:
    """Lazy import to avoid circular deps / heavy imports at module load."""
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


AGENTS = _build_agent_registry()


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class TaskRequest(BaseModel):
    input: str
    agent: str = "product"
    department_id: str = ""
    task_id: str | None = None  # user_id / tenant_id derived from JWT


class TaskResponse(BaseModel):
    task_id: str
    status: str
    output: dict[str, Any] | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Auth dependency factory - bound at include_router time
# ---------------------------------------------------------------------------
def _require_auth():
    """Factory: returns a Depends-bound require_auth with the singleton service."""
    return require_auth(get_auth_service_singleton())


# ---------------------------------------------------------------------------
# POST /api/tasks - submit a task
# ---------------------------------------------------------------------------
@router.post("/api/tasks", response_model=TaskResponse)
async def submit_task(
    request: TaskRequest,
    user: User = Depends(_require_auth()),
) -> TaskResponse:
    """Submit a task to an agent. Requires Bearer token."""
    sanitizer = get_sanitizer_singleton()
    audit_logger = get_audit_logger_singleton()
    checkpoint_tracker = get_checkpoint_tracker_singleton()
    task_store = get_task_store_singleton()

    # 1. Sanitize input
    validation = sanitizer.validate(request.input)
    if not validation.valid:
        await audit_logger.log(AuditLogEntry(
            user_id=user.id, action="task.rejected",
            details={"reason": validation.issues, "agent": request.agent},
            outcome="denied",
        ))
        raise HTTPException(status_code=400, detail={
            "error": "Input validation failed",
            "issues": validation.issues,
            "risk_level": validation.risk_level,
        })

    # 2. Derive ids from JWT (do NOT trust the request body)
    user_id = user.id
    tenant_id = user.tenant_id
    task_id = request.task_id or f"api-{uuid.uuid4().hex[:12]}"

    # 3. Resolve agent
    agent_cls = AGENTS.get(request.agent)
    if not agent_cls:
        raise HTTPException(status_code=400, detail=f"Unknown agent: {request.agent}")
    agent = agent_cls()
    sanitized_input = validation.sanitized

    # 4. Register checkpoint
    checkpoint_tracker.start(task_id, request.agent, sanitized_input, tenant_id=user.tenant_id)

    # 5. Run
    try:
        await event_bus.publish(make_event(
            category=EventCategory.AGENT, name="api.task.started",
            source=request.agent,
            data={"task_id": task_id, "user_id": user_id, "tenant_id": tenant_id},
        ))

        async def _run_and_track(agent, inp, tid):
            from agent_system.core.graph import run_agent_async
            task = asyncio.create_task(run_agent_async(agent, inp, task_id=tid))
            get_in_flight_tasks().add(task)
            try:
                return await task
            finally:
                get_in_flight_tasks().discard(task)

        result = await _run_and_track(agent, sanitized_input, task_id)
        status = result.get("status", "unknown")

        if status == "failed":
            checkpoint_tracker.fail_step(task_id, "do_work", result.get("error", ""))
        else:
            checkpoint_tracker.finish(task_id, success=True, output=result.get("output"))

        # 6. Persist result via TaskStore
        now = datetime.now(timezone.utc)
        record = TaskRecord(
            id=task_id,
            agent=request.agent,
            input=sanitized_input,
            status=status,
            tenant_id=tenant_id,
            user_id=user_id,
            output=result.get("output"),
            error=result.get("error"),
            started_at=now,
            completed_at=now,
            owner_id=user_id,                # v0.6.0: creator is immutable owner
            assignee_id=None,                # unclaimed by default
            version=1,                       # v0.6.0: initial version
            visibility="private",            # v0.6.0: most conservative default
            created_at=now,
            updated_at=now,
        )
        task_store.save(record)

        await audit_logger.log(AuditLogEntry(
            user_id=user_id,
            action="task.completed",
            resource_id=task_id,
            resource_type="task",
            task_id=task_id,                 # v0.6.0: explicit task_id for query
            details={"agent": request.agent, "status": status, "tenant_id": tenant_id},
            outcome="success",
        ))

        # Broadcast to WebSocket subscribers
        ws_conns = get_ws_connections()
        for ws in ws_conns.get(task_id, []):
            try:
                await ws.send_text(json.dumps({
                    "type": "task.complete",
                    "data": {"task_id": task_id, "status": status},
                }))
            except Exception:
                pass

        return TaskResponse(task_id=task_id, status=status, output=result.get("output"), error=result.get("error"))

    except Exception as e:
        await audit_logger.log(AuditLogEntry(
            user_id=user_id,
            action="task.failed",
            resource_id=task_id,
            resource_type="task",
            details={"agent": request.agent, "error": str(e)},
            outcome="error",
        ))
        checkpoint_tracker.fail_step(task_id, "do_work", str(e))
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /api/tasks/{task_id} - get task result
# ---------------------------------------------------------------------------
@router.get("/api/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    user: User = Depends(_require_auth()),
) -> TaskResponse:
    """Get a task by id. Tenant-isolated + AccessControl-filtered."""
    task_store = get_task_store_singleton()
    record = task_store.get(task_id)
    if not record:
        raise HTTPException(status_code=404, detail="Task not found")
    _ensure_can_read(user, record)  # v0.6.0: also checks visibility (private / shared / etc.)
    return TaskResponse(
        task_id=record.id,
        status=record.status,
        output=record.output,
        error=record.error,
    )


# ---------------------------------------------------------------------------
# GET /api/tasks - list tasks
# ---------------------------------------------------------------------------
@router.get("/api/tasks")
async def list_tasks(
    limit: int = Query(10, le=100),
    offset: int = Query(0, ge=0),
    status: str | None = None,
    user: User = Depends(_require_auth()),
) -> dict[str, Any]:
    """List recent tasks. Tenant-isolated + AccessControl-filtered."""
    task_store = get_task_store_singleton()
    all_records = task_store.list(tenant_id=user.tenant_id, status=status, limit=limit + offset)
    # v0.6.0: post-filter by AccessControl (SQL pushdown is a future TODO).
    ctx = _to_user_ctx(user)
    visible = [r for r in all_records if _acl.can_read(ctx, _record_to_resource(r))]
    page = visible[offset:offset + limit]
    return {
        "tasks": [
            {
                "task_id": r.id,
                "agent": r.agent,
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
            }
            for r in page
        ],
        "total": len(page),
        "offset": offset,
    }


# ---------------------------------------------------------------------------
# GET /api/tasks/{task_id}/progress - live progress
# ---------------------------------------------------------------------------
@router.get("/api/tasks/{task_id}/progress", response_model=LiveProgress)
async def get_task_progress(
    task_id: str,
    user: User = Depends(_require_auth()),
) -> LiveProgress:
    """Get live progress for a running or recently-completed task."""
    checkpoint_tracker = get_checkpoint_tracker_singleton()
    progress = checkpoint_tracker.get_live(task_id)
    if not progress:
        raise HTTPException(status_code=404, detail="No progress info for this task")
    # v0.6.0: AccessControl — read access to the task is required to see
    # progress. Cross-tenant check is folded into can_read.
    task_store = get_task_store_singleton()
    record = task_store.get(task_id)
    if record is not None:
        _ensure_can_read(user, record)
    elif progress.tenant_id != user.tenant_id and user.global_role.value not in (
        "platform_admin", "tenant_admin"
    ):
        raise HTTPException(status_code=403, detail="Access denied")
    return progress


# ---------------------------------------------------------------------------
# WS /api/ws/{task_id} - WebSocket for real-time progress
# ---------------------------------------------------------------------------
@router.websocket("/api/ws/{task_id}")
async def websocket_endpoint(
    ws: WebSocket,
    task_id: str,
) -> None:
    """WebSocket for real-time task progress. Requires token in query param."""
    auth_service = get_auth_service_singleton()
    task_store = get_task_store_singleton()
    ws_conns = get_ws_connections()

    # Auth via query param (browsers cannot set headers on WS upgrade)
    token = ws.query_params.get("token")
    if not token:
        await ws.close(code=1008)
        return
    payload = auth_service.verify_token(token)
    if not payload:
        await ws.close(code=1008)
        return

    # Tenant isolation
    try:
        record = task_store.get(task_id)
        if record and record.tenant_id != payload.tenant_id:
            await ws.close(code=1008)
            return
    except Exception:
        pass  # allow WS if task store lookup fails

    await ws.accept()
    if task_id not in ws_conns:
        ws_conns[task_id] = []
    ws_conns[task_id].append(ws)

    # Per-connection rate limiting
    _msg_count = 0
    _msg_start = _time.time()
    _msg_limit_per_sec = 20
    try:
        while True:
            data = await ws.receive_text()
            _msg_count += 1
            _elapsed = _time.time() - _msg_start
            if _elapsed > 1.0:
                _msg_count = 0
                _msg_start = _time.time()
            elif _msg_count > _msg_limit_per_sec:
                await ws.close(code=1009)
                break
            try:
                await ws.send_text(json.dumps({"type": "pong", "data": data}))
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        if task_id in ws_conns and ws in ws_conns[task_id]:
            ws_conns[task_id].remove(ws)
            if not ws_conns[task_id]:
                del ws_conns[task_id]
