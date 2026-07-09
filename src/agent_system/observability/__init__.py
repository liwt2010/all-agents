"""Observability subpackage — Tracing + Prometheus metrics (PLATFORM §5.5)."""
from agent_system.observability.tracing import (
    Span, Tracer, get_tracer, reset_tracer,
)
from agent_system.observability.metrics import (
    Counter, Gauge, Histogram, MetricsRegistry,
    get_metrics_registry, reset_metrics_registry,
)
from agent_system.observability.instrumentation import (
    track_task, track_llm, track_storage,
    update_memory_node_gauge,
    HTTP_REQUESTS_TOTAL, HTTP_REQUEST_DURATION,
    TASKS_TOTAL, TASK_DURATION,
    LLM_REQUESTS_TOTAL, LLM_TOKENS_TOTAL, LLM_REQUEST_DURATION,
    STORAGE_OPS_TOTAL, STORAGE_OP_DURATION,
    ACTIVE_TASKS, MEMORY_NODES_TOTAL,
)
from agent_system.observability.otel_exporter import (
    init_otel_exporter, shutdown_otel_exporter, is_enabled as otel_is_enabled,
    get_exporter_kind, get_service_name, get_otel_tracer, force_flush,
)

__all__ = [
    "Span", "Tracer", "get_tracer", "reset_tracer",
    "Counter", "Gauge", "Histogram", "MetricsRegistry",
    "get_metrics_registry", "reset_metrics_registry",
    "track_task", "track_llm", "track_storage",
    "update_memory_node_gauge",
    # metric name constants
    "HTTP_REQUESTS_TOTAL", "HTTP_REQUEST_DURATION",
    "TASKS_TOTAL", "TASK_DURATION",
    "LLM_REQUESTS_TOTAL", "LLM_TOKENS_TOTAL", "LLM_REQUEST_DURATION",
    "STORAGE_OPS_TOTAL", "STORAGE_OP_DURATION",
    "ACTIVE_TASKS", "MEMORY_NODES_TOTAL",
    # OTel exporter (PR-14)
    "init_otel_exporter", "shutdown_otel_exporter", "otel_is_enabled",
    "get_exporter_kind", "get_service_name", "get_otel_tracer", "force_flush",
]