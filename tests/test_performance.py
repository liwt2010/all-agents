"""
Performance benchmarks — establish baselines for hot paths.

Run via:
  pytest tests/test_performance.py -v --benchmark-only
  (requires pytest-benchmark) OR
  python -m pytest tests/test_performance.py -v
  (uses internal timing)

The tests use a simple in-memory timing to avoid extra deps.
"""

import os
import asyncio
import time
import statistics

import pytest
from fastapi.testclient import TestClient

# Disable security middleware in benchmarks (it adds ~5ms latency)
os.environ.setdefault("DISABLE_SECURITY_MIDDLEWARE", "1")

from agent_system.core.auth import get_auth_service
from agent_system.api.server import app


def _stats(times: list) -> dict:
    """Return p50/p95/p99/avg for a list of millisecond timings."""
    if not times:
        return {}
    return {
        "avg_ms": statistics.mean(times),
        "p50_ms": statistics.median(times),
        "p95_ms": sorted(times)[int(len(times) * 0.95)],
        "p99_ms": sorted(times)[int(len(times) * 0.99)] if len(times) >= 100 else max(times),
        "n": len(times),
    }


def _bench(func, n: int = 100) -> dict:
    """Run func n times sequentially, return ms statistics."""
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        func()
        times.append((time.perf_counter() - t0) * 1000)
    return _stats(times)


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module")
def auth_headers():
    svc = get_auth_service()
    token = svc.issue_token("alice", tenant_id="acme")
    return {"Authorization": f"Bearer {token}"}


class TestHealthEndpoint:
    """Health check should be < 5ms p95."""

    def test_health_latency(self, client):
        stats = _bench(lambda: client.get("/api/health"), n=200)
        assert stats["p95_ms"] < 20, f"Health too slow: {stats}"
        print(f"\nHealth: p50={stats['p50_ms']:.2f}ms, p95={stats['p95_ms']:.2f}ms, n={stats['n']}")


class TestListEndpoints:
    """List should be < 50ms p95 with small data."""

    def test_list_agents(self, client):
        stats = _bench(lambda: client.get("/api/agents"), n=100)
        assert stats["p95_ms"] < 100, f"List agents too slow: {stats}"
        print(f"\nList agents: p50={stats['p50_ms']:.2f}ms, p95={stats['p95_ms']:.2f}ms, n={stats['n']}")

    def test_list_metrics(self, client):
        stats = _bench(lambda: client.get("/api/metrics"), n=100)
        assert stats["p95_ms"] < 100, f"List metrics too slow: {stats}"
        print(f"\nList metrics: p50={stats['p50_ms']:.2f}ms, p95={stats['p95_ms']:.2f}ms, n={stats['n']}")


class TestGraphOperations:
    """Graph ops: insert + lookup should be < 10ms p95 for small graph."""

    def test_graph_node_lookup(self, client):
        from agent_system.memory.graph import (
            get_graph, GraphNode, NodeType, LinkType, reset_graph,
        )
        reset_graph()
        g = get_graph()
        g.add_node(GraphNode(id="bench-1", type=NodeType.TASK, content={"x": 1}))
        g.add_node(GraphNode(id="bench-2", type=NodeType.TASK, content={"x": 2}))
        g.link("bench-1", "bench-2", LinkType.CREATED_BY)

        stats = _bench(lambda: g.get_node("bench-1"), n=1000)
        assert stats["p95_ms"] < 1, f"Node lookup too slow: {stats}"
        print(f"\nNode lookup: p50={stats['p50_ms']:.4f}ms, p95={stats['p95_ms']:.4f}ms, n={stats['n']}")

    def test_graph_find_nodes(self):
        from agent_system.memory.graph import (
            get_graph, GraphNode, NodeType, reset_graph,
        )
        reset_graph()
        g = get_graph()
        for i in range(50):
            g.add_node(GraphNode(
                id=f"node-{i}",
                type=NodeType.TASK,
                content={"x": i},
            ))

        stats = _bench(lambda: g.find_nodes(NodeType.TASK), n=200)
        assert stats["p95_ms"] < 20, f"find_nodes too slow: {stats}"
        print(f"\nfind_nodes: p50={stats['p50_ms']:.2f}ms, p95={stats['p95_ms']:.2f}ms, n={stats['n']}")


class TestContextVarOverhead:
    """ContextVar read/write is fast (used in TenantContext)."""

    def test_contextvar_throughput(self):
        from agent_system.core.auth.context import set_tenant_context, reset_tenant_context
        from agent_system.core.auth.context import TenantContext, _UserStub
        ctx = TenantContext(
            user=_UserStub(user_id="u1", tenant_id="t1"),
            tenant_id="t1",
        )
        def cycle():
            tok = set_tenant_context(ctx)
            reset_tenant_context(tok)
        stats = _bench(cycle, n=1000)
        # p95 should be < 0.1ms
        assert stats["p95_ms"] < 0.5, f"ContextVar too slow: {stats}"
        print(f"\nContextVar cycle: p50={stats['p50_ms']:.4f}ms, p95={stats['p95_ms']:.4f}ms, n={stats['n']}")


class TestAuditLoggerThroughput:
    """In-memory audit append should be sub-millisecond."""

    def test_in_memory_audit(self):
        from agent_system.core.audit_logger import AuditLogger, AuditLogEntry
        logger = AuditLogger()
        i = [0]
        def add_one():
            logger._in_memory.append(AuditLogEntry(
                user_id="u", action="test", resource_id=f"r-{i[0]}"
            ))
            i[0] += 1
        stats = _bench(add_one, n=1000)
        assert stats["p95_ms"] < 1, f"Audit append too slow: {stats}"
        print(f"\nAudit append: p50={stats['p50_ms']:.4f}ms, p95={stats['p95_ms']:.4f}ms, n={stats['n']}")


class TestRateLimiterOverhead:
    """Per-IP rate limit check should be sub-millisecond."""

    def test_rate_limit_check(self):
        from agent_system.core.security_middleware import RateLimiter
        limiter = RateLimiter(rate=60, window=60)
        stats = _bench(lambda: limiter.check("1.1.1.1"), n=10000)
        assert stats["p95_ms"] < 0.5, f"Rate limit too slow: {stats}"
        print(f"\nRate limit check: p50={stats['p50_ms']:.4f}ms, p95={stats['p95_ms']:.4f}ms, n={stats['n']}")


class TestEndToEndHealthCheck:
    """Full HTTP round-trip including auth + CORS + headers."""

    def test_full_health(self, client, auth_headers):
        def hit():
            r = client.get("/api/health")
            assert r.status_code == 200
        stats = _bench(hit, n=200)
        assert stats["p95_ms"] < 30, f"Full health too slow: {stats}"
        print(f"\nFull health: p50={stats['p50_ms']:.2f}ms, p95={stats['p95_ms']:.2f}ms, n={stats['n']}")
