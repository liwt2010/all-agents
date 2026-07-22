"""
OpenTelemetry exporter — bridges our in-memory Tracer (observability/tracing.py)
to a real OTel collector via OTLP HTTP.

Three modes, selected by env vars at first init:
  1. DISABLED  (default) — in-memory only. No exporter. No network. Zero overhead.
  2. CONSOLE             — print spans to stderr. For local debugging.
  3. OTLP_HTTP           — export to an OTel collector (Jaeger, Tempo, SigNoz, etc.)
                            via OTLP/HTTP. Endpoint from OTEL_EXPORTER_OTLP_ENDPOINT
                            (default http://localhost:4318).

The bridge is one-way: our internal `Span` -> OTel `ReadableSpan`.
We do NOT replace the in-process Tracer with the OTel SDK; the in-process
Spans are still the source of truth (so existing tracing.py tests keep
working). This module ADDS an exporter sink on top.

Usage in production:
    from agent_system.observability.otel_exporter import init_otel_exporter
    init_otel_exporter()  # one-time at app startup; no-op if disabled
    # every span.end() will now be exported (if enabled)

Env vars:
    AGENT_OTEL_ENABLED=true|false  (default false)
    AGENT_OTEL_EXPORTER=otlp_http|console  (default otlp_http when enabled)
    AGENT_OTEL_SERVICE_NAME=agent-system  (default)
    AGENT_OTEL_SAMPLE_RATE=1.0  (default; 0.0-1.0)
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318  (default)
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_initialized = False
_init_lock = threading.Lock()
_enabled = False
_exporter_kind: str = "none"  # none | console | otlp_http
_service_name: str = "agent-system"


# ── Public init ──────────────────────────────────────────────────────


def init_otel_exporter(
    enabled: bool | None = None,
    exporter: str | None = None,
    service_name: str | None = None,
    sample_rate: float | None = None,
    endpoint: str | None = None,
) -> bool:
    """
    Initialize the OTel exporter. Idempotent. Returns True if enabled.

    Reads env vars by default; explicit args override.
    Safe to call multiple times — only the first call has effect.
    """
    global _initialized, _enabled, _exporter_kind, _service_name

    with _init_lock:
        if _initialized:
            return _enabled

        enabled_env = (enabled if enabled is not None
                       else _truthy(os.environ.get("AGENT_OTEL_ENABLED", "false")))
        if not enabled_env:
            _initialized = True
            _enabled = False
            _exporter_kind = "none"
            logger.info("OTel exporter: disabled (AGENT_OTEL_ENABLED=false)")
            return False

        _enabled = True
        _exporter_kind = (exporter or os.environ.get("AGENT_OTEL_EXPORTER", "otlp_http")).strip().lower()
        _service_name = (service_name or os.environ.get("AGENT_OTEL_SERVICE_NAME", "agent-system")).strip()

        # Validate kind
        if _exporter_kind not in ("console", "otlp_http"):
            logger.warning(
                "Unknown OTel exporter kind %r, falling back to 'console'",
                _exporter_kind,
            )
            _exporter_kind = "console"

        if _exporter_kind == "otlp_http":
            _init_otlp_http_exporter(endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"))
        else:
            _init_console_exporter()

        sample = sample_rate if sample_rate is not None else float(os.environ.get("AGENT_OTEL_SAMPLE_RATE", "1.0"))
        _configure_sampler(sample)

        _initialized = True
        logger.info(
            "OTel exporter: enabled (kind=%s, service=%s, endpoint=%s)",
            _exporter_kind, _service_name,
            os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
        )
        return True


def is_enabled() -> bool:
    return _enabled


def get_exporter_kind() -> str:
    return _exporter_kind


def get_service_name() -> str:
    return _service_name


def shutdown_otel_exporter(timeout_seconds: float = 5.0) -> bool:
    """
    Flush + shutdown the OTel exporter. Returns True if anything was flushed.
    Production: call from FastAPI lifespan shutdown.
    """
    global _initialized, _enabled
    if not _enabled:
        _initialized = False
        return False
    try:
        from opentelemetry import trace
        provider = trace.get_tracer_provider()
        shutdown = getattr(provider, "shutdown", None)
        if callable(shutdown):
            shutdown()
            logger.info("OTel exporter: shut down cleanly")
    except Exception as e:
        logger.warning("OTel exporter shutdown error: %s", e)
    _initialized = False
    _enabled = False
    return True


def instrument_fastapi(app: Any) -> bool:
    """
    Enable FastAPI auto-instrumentation for per-route span granularity.

    Requires `opentelemetry-instrumentation-fastapi` to be installed.
    When enabled, every request creates a span named after the matched
    route (e.g. `POST /api/tasks`) — much richer than our custom
    middleware's single-span-per-request approach.

    Idempotent: returns False (and logs) if the package isn't installed
    or the app was already instrumented.

    Use:
        init_otel_exporter()           # first
        instrument_fastapi(app)        # then — needs an active TracerProvider
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logger.info(
            "FastAPI auto-instrumentation unavailable — "
            "install opentelemetry-instrumentation-fastapi to enable."
        )
        return False

    if getattr(app, "_is_instrumented_by_opentelemetry", False):
        logger.debug("FastAPI app already instrumented; skipping")
        return True

    try:
        FastAPIInstrumentor.instrument_app(app)
        logger.info("FastAPI auto-instrumentation enabled (per-route spans)")
        return True
    except Exception as e:
        logger.warning(f"FastAPI auto-instrumentation failed: {e}")
        return False


# ── Internal: OTel SDK init ──────────────────────────────────────────


def _truthy(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "on", "enabled")


def _init_console_exporter() -> None:
    """Set up OTel SDK with ConsoleSpanExporter (prints to stderr)."""
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
        )

        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(provider)
    except Exception as e:
        logger.warning("OTel ConsoleSpanExporter init failed: %s", e)


def _init_otlp_http_exporter(endpoint: str) -> None:
    """Set up OTel SDK with OTLP/HTTP SpanExporter (to collector)."""
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        # OTLPSpanExporter reads OTEL_EXPORTER_OTLP_ENDPOINT env var natively
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
    except Exception as e:
        logger.warning("OTel OTLPSpanExporter init failed (endpoint=%s): %s", endpoint, e)


def _configure_sampler(sample_rate: float) -> None:
    """Apply sampling rate to the provider. 1.0 = always, 0.0 = never."""
    if not (0.0 <= sample_rate <= 1.0):
        logger.warning("Invalid sample_rate %s, ignoring", sample_rate)
        return
    try:
        from opentelemetry import trace
        provider = trace.get_tracer_provider()
        sampler = getattr(provider, "_sampler", None)
        # Re-instantiate provider with sampler if non-default
        if sample_rate < 1.0 and sampler is None:
            from opentelemetry.sdk.trace import TracerProvider as TP
            from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
            new_provider = TP(sampler=TraceIdRatioBased(sample_rate))
            # Re-attach processors
            for proc in getattr(provider, "_active_span_processor", None) and provider._active_span_processor._span_processors or []:
                new_provider.add_span_processor(proc)
            trace.set_tracer_provider(new_provider)
            logger.info("OTel sampler: %.2f", sample_rate)
    except Exception as e:
        logger.debug("OTel sampler config skipped: %s", e)


# ── Public span helper ───────────────────────────────────────────────


def get_otel_tracer(name: str = "agent_system"):
    """
    Return an OTel tracer (for instrumenting code that wants to emit OTel spans
    directly, e.g. FastAPI middleware, agent execute()).

    If OTel is disabled, returns a NoOp tracer (zero overhead).
    """
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except Exception:
        # Return a no-op shim that mimics the API surface
        return _NoOpTracer()


def force_flush(timeout_seconds: float = 5.0) -> bool:
    """Force flush the batch span processor (for tests)."""
    if not _enabled:
        return False
    try:
        from opentelemetry import trace
        provider = trace.get_tracer_provider()
        flush = getattr(provider, "force_flush", None)
        if callable(flush):
            flush(timeout_millis=int(timeout_seconds * 1000))
            return True
    except Exception as e:
        logger.debug("OTel force_flush error: %s", e)
    return False


class _NoOpTracer:
    """Stand-in for opentelemetry.trace.Tracer when SDK is unavailable."""
    def start_as_current_span(self, name, **kw):
        return _NoOpSpan()

    def start_span(self, name, **kw):
        return _NoOpSpan()


class _NoOpSpan:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def set_attribute(self, k, v): pass
    def set_status(self, *a, **kw): pass
    def record_exception(self, e): pass
    def end(self): pass
