"""
FastAPI middleware that auto-wraps every HTTP request in an OTel span.

Only emits spans if init_otel_exporter() has been called with enabled=True.
Otherwise it is a no-op (zero overhead).

Span name: "http.{method} {path}"
Attributes:
  - http.method, http.route, http.status_code
  - request.id (from RequestIDMiddleware if installed)
  - user agent, client IP

Usage:
    from agent_system.observability.otel_middleware import OTelMiddleware
    app.add_middleware(OTelMiddleware)
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

logger = logging.getLogger(__name__)


class OTelMiddleware:
    """
    Pure ASGI middleware (no BaseHTTPMiddleware overhead) that wraps each
    request in an OTel span. No-op when OTel is disabled.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Skip non-HTTP scopes (lifespan, websocket)
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from agent_system.observability.otel_exporter import is_enabled, get_otel_tracer
        if not is_enabled():
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        # Build span name: GET /api/v1/agents -> "http.GET /api/v1/agents"
        span_name = f"http.{method} {path}"

        # Get matched route template (set by FastAPI during routing)
        # We can't know it here pre-routing; use raw path for now.
        tracer = get_otel_tracer("agent_system.api")
        start = time.perf_counter()
        status_holder = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message.get("status", 500)
            await send(message)

        with tracer.start_as_current_span(
            span_name,
            attributes={
                "http.method": method,
                "http.route": path,
                "http.scheme": scope.get("scheme", "http"),
                "http.host": ",".join(scope.get("headers", []).__class__.__name__ and
                                       [v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"host"] or [""]),
            },
        ) as span:
            try:
                await self.app(scope, receive, send_wrapper)
                duration = (time.perf_counter() - start) * 1000
                code = status_holder["code"]
                span.set_attribute("http.status_code", code)
                span.set_attribute("http.duration_ms", round(duration, 2))
                # Mark as error on 5xx
                if code >= 500:
                    from opentelemetry.trace import Status, StatusCode
                    span.set_status(Status(StatusCode.ERROR, f"HTTP {code}"))
                else:
                    from opentelemetry.trace import Status, StatusCode
                    span.set_status(Status(StatusCode.OK))
            except Exception as e:
                duration = (time.perf_counter() - start) * 1000
                span.set_attribute("http.duration_ms", round(duration, 2))
                span.record_exception(e)
                from opentelemetry.trace import Status, StatusCode
                span.set_status(Status(StatusCode.ERROR, str(e)[:200]))
                raise
