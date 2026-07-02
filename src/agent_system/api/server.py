"""
Agent System — FastAPI REST API

Endpoints:
  POST /api/tasks          Submit a task
  GET  /api/tasks/{id}     Get task result
  GET  /api/tasks          List tasks
  GET  /api/agents         List available agents
  GET  /api/graph/stats    Graph statistics
  GET  /api/graph/node/{id} Get graph node
  GET  /api/metrics        Prometheus-compatible metrics
  GET  /api/health         Health check (public)
  POST /api/auth/token     Issue JWT token (for testing; in prod use SSO)
"""

import json
import logging
import asyncio  # noqa: F811
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Load .env before any imports that read os.environ
load_dotenv()

from agent_system.agents.product_agent import ProductAgent
from agent_system.agents.tech_agent import TechAgent
from agent_system.agents.test_agent import TestAgent
from agent_system.agents.deploy_agent import DeployAgent
from agent_system.agents.ceo_agent import CEOAgent
from agent_system.core.graph import run_agent_async
from agent_system.core.schema import OutputSchema
from agent_system.core.event_bus import event_bus, EventCategory, EventSeverity, make_event
from agent_system.memory.graph import get_graph, NodeType
from agent_system.memory.persistence import save_graph, load_graph
from agent_system.core.observability import MetricsCalculator, tracer
from agent_system.core.checkpoint_tracker import CheckpointTracker, LiveProgress
from agent_system.core.security import sanitizer, audit_logger, AuditLogEntry
from agent_system.core.auth import (
    AuthService, AuthMiddleware, User, require_auth, get_auth_service,
)
from agent_system.core.security_middleware import (
    SecurityHeadersMiddleware, RateLimitMiddleware,
    RequestSizeLimitMiddleware, SecretsInRequestMiddleware,
)
from agent_system.storage.task_store import TaskRecord, get_task_store

logger = logging.getLogger(__name__)

# ── App-wide singletons ──

_start_time = datetime.now(timezone.utc)
_task_store = get_task_store()
_checkpoint_tracker = CheckpointTracker()
_auth_service = get_auth_service()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load persisted graph on startup, save on shutdown."""
    try:
        load_graph()
        logger.info("Graph loaded from disk")
    except Exception as e:
        logger.warning(f"Could not load graph from disk: {e}")
    yield
    # Graceful shutdown: wait for in-flight tasks
    if _in_flight_tasks:
        shutdown_timeout = 25
        logger.info(f"Waiting for {len(_in_flight_tasks)} in-flight tasks ({shutdown_timeout}s)...")
        done, pending = await asyncio.wait(
            _in_flight_tasks, timeout=shutdown_timeout
        )
        if pending:
            logger.warning(f"{len(pending)} tasks did not complete before shutdown")
        else:
            logger.info("All in-flight tasks completed")
    try:
        graph = get_graph()
        save_graph(graph)
        logger.info("Graph saved to disk")
    except Exception as e:
        logger.warning(f"Could not save graph: {e}")


# ── FastAPI app ──

app = FastAPI(
    title="Agent System API",
    version="0.1.0",
    description="Enterprise Multi-Agent Platform",
    lifespan=lifespan,
)

# Wire auth: extracts User from Authorization header and sets TenantContext
app.add_middleware(AuthMiddleware, auth_service=_auth_service)

# Security middleware: rate limit, request size cap, secrets detection,
# security headers. Disabled in tests by default; enable in production.
if os.environ.get("DISABLE_SECURITY_MIDDLEWARE") != "1":
    app.add_middleware(SecurityHeadersMiddleware, is_production=os.environ.get("ENVIRONMENT") == "production")
    app.add_middleware(RateLimitMiddleware, rate_per_minute=int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60")))
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=int(os.environ.get("MAX_REQUEST_BYTES", str(1024 * 1024))))
    app.add_middleware(SecretsInRequestMiddleware)

# CORS — narrowed to actual production domains.
# In dev, CORS_DEV_ORIGINS env var (comma-separated) extends this.
_default_cors = ["http://localhost:5173", "http://127.0.0.1:5173"]
# WebSocket registry (keyed by task_id, list of subscribers)
_ws_connections: Dict[str, List[WebSocket]] = {}

# In-flight task registry for graceful shutdown
_in_flight_tasks: set = set()


_extra = os.environ.get("CORS_DEV_ORIGINS", "")
if _extra:
    _default_cors.extend(o.strip() for o in _extra.split(",") if o.strip())
app.add_middleware(
    CORSMiddleware,
    allow_origins=_default_cors,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# ── Agent factory ──

AGENTS = {
    "product": ProductAgent,
    "tech": TechAgent,
    "test": TestAgent,
    "deploy": DeployAgent,
    "ceo": CEOAgent,
}


# ── Request/Response models ──

class TaskRequest(BaseModel):
    input: str
    agent: str = "product"
    department_id: str = ""
    task_id: Optional[str] = None
    # user_id and tenant_id are derived from the JWT, not the request body


class TaskResponse(BaseModel):
    task_id: str
    status: str
    output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class AgentInfo(BaseModel):
    name: str
    description: str
    capabilities: List[str]


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    uptime: float = 0.0


class TokenRequest(BaseModel):
    user_id: str
    tenant_id: str = "default"
    role: str = "user"
    ttl: Optional[int] = None


class TokenResponse(BaseModel):
    access_token: str
    expires_in: int


# ── Public endpoints ──

@app.get("/api/health", response_model=HealthResponse)
async def health():
    """Liveness probe — always returns 200."""
    uptime = (datetime.now(timezone.utc) - _start_time).total_seconds()
    return HealthResponse(uptime=uptime)


@app.get("/api/ready")
async def ready():
    """Readiness probe — checks that core dependencies are working."""
    checks = {}
    # Check storage
    try:
        _task_store.get("__readiness_check__")
        checks["storage"] = "ok"
    except Exception as e:
        checks["storage"] = f"error: {e}"
    # Check graph loaded
    try:
        get_graph()
        checks["graph"] = "ok"
    except Exception as e:
        checks["graph"] = f"error: {e}"
    all_ok = all(v == "ok" for v in checks.values())
    # In production, also require a real Anthropic API key
    try:
        from agent_system.core.llm_router import router as _llm_router
        _llm_router.require_real_key()
        checks["llm"] = "ok"
    except RuntimeError as e:
        checks["llm"] = f"error: {e}"
        all_ok = False
    status_code = 200 if all_ok else 503
    return {"status": "ok" if all_ok else "degraded", "checks": checks}


# ── Auth endpoint (for testing; in prod, use SSO) ──

@app.post("/api/auth/token", response_model=TokenResponse)
async def issue_token(req: TokenRequest):
    """Issue a JWT for the given user. For local dev/testing only.

    In production, replace with an SSO/OIDC integration.
    """
    token = _auth_service.issue_token(
        user_id=req.user_id,
        tenant_id=req.tenant_id,
        role=req.role,
        ttl=req.ttl or 3600,
    )
    return TokenResponse(access_token=token, expires_in=req.ttl or 3600)


# ── Task endpoints (auth required) ──

@app.post("/api/tasks", response_model=TaskResponse)
async def submit_task(
    request: TaskRequest,
    user: User = Depends(require_auth(_auth_service)),
):
    """Submit a task to an agent. Requires Bearer token."""
    # 1. Sanitize input
    validation = sanitizer.validate(request.input)
    if not validation.valid:
        audit_logger.log(AuditLogEntry(
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
    department_id = request.department_id  # optional user-provided tag

    task_id = request.task_id or f"api-{uuid.uuid4().hex[:12]}"

    # 3. Resolve agent
    agent_cls = AGENTS.get(request.agent)
    if not agent_cls:
        raise HTTPException(status_code=400, detail=f"Unknown agent: {request.agent}")
    agent = agent_cls()
    sanitized_input = validation.sanitized

    # 4. Register checkpoint
    _checkpoint_tracker.start(task_id, request.agent, sanitized_input, tenant_id=user.tenant_id)

    # 5. Run
    try:
        await event_bus.publish(make_event(
            category=EventCategory.AGENT, name="api.task.started",
            source=request.agent,
            data={"task_id": task_id, "user_id": user_id, "tenant_id": tenant_id},
        ))

        async def _run_and_track(agent, inp, tid):
            task = asyncio.create_task(run_agent_async(agent, inp, task_id=tid))
            _in_flight_tasks.add(task)
            try:
                return await task
            finally:
                _in_flight_tasks.discard(task)
        result = await _run_and_track(agent, sanitized_input, task_id)
        status = result.get("status", "unknown")

        if status == "failed":
            _checkpoint_tracker.fail_step(task_id, "do_work", result.get("error", ""))
        else:
            _checkpoint_tracker.finish(task_id, success=True, output=result.get("output"))

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
        _task_store.save(record)

        audit_logger.log(AuditLogEntry(
            user_id=user_id,
            action="task.completed",
            resource_id=task_id,
            resource_type="task",
            details={"agent": request.agent, "status": status, "tenant_id": tenant_id},
        ))

        return TaskResponse(
            task_id=task_id,
            status=status,
            output=result.get("output"),
            error=result.get("error"),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Task {task_id} failed unexpectedly")
        _checkpoint_tracker.fail_step(task_id, "do_work", str(e))
        audit_logger.log(AuditLogEntry(
            user_id=user_id,
            action="task.failed",
            resource_id=task_id,
            details={"error": str(e)[:500]},
            outcome="failure",
        ))
        return TaskResponse(
            task_id=task_id,
            status="failed",
            error=str(e)[:500],
        )


@app.get("/api/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    user: User = Depends(require_auth(_auth_service)),
):
    """Get a task by id. Tenant-isolated."""
    record = _task_store.get(task_id)
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


@app.get("/api/tasks")
async def list_tasks(
    limit: int = Query(10, le=100),
    offset: int = Query(0, ge=0),
    status: Optional[str] = None,
    user: User = Depends(require_auth(_auth_service)),
):
    """List recent tasks (tenant-isolated)."""
    # List with offset pagination
    all_records = _task_store.list(tenant_id=user.tenant_id, status=status, limit=limit + offset)
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


# ── Agent endpoints ──

@app.get("/api/agents", response_model=List[AgentInfo])
async def list_agents(
    user: User = Depends(require_auth(_auth_service)),
):
    """List available agents."""
    agents = []
    for name, cls in AGENTS.items():
        instance = cls()
        agents.append(AgentInfo(
            name=name,
            description=instance.description,
            capabilities=instance.agent_capabilities,
        ))
    return agents


# ── Graph endpoints ──

@app.get("/api/graph/stats")
async def graph_stats(
    user: User = Depends(require_auth(_auth_service)),
):
    """Get graph statistics (tenant-isolated)."""
    graph = get_graph()
    return graph.stats()


@app.get("/api/graph/node/{node_id}")
async def get_graph_node(
    node_id: str,
    user: User = Depends(require_auth(_auth_service)),
):
    """Get a graph node with neighbors (tenant-isolated)."""
    graph = get_graph()
    ctx = graph.related_with_context(node_id)
    if not ctx["node"]:
        raise HTTPException(status_code=404, detail="Node not found")

    # Tenant isolation check
    node_tenant = (ctx["node"].metadata or {}).get("tenant_id", "default")
    if node_tenant != user.tenant_id and user.global_role.value not in ("platform_admin", "tenant_admin"):
        raise HTTPException(status_code=403, detail="Access denied")

    return {
        "node": ctx["node"].model_dump(mode="json"),
        "neighbors": [
            {
                "node_id": n.node.id,
                "node_type": n.node.type.value,
                "link_type": n.link.link_type.value,
                "depth": n.depth,
            }
            for n in ctx["neighbors"]
        ],
        "outgoing_count": ctx["outgoing_count"],
        "incoming_count": ctx["incoming_count"],
    }


# ── Metrics endpoint ──

@app.get("/api/metrics")
async def get_metrics(
    user: User = Depends(require_auth(_auth_service)),
):
    """Get Prometheus-compatible metrics."""
    calc = MetricsCalculator()
    metrics = calc.calculate_all()
    return {
        "metrics": {
            name: {"value": m.value, "unit": m.unit, "labels": m.labels}
            for name, m in metrics.items()
        }
    }


@app.get("/api/metrics/prometheus")
async def get_prometheus_metrics(
    user: User = Depends(require_auth(_auth_service)),
):
    """Get metrics in Prometheus text format."""
    from agent_system.observability.metrics import get_metrics_registry
    text = get_metrics_registry().render()
    return {"metrics_text": text}


@app.get("/api/tasks/{task_id}/progress", response_model=LiveProgress)
async def get_task_progress(
    task_id: str,
    user: User = Depends(require_auth(_auth_service)),
):
    """Get live progress for a running or recently-completed task."""
    progress = _checkpoint_tracker.get_live(task_id)
    if not progress:
        raise HTTPException(status_code=404, detail="No progress info for this task")
    # Tenant isolation
    if progress.tenant_id != user.tenant_id and user.global_role.value not in ("platform_admin", "tenant_admin"):
        raise HTTPException(status_code=403, detail="Access denied")
    return progress


# ── WebSocket endpoint ──

@app.websocket("/api/ws/{task_id}")
async def websocket_endpoint(
    ws: WebSocket,
    task_id: str,
):
    """WebSocket for real-time task progress. Requires token in query param."""
    import time as _time
    token = ws.query_params.get("token")
    if not token:
        await ws.close(code=1008)
        return
    payload = _auth_service.verify_token(token)
    if not payload:
        await ws.close(code=1008)
        return
    # Tenant isolation: check task belongs to user's tenant
    try:
        record = _task_store.get(task_id)
        if record and record.tenant_id != payload.tenant_id:
            await ws.close(code=1008)
            return
    except Exception:
        pass  # if task store fails, allow WS (non-critical)
    await ws.accept()
    if task_id not in _ws_connections:
        _ws_connections[task_id] = []
    _ws_connections[task_id].append(ws)
    _msg_count = 0
    _msg_start = _time.time()
    _msg_limit_per_sec = 20
    try:
        while True:
            data = await ws.receive_text()
            # Per-connection message rate limit
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
        if task_id in _ws_connections and ws in _ws_connections[task_id]:
            _ws_connections[task_id].remove(ws)
            if not _ws_connections[task_id]:
                del _ws_connections[task_id]
