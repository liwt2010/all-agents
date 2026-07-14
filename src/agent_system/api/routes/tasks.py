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
from typing import Any, Dict, Optional

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
from agent_system.core.audit_logger import AuditLogEntry
from agent_system.core.auth import User, require_auth
from agent_system.core.checkpoint_tracker import LiveProgress
from agent_system.core.event_bus import EventCategory, event_bus, make_event
from agent_system.storage.task_store import TaskRecord

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"])


# ---------------------------------------------------------------------------
# Agent registry (small enough to keep here; could move to config later)
# ---------------------------------------------------------------------------
def _build_agent_registry() -> Dict[str, type]:
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
    task_id: Optional[str] = None  # user_id / tenant_id derived from JWT


class TaskResponse(BaseModel):
    task_id: str
    status: str
    output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


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
        record = TaskRecord(
            id=task_id,
            agent=request.agent,
            input=sanitized_input,
            status=status,
            tenant_id=tenant_id,
            user_id=user_id,
            output=result.get("output"),
            error=result.get("error"),
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
        task_store.save(record)

        await audit_logger.log(AuditLogEntry(
            user_id=user_id,
            action="task.completed",
            resource_id=task_id,
            resource_type="task",
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
    """Get a task by id. Tenant-isolated."""
    task_store = get_task_store_singleton()
    record = task_store.get(task_id)
    if not record:
        raise HTTPException(status_code=404, detail="Task not found")
    # Tenant isolation: only the task's tenant can read it (admins can read all)
    if record.tenant_id != user.tenant_id and user.global_role.value not in ("platform_admin", "tenant_admin"):
        raise HTTPException(status_code=403, detail="Access denied")
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
    status: Optional[str] = None,
    user: User = Depends(_require_auth()),
) -> Dict[str, Any]:
    """List recent tasks (tenant-isolated)."""
    task_store = get_task_store_singleton()
    all_records = task_store.list(tenant_id=user.tenant_id, status=status, limit=limit + offset)
    page = all_records[offset:offset + limit]
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
    # Tenant isolation
    if progress.tenant_id != user.tenant_id and user.global_role.value not in ("platform_admin", "tenant_admin"):
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
