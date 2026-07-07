"""
FastAPI middleware that records HTTP request metrics for Prometheus scraping.

Records:
    - agent_http_requests_total{method, path, status} — Counter
    - agent_http_request_duration_seconds{method, path} — Histogram
"""

import logging
import os
import re
import time
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from agent_system.observability.metrics import get_metrics_registry

logger = logging.getLogger(__name__)

# Path normalization: collapse IDs to {id} to keep label cardinality bounded.
# e.g. /api/agents/abc-123 → /api/agents/{id}
_PATH_ID_PATTERN = re.compile(r"/[a-f0-9-]{8,}|/\d+")


def _normalize_path(path: str) -> str:
    """Replace high-cardinality path segments with placeholders."""
    return _PATH_ID_PATTERN.sub("/{id}", path)


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record HTTP request count + duration in Prometheus metrics."""

    def __init__(self, app, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled and os.environ.get("AGENT_OBSERVABILITY_ENABLED", "true").lower() in ("1", "true", "yes")
        if self.enabled:
            self._requests = get_metrics_registry().counter(
                "agent_http_requests_total",
                "Total HTTP requests",
                ["method", "path", "status"],
            )
            self._duration = get_metrics_registry().histogram(
                "agent_http_request_duration_seconds",
                "HTTP request duration in seconds",
                label_names=["method", "path"],
            )

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self.enabled:
            return await call_next(request)

        started = time.perf_counter()
        method = request.method
        path = _normalize_path(request.url.path)
        status = 500  # default if exception raised before response
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        except Exception:
            # Re-raise after recording metrics
            self._requests.inc(1.0, method=method, path=path, status=status)
            self._duration.observe(time.perf_counter() - started, method=method, path=path)
            raise
        finally:
            self._requests.inc(1.0, method=method, path=path, status=status)
            self._duration.observe(time.perf_counter() - started, method=method, path=path)