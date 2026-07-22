"""Agent System - FastAPI REST API entry point.

This module is the thin entry point: it builds the FastAPI app,
wires middleware, configures lifespan, and includes routers from
agent_system.api.routes.

Route handlers themselves live in:
    agent_system.api.routes.health    - liveness + readiness
    agent_system.api.routes.auth      - JWT issuance (dev/test)
    agent_system.api.routes.tasks     - submit/get/list/progress + WebSocket
    agent_system.api.routes.agents    - agent discovery
    agent_system.api.routes.graph     - memory graph query
    agent_system.api.routes.metrics   - metrics (JSON + Prometheus)
    agent_system.api.routes.audit     - audit log query

Shared singletons (task_store, auth_service, sanitizer, etc.) live in
agent_system.api.state.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Load .env before any imports that read os.environ
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_system.api.routes import (
    agents_router,
    audit_router,
    auth_router,
    graph_router,
    health_router,
    metrics_router,
    tasks_router,
)
from agent_system.api.state import (
    get_auth_service_singleton,
    get_checkpoint_tracker_singleton,
    get_in_flight_tasks,
    get_start_time,
    _checkpoint_tracker,
)
from agent_system.core.auth import AuthMiddleware
from agent_system.core.metrics_middleware import MetricsMiddleware
from agent_system.core.security.cors import build_cors_config
from agent_system.core.security.tls import (
    HSTSHeaderMiddleware,
    HTTPSRedirectMiddleware,
    SecureCookieChecker,
)
from agent_system.core.security_middleware import (
    RateLimitMiddleware,
    RequestIDMiddleware,
    RequestSizeLimitMiddleware,
    SecretsInRequestMiddleware,
    SecurityHeadersMiddleware,
    SlidingWindowRateLimitMiddleware,
)
from agent_system.memory.graph import get_graph
from agent_system.memory.persistence import load_graph, save_graph

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan: startup / shutdown hooks
# ---------------------------------------------------------------------------
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
    in_flight = get_in_flight_tasks()
    if in_flight:
        shutdown_timeout = 25
        logger.info(f"Waiting for {len(in_flight)} in-flight tasks ({shutdown_timeout}s)...")
        done, pending = await asyncio.wait(in_flight, timeout=shutdown_timeout)
        if pending:
            logger.warning(f"{len(pending)} tasks did not complete before shutdown")
        else:
            logger.info("All in-flight tasks completed")
    try:
        save_graph(get_graph())
        logger.info("Graph saved to disk")
    except Exception as e:
        logger.warning(f"Could not save graph: {e}")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Agent System API",
    version="0.1.0",
    description=(
        "Enterprise Multi-Agent Platform - production-grade agent orchestration API.\n\n"
        "## Features\n"
        "- **Multi-agent orchestration**: Product, Tech, Test, Deploy, CEO agents\n"
        "- **4-way resolution**: SELF / PEER / HUMAN / ESCALATE\n"
        "- **Schema tolerance**: 4-tier validation (STRICT / LENIENT / REPAIR / WARN)\n"
        "- **Data provenance**: Every output labeled REAL_LLM / MOCK / LLM_FAILURE\n"
        "- **Experience feedback loop**: Failed tasks inform future attempts\n"
        "- **Distributed tracing**: OpenTelemetry OTLP exporter (Jaeger / Tempo / SigNoz)\n"
        "- **Prometheus metrics**: 11 metrics at /metrics\n"
        "- **Audit log**: Queryable batch logger with retention\n"
        "- **Rate limit**: Per-user / per-scope sliding window\n\n"
        "## Auth\n"
        "JWT bearer token in `Authorization: Bearer <token>` header. "
        "See `/api/auth/token` for the OAuth2 password flow.\n\n"
        "## Idempotency\n"
        "POST endpoints accept `Idempotency-Key` header to make retries safe.\n\n"
        "## Rate limit\n"
        "Default: 120 req/min/user, 20 req/min on /execute endpoints. "
        "Returns `429` with `X-RateLimit-*` headers when exceeded."
    ),
    contact={
        "name": "Agent System Team",
        "url": "https://github.com/liwt2010/all-agents",
    },
    license_info={
        "name": "MIT",
        "url": "https://opensource.org/licenses/MIT",
    },
    servers=[
        {"url": "https://api.agentsystem.example.com", "description": "Production"},
        {"url": "https://staging-api.agentsystem.example.com", "description": "Staging"},
        {"url": "http://localhost:8000", "description": "Local dev"},
    ],
    openapi_tags=[
        {"name": "health", "description": "Liveness + readiness probes"},
        {"name": "auth", "description": "JWT login / token refresh"},
        {"name": "agents", "description": "Run individual agents (product, tech, test, deploy, ceo)"},
        {"name": "pipeline", "description": "Multi-agent pipeline orchestration (product->tech->test->deploy)"},
        {"name": "tasks", "description": "Task submission, status, and progress streaming"},
        {"name": "memory", "description": "Memory graph CRUD + experience query"},
        {"name": "audit", "description": "Audit log query + export"},
        {"name": "metrics", "description": "Prometheus metrics (scraped at /metrics)"},
    ],
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Middleware (order matters - first added = outermost)
# ---------------------------------------------------------------------------
# Auth: extracts User from Authorization header and sets TenantContext
app.add_middleware(AuthMiddleware, auth_service=get_auth_service_singleton())

# Request ID propagation - must be added BEFORE logging middleware
app.add_middleware(RequestIDMiddleware)

# Metrics middleware (records HTTP request count + duration)
app.add_middleware(MetricsMiddleware)

# Security middleware (disabled in tests by default)
if os.environ.get("DISABLE_SECURITY_MIDDLEWARE") != "1":
    is_prod = os.environ.get("ENVIRONMENT") == "production"
    app.add_middleware(SecurityHeadersMiddleware, is_production=is_prod)
    app.add_middleware(
        RequestSizeLimitMiddleware,
        max_bytes=int(os.environ.get("MAX_REQUEST_BYTES", str(1024 * 1024))),
    )
    app.add_middleware(SecretsInRequestMiddleware)
    if os.environ.get("AGENT_RATE_LIMIT_ENABLED", "true").lower() in ("1", "true", "yes"):
        app.add_middleware(SlidingWindowRateLimitMiddleware)
    else:
        # Legacy IP-only limiter as fallback
        app.add_middleware(
            RateLimitMiddleware,
            rate_per_minute=int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60")),
        )

# TLS / HTTPS enforcement
app.add_middleware(SecureCookieChecker)
app.add_middleware(HSTSHeaderMiddleware)
app.add_middleware(HTTPSRedirectMiddleware)

# CORS (environment-aware)
_cors_config = build_cors_config()
app.add_middleware(CORSMiddleware, **_cors_config.to_fastapi_kwargs())
logger.info(
    "CORS configured for env=%s, %d origins, credentials=%s",
    _cors_config.environment,
    len(_cors_config.allowed_origins),
    _cors_config.allow_credentials,
)


# ---------------------------------------------------------------------------
# Optional dependency check: AutoGen (PEER path upgrade)
# ---------------------------------------------------------------------------
try:
    from agent_system.core.autogen_discussion import HAS_AUTOGEN
    if HAS_AUTOGEN:
        logger.info("PEER path: AutoGen 0.4+ enabled (full multi-agent debate available).")
    else:
        logger.warning(
            "================================================================\n"
            " PEER path running in FALLBACK mode: AutoGen 0.4+ not installed.\n"
            "   Multi-agent debate is disabled. Resolver will use the lightweight\n"
            "   DiscussionMixin path (single-shot consensus, no AutoGen team chat).\n"
            "   To enable full PEER: pip install 'autogen-agentchat>=0.4' 'autogen-ext[openai]>=0.4'\n"
            "================================================================"
        )
except Exception as _e:  # pragma: no cover - defensive only
    logger.debug(f"Optional autogen probe failed: {_e}")


# ---------------------------------------------------------------------------
# Mount routers (route handlers themselves are in agent_system.api.routes.*)
# ---------------------------------------------------------------------------
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(tasks_router)
app.include_router(agents_router)
app.include_router(graph_router)
app.include_router(metrics_router)
app.include_router(audit_router)
