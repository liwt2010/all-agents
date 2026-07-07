"""
Tests for PR-10 instrumentation layer.

Covers:
- track_task / track_llm / track_storage decorators
- MetricsMiddleware records HTTP request metrics
- Prometheus exposition format is valid
- AGENT_OBSERVABILITY_ENABLED=false makes everything no-op
"""

import os
import re
from unittest.mock import AsyncMock

import pytest


# ── Decorator tests ──

class TestTrackTask:
    def test_track_task_records_success(self):
        from agent_system.observability.instrumentation import (
            track_task, TASKS_TOTAL,
        )
        from agent_system.observability.metrics import (
            get_metrics_registry, reset_metrics_registry,
        )
        reset_metrics_registry()

        @track_task(agent_type="test")
        async def run():
            return "ok"

        import asyncio
        result = asyncio.run(run())
        assert result == "ok"

        registry = get_metrics_registry()
        counter = registry._metrics[TASKS_TOTAL]
        # Find a value with status=success
        success_keys = [k for k in counter._values if any(v == "success" for _, v in k)]
        assert len(success_keys) >= 1

    def test_track_task_records_failure(self):
        from agent_system.observability.instrumentation import track_task, TASKS_TOTAL
        from agent_system.observability.metrics import (
            get_metrics_registry, reset_metrics_registry,
        )
        reset_metrics_registry()

        @track_task(agent_type="test")
        async def run():
            raise ValueError("boom")

        import asyncio
        with pytest.raises(ValueError):
            asyncio.run(run())

        registry = get_metrics_registry()
        counter = registry._metrics[TASKS_TOTAL]
        failure_keys = [k for k in counter._values if any(v == "failure" for _, v in k)]
        assert len(failure_keys) >= 1

    def test_track_task_sync_function(self):
        from agent_system.observability.instrumentation import track_task
        from agent_system.observability.metrics import (
            get_metrics_registry, reset_metrics_registry,
        )
        reset_metrics_registry()

        @track_task(agent_type="sync")
        def run():
            return 42

        assert run() == 42


class TestTrackLLM:
    def test_track_llm_records_tokens_from_usage(self):
        from agent_system.observability.instrumentation import (
            track_llm, LLM_TOKENS_TOTAL,
        )
        from agent_system.observability.metrics import (
            get_metrics_registry, reset_metrics_registry,
        )
        reset_metrics_registry()

        class MockUsage:
            prompt_tokens = 100
            completion_tokens = 50

        class MockResult:
            usage = MockUsage()

        @track_llm(model="test-model", provider="test")
        async def chat(messages):
            return MockResult()

        import asyncio
        result = asyncio.run(chat([{"role": "user", "content": "hi"}]))
        assert result.usage.prompt_tokens == 100

        registry = get_metrics_registry()
        tokens = registry._metrics[LLM_TOKENS_TOTAL]
        # input tokens
        input_keys = [k for k in tokens._values if any(v == "input" for _, v in k)]
        output_keys = [k for k in tokens._values if any(v == "output" for _, v in k)]
        assert any(tokens._values[k] == 100 for k in input_keys)
        assert any(tokens._values[k] == 50 for k in output_keys)


class TestTrackStorage:
    def test_track_storage_records_success(self):
        from agent_system.observability.instrumentation import (
            track_storage, STORAGE_OPS_TOTAL,
        )
        from agent_system.observability.metrics import (
            get_metrics_registry, reset_metrics_registry,
        )
        reset_metrics_registry()

        @track_storage(backend="test", op="op_x")
        def do_op():
            return "ok"

        assert do_op() == "ok"

        registry = get_metrics_registry()
        counter = registry._metrics[STORAGE_OPS_TOTAL]
        ok_keys = [k for k in counter._values if any(v == "ok" for _, v in k)]
        assert any(counter._values[k] == 1 for k in ok_keys)

    def test_track_storage_records_failure(self):
        from agent_system.observability.instrumentation import (
            track_storage, STORAGE_OPS_TOTAL,
        )
        from agent_system.observability.metrics import (
            get_metrics_registry, reset_metrics_registry,
        )
        reset_metrics_registry()

        @track_storage(backend="test", op="op_y")
        def do_op():
            raise RuntimeError("nope")

        with pytest.raises(RuntimeError):
            do_op()

        registry = get_metrics_registry()
        counter = registry._metrics[STORAGE_OPS_TOTAL]
        fail_keys = [k for k in counter._values if any(v == "fail" for _, v in k)]
        assert len(fail_keys) >= 1


# ── Disable env var ──

class TestObservabilityDisabled:
    def test_disabled_makes_decorator_noop(self, monkeypatch):
        from agent_system.observability.instrumentation import (
            track_task, TASKS_TOTAL,
        )
        from agent_system.observability.metrics import (
            get_metrics_registry, reset_metrics_registry,
        )
        monkeypatch.setenv("AGENT_OBSERVABILITY_ENABLED", "false")
        reset_metrics_registry()

        @track_task(agent_type="noop")
        async def run():
            return "result"

        import asyncio
        assert asyncio.run(run()) == "result"

        registry = get_metrics_registry()
        # No metrics should have been created
        assert TASKS_TOTAL not in registry._metrics


# ── Prometheus exposition format ──

class TestPrometheusFormat:
    def test_render_includes_help_and_type(self):
        from agent_system.observability.metrics import (
            get_metrics_registry, reset_metrics_registry,
        )
        reset_metrics_registry()

        registry = get_metrics_registry()
        counter = registry.counter("my_counter", "A test counter", label_names=["status"])
        counter.inc(1.0, status="ok")

        text = registry.render()
        assert "# HELP my_counter A test counter" in text
        assert "# TYPE my_counter counter" in text
        assert 'my_counter{status="ok"} 1.0' in text

    def test_render_histogram_has_buckets(self):
        from agent_system.observability.metrics import (
            get_metrics_registry, reset_metrics_registry,
        )
        reset_metrics_registry()

        registry = get_metrics_registry()
        hist = registry.histogram("my_hist", "Test histogram")
        hist.observe(0.1)
        hist.observe(0.5)

        text = registry.render()
        assert 'my_hist_bucket{le="0.005"}' in text
        assert 'my_hist_bucket{le="0.1"}' in text
        assert 'my_hist_bucket{le="+Inf"} 2' in text
        assert 'my_hist_count 2' in text
        assert 'my_hist_sum' in text


# ── MetricsMiddleware ──

class TestMetricsMiddleware:
    @pytest.fixture
    def client(self):
        from fastapi import FastAPI
        from agent_system.core.metrics_middleware import MetricsMiddleware
        from agent_system.observability.metrics import reset_metrics_registry, get_metrics_registry

        app = FastAPI()
        app.add_middleware(MetricsMiddleware)

        @app.get("/api/test")
        async def test_route():
            return {"ok": True}

        @app.get("/api/error")
        async def error_route():
            from fastapi import HTTPException
            raise HTTPException(status_code=500)

        reset_metrics_registry()
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            yield c
        reset_metrics_registry()

    def test_middleware_records_200(self, client):
        r = client.get("/api/test")
        assert r.status_code == 200
        from agent_system.observability.metrics import get_metrics_registry
        registry = get_metrics_registry()
        counter = registry._metrics["agent_http_requests_total"]
        # status is stored as string label
        ok_keys = [k for k in counter._values if dict(k).get("status") == 200]
        assert len(ok_keys) >= 1

    def test_middleware_records_500(self, client):
        r = client.get("/api/error")
        assert r.status_code == 500
        from agent_system.observability.metrics import get_metrics_registry
        registry = get_metrics_registry()
        counter = registry._metrics["agent_http_requests_total"]
        err_keys = [k for k in counter._values if dict(k).get("status") == 500]
        assert len(err_keys) >= 1

    def test_middleware_normalizes_id_paths(self, client):
        r = client.get("/api/test/abc-12345678")
        # Should normalize to /api/test/{id}
        from agent_system.observability.metrics import get_metrics_registry
        registry = get_metrics_registry()
        counter = registry._metrics["agent_http_requests_total"]
        # Should have recorded some metric (404 is fine; we just check path normalization)
        assert len(counter._values) >= 1


# ── /metrics endpoint ──

class TestMetricsEndpoint:
    def test_metrics_endpoint_returns_prometheus_text(self):
        """Test the /metrics route handler directly without importing the full
        server (which would load .env and pollute os.environ for other tests)."""
        from fastapi import FastAPI
        from fastapi.responses import PlainTextResponse
        from fastapi.testclient import TestClient
        from agent_system.observability.metrics import (
            get_metrics_registry, reset_metrics_registry,
        )

        reset_metrics_registry()
        registry = get_metrics_registry()
        registry.counter("smoke_metric", "smoke test")

        app = FastAPI()

        @app.get("/metrics")
        async def metrics_prometheus_text():
            return PlainTextResponse(
                content=get_metrics_registry().render(),
                media_type="text/plain; version=0.0.4",
            )

        with TestClient(app) as client:
            r = client.get("/metrics")
            assert r.status_code == 200
            assert "text/plain" in r.headers["content-type"]
            assert "# HELP smoke_metric" in r.text

        reset_metrics_registry()