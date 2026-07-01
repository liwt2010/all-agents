"""
Tests: Observability (Tracing + Prometheus metrics)
"""

import pytest
import asyncio
import time

from agent_system.observability import (
    Tracer, get_tracer, reset_tracer,
    MetricsRegistry, Counter, Gauge, Histogram,
    get_metrics_registry, reset_metrics_registry,
)


# ── Tracing ──

class TestTracer:
    def setup_method(self):
        reset_tracer()

    def test_sync_span(self):
        t = Tracer()
        with t.start_span("op1") as span:
            assert span.name == "op1"
            time.sleep(0.001)
        assert len(t._spans) == 1
        assert t._spans[0].duration_ms > 0

    def test_nested_spans(self):
        t = Tracer()
        with t.start_span("outer") as outer:
            with t.start_span("inner") as inner:
                assert inner.parent_id == outer.span_id
                assert inner.trace_id == outer.trace_id
        assert len(t._spans) == 2

    def test_error_marks_span(self):
        t = Tracer()
        try:
            with t.start_span("failing") as span:
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert t._spans[0].status == "error"
        assert t._spans[0].attributes.get("error.message") == "boom"

    def test_max_spans_eviction(self):
        t = Tracer()
        t._max_spans = 5
        for i in range(20):
            with t.start_span(f"op-{i}"):
                pass
        assert len(t._spans) == 5

    @pytest.mark.asyncio
    async def test_async_span(self):
        t = Tracer()
        async with t.astart_span("async-op") as span:
            await asyncio.sleep(0.001)
        assert t._spans[0].name == "async-op"

    def test_get_spans_by_trace(self):
        t = Tracer()
        with t.start_span("a"):
            with t.start_span("b"):
                pass
        # 2 spans, both share a trace id
        assert len(t.get_spans_by_trace(t._spans[0].trace_id)) == 2

    def test_stats(self):
        t = Tracer()
        with t.start_span("op1"):
            pass
        s = t.stats()
        assert s["total_spans"] == 1
        assert s["active_spans"] == 0
        assert s["avg_duration_ms"] >= 0

    def test_singleton(self):
        a = get_tracer()
        b = get_tracer()
        assert a is b


# ── Metrics ──

class TestCounter:
    def test_basic_inc(self):
        c = Counter("ops", "Number of operations")
        c.inc()
        c.inc()
        c.inc(5)
        lines = c.render()
        # Should have 1 line with value 7
        value_lines = [l for l in lines if l.startswith("ops ") or l.startswith("ops{")]
        assert any("7" in l for l in value_lines)

    def test_with_labels(self):
        c = Counter("reqs", "Requests", label_names=["method"])
        c.inc(1, method="GET")
        c.inc(2, method="POST")
        lines = c.render()
        assert any('method="GET"' in l for l in lines)
        assert any('method="POST"' in l for l in lines)


class TestGauge:
    def test_set(self):
        g = Gauge("temp", "Temperature")
        g.set(20.5)
        lines = g.render()
        assert any("20.5" in l for l in lines)

    def test_inc_dec(self):
        g = Gauge("active", "Active")
        g.set(10)
        g.inc()
        g.dec(2)
        lines = g.render()
        assert any("9" in l for l in lines)


class TestHistogram:
    def test_observe_buckets(self):
        h = Histogram("lat", "Latency", buckets=[0.1, 0.5, 1.0])
        h.observe(0.05)
        h.observe(0.3)
        h.observe(2.0)
        lines = h.render()
        # Should have bucket lines
        assert any("_bucket" in l for l in lines)
        assert any("_sum" in l for l in lines)
        assert any("_count" in l for l in lines)


class TestRegistry:
    def test_register_and_render(self):
        r = MetricsRegistry()
        c = r.counter("c1", "First counter")
        c.inc(3)
        g = r.gauge("g1", "A gauge")
        g.set(42)
        h = r.histogram("h1", "A histogram")
        h.observe(0.5)
        text = r.render()
        assert "c1" in text
        assert "g1" in text
        assert "h1" in text

    def test_cached_on_name(self):
        r = MetricsRegistry()
        c1 = r.counter("same", "x")
        c2 = r.counter("same", "x")
        assert c1 is c2

    def test_singleton(self):
        a = get_metrics_registry()
        b = get_metrics_registry()
        assert a is b
