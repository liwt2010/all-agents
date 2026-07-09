"""
PR-14 OTel exporter tests.

Verifies:
  1. init_otel_exporter is a safe no-op when AGENT_OTEL_ENABLED=false
  2. init_otel_exporter can be enabled with console exporter
  3. The OTel tracer is wired into SmartAgent.execute() — success emits OK status
  4. The OTel tracer is wired into SmartAgent.execute() — error emits ERROR status
  5. The FastAPI middleware emits an OTel span per request
  6. force_flush works and shutdown_otel_exporter is idempotent
  7. Disabled OTel returns a no-op tracer (no exceptions)
"""
import asyncio
import os
import sys

import pytest


# ── Helpers ──────────────────────────────────────────────────────────


def _reset_otel_module():
    """Reset module-level state so each test gets a clean init."""
    import agent_system.observability.otel_exporter as mod
    mod._initialized = False
    mod._enabled = False
    mod._exporter_kind = "none"
    mod._service_name = "agent-system"


# ── 1. Disabled by default ───────────────────────────────────────────


def test_disabled_by_default_is_safe_noop():
    """Without env vars, init_otel_exporter() returns False and does nothing."""
    os.environ.pop("AGENT_OTEL_ENABLED", None)
    _reset_otel_module()
    from agent_system.observability.otel_exporter import (
        init_otel_exporter, is_enabled, get_exporter_kind, get_otel_tracer,
    )
    assert init_otel_exporter() is False
    assert is_enabled() is False
    assert get_exporter_kind() == "none"
    # Tracer should be no-op
    tracer = get_otel_tracer("test")
    with tracer.start_as_current_span("noop") as span:
        span.set_attribute("foo", "bar")  # no-op, no exception


# ── 2. Console exporter init ────────────────────────────────────────


def test_console_exporter_init():
    """Enabling with console exporter initializes the OTel SDK."""
    _reset_otel_module()
    from agent_system.observability.otel_exporter import (
        init_otel_exporter, is_enabled, get_exporter_kind, get_service_name,
        force_flush, shutdown_otel_exporter,
    )
    assert init_otel_exporter(
        enabled=True, exporter="console", service_name="test-svc"
    ) is True
    assert is_enabled() is True
    assert get_exporter_kind() == "console"
    assert get_service_name() == "test-svc"
    # Tracer is a real OTel tracer
    from opentelemetry import trace
    tracer = trace.get_tracer("test")
    assert tracer is not None
    # Clean up
    force_flush()
    assert shutdown_otel_exporter() is True
    assert is_enabled() is False
    # Idempotent shutdown
    assert shutdown_otel_exporter() is False


# ── 3. Idempotent init ──────────────────────────────────────────────


def test_init_otel_exporter_is_idempotent():
    """Calling init twice only takes effect once."""
    _reset_otel_module()
    from agent_system.observability.otel_exporter import (
        init_otel_exporter, is_enabled, shutdown_otel_exporter,
    )
    init_otel_exporter(enabled=True, exporter="console", service_name="svc1")
    # Second call should be a no-op (returns existing state)
    init_otel_exporter(enabled=True, exporter="console", service_name="svc2")
    from agent_system.observability.otel_exporter import get_service_name
    assert get_service_name() == "svc1", "Second init should not change service name"
    shutdown_otel_exporter()


# ── 4. Agent execute() emits OTel span ─────────────────────────────


@pytest.mark.asyncio
async def test_agent_execute_emits_otel_span_on_success():
    """SmartAgent.execute() creates an OTel span with success status when OTel enabled."""
    _reset_otel_module()
    from agent_system.observability.otel_exporter import (
        init_otel_exporter, is_enabled, shutdown_otel_exporter,
    )
    init_otel_exporter(enabled=True, exporter="console", service_name="agent-svc")
    assert is_enabled()

    # Attach an in-memory exporter to the existing provider (OTel disallows
    # replacing the global provider once set, so we add a processor).
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry import trace
    inmem = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    provider.add_span_processor(SimpleSpanProcessor(inmem))

    from agent_system.agents.product_agent import ProductAgent
    from agent_system.core.agent import TaskContext

    agent = ProductAgent()
    agent.memory_enabled = False  # keep test focused on OTel
    ctx = TaskContext(task_id="otel-1", input="一句话:1+1=?")

    # Stub do_work to avoid the LLM call (Pydantic v2 requires object.__setattr__)
    async def fake_do_work(task):
        from agent_system.core.schema import OutputSchema
        return OutputSchema(
            id="x", type="product", schema_version="1.0",
            payload={"result": "stubbed"},
        )
    object.__setattr__(agent, "do_work", fake_do_work)

    result = await agent.execute(ctx)
    assert result is not None

    # Force flush + check
    provider.force_flush()
    spans = inmem.get_finished_spans()
    execute_span = next((s for s in spans if s.name == "agent.execute"), None)
    assert execute_span is not None, f"Expected 'agent.execute' span, got: {[s.name for s in spans]}"
    assert execute_span.attributes.get("agent.name") == "product_agent"
    assert execute_span.attributes.get("agent.execute.status") == "ok"

    shutdown_otel_exporter()


@pytest.mark.asyncio
async def test_agent_execute_emits_otel_span_on_error():
    """On failure, the OTel span gets status=error and the exception recorded."""
    _reset_otel_module()
    from agent_system.observability.otel_exporter import init_otel_exporter, shutdown_otel_exporter

    init_otel_exporter(enabled=True, exporter="console", service_name="agent-svc")

    # Attach in-memory exporter
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry import trace
    inmem = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    provider.add_span_processor(SimpleSpanProcessor(inmem))

    from agent_system.agents.product_agent import ProductAgent
    from agent_system.core.agent import TaskContext

    agent = ProductAgent()
    agent.memory_enabled = False

    # Stub do_work to raise (Pydantic v2 requires object.__setattr__)
    async def fail_do_work(task):
        raise RuntimeError("simulated LLM hallucination")
    object.__setattr__(agent, "do_work", fail_do_work)

    ctx = TaskContext(task_id="otel-err-1", input="1+1=?")

    raised = False
    try:
        await agent.execute(ctx)
    except RuntimeError:
        raised = True
    assert raised, "Expected the failing agent to raise"

    provider.force_flush()
    spans = inmem.get_finished_spans()
    execute_span = next((s for s in spans if s.name == "agent.execute"), None)
    assert execute_span is not None
    assert execute_span.attributes.get("agent.execute.status") == "error"
    # OTel records the exception as an event
    assert len(execute_span.events) >= 1

    shutdown_otel_exporter()


# ── 5. FastAPI middleware ────────────────────────────────────────────


def test_otel_middleware_disabled_is_passthrough():
    """When OTel is disabled, the middleware is a pure pass-through."""
    _reset_otel_module()
    from agent_system.observability.otel_middleware import OTelMiddleware

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = OTelMiddleware(app)
    sent = []

    async def send_out(msg):
        sent.append(msg)

    asyncio.run(mw({"type": "http", "method": "GET", "path": "/"}, None, send_out))
    assert len(sent) == 2
    assert sent[0]["status"] == 200


def test_otel_middleware_emits_span_when_enabled():
    """When OTel is enabled, each request gets a span."""
    _reset_otel_module()
    from agent_system.observability.otel_exporter import init_otel_exporter, shutdown_otel_exporter
    from agent_system.observability.otel_middleware import OTelMiddleware

    init_otel_exporter(enabled=True, exporter="console", service_name="api-svc")

    # Attach an in-memory exporter to the global provider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry import trace
    inmem = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    provider.add_span_processor(SimpleSpanProcessor(inmem))

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"hi"})

    mw = OTelMiddleware(app)
    sent = []

    async def send_out(msg):
        sent.append(msg)

    asyncio.run(mw(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/agents/run",
            "scheme": "http",
            "headers": [],
        },
        None, send_out,
    ))
    assert sent[0]["status"] == 200

    provider.force_flush()
    spans = inmem.get_finished_spans()
    http_spans = [s for s in spans if s.name.startswith("http.")]
    assert len(http_spans) >= 1
    s = http_spans[0]
    assert s.attributes.get("http.method") == "POST"
    assert s.attributes.get("http.status_code") == 200

    shutdown_otel_exporter()


# ── 6. Disabled tracer is safe ──────────────────────────────────────


def test_disabled_otel_tracer_is_noop():
    """When OTel is disabled, the tracer is a no-op shim (no exceptions)."""
    _reset_otel_module()
    from agent_system.observability.otel_exporter import get_otel_tracer
    tracer = get_otel_tracer("test")
    # These should not raise
    span = tracer.start_span("test")
    with tracer.start_as_current_span("ctx-test") as s:
        s.set_attribute("k", "v")
        s.set_status("ok")
        s.record_exception(ValueError("x"))
    span.end()


# ── 7. force_flush is idempotent and safe ───────────────────────────


def test_force_flush_disabled_returns_false():
    _reset_otel_module()
    from agent_system.observability.otel_exporter import force_flush
    assert force_flush() is False  # disabled -> False
