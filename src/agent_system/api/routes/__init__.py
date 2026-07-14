"""Routes package - split server.py into focused modules.

Each module exposes an `APIRouter` named `router` that the main
server.py includes via `app.include_router(...)`.

Layout:
    - health.py    liveness + readiness probes (public)
    - auth.py      JWT issuance (dev/test only)
    - tasks.py     task submit/get/list/progress + WebSocket
    - agents.py    agent discovery
    - graph.py     memory graph query
    - metrics.py   metrics (JSON + Prometheus)
    - audit.py     audit log query
"""
from agent_system.api.routes.agents import router as agents_router
from agent_system.api.routes.audit import router as audit_router
from agent_system.api.routes.auth import router as auth_router
from agent_system.api.routes.graph import router as graph_router
from agent_system.api.routes.health import router as health_router
from agent_system.api.routes.metrics import router as metrics_router
from agent_system.api.routes.tasks import router as tasks_router

__all__ = [
    "agents_router",
    "audit_router",
    "auth_router",
    "graph_router",
    "health_router",
    "metrics_router",
    "tasks_router",
]
