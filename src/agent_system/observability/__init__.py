"""Observability subpackage — Tracing + Prometheus metrics (PLATFORM §5.5)."""
from agent_system.observability.tracing import (
    Span, Tracer, get_tracer, reset_tracer,
)
from agent_system.observability.metrics import (
    Counter, Gauge, Histogram, MetricsRegistry,
    get_metrics_registry, reset_metrics_registry,
)

__all__ = [
    "Span", "Tracer", "get_tracer", "reset_tracer",
    "Counter", "Gauge", "Histogram", "MetricsRegistry",
    "get_metrics_registry", "reset_metrics_registry",
]
