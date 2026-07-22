"""Metrics endpoints - JSON and Prometheus exposition."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Response
from fastapi.responses import PlainTextResponse

from agent_system.api.state import get_auth_service_singleton
from agent_system.core.auth import User, require_auth

router = APIRouter(tags=["metrics"])


@router.get("/api/metrics")
async def get_metrics(
    user: User = Depends(require_auth(get_auth_service_singleton())),
) -> dict[str, Any]:
    """Get Prometheus-compatible metrics as JSON."""
    from agent_system.core.observability import MetricsCalculator
    calc = MetricsCalculator()
    metrics = calc.calculate_all()
    return {
        "metrics": {
            name: {"value": m.value, "unit": m.unit, "labels": m.labels}
            for name, m in metrics.items()
        }
    }


@router.get("/api/metrics/prometheus")
async def get_prometheus_metrics(
    user: User = Depends(require_auth(get_auth_service_singleton())),
) -> dict[str, str]:
    """Get metrics in Prometheus text format (JSON wrapped)."""
    from agent_system.observability.metrics import get_metrics_registry
    text = get_metrics_registry().render()
    return {"metrics_text": text}


@router.get("/metrics", response_class=Response)
async def metrics_prometheus_text() -> PlainTextResponse:
    """Standard Prometheus scrape endpoint (text/plain exposition format).

    Prometheus by default scrapes /metrics. This endpoint is intentionally
    unauthenticated for scrapers; restrict via network policy in production.
    """
    from agent_system.observability.metrics import get_metrics_registry
    return PlainTextResponse(
        content=get_metrics_registry().render(),
        media_type="text/plain; version=0.0.4",
    )
