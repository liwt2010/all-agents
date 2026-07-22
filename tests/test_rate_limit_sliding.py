"""
Tests for PR-12 per-user / per-scope sliding window rate limiter.

Covers:
- SlidingWindowLimiter: allow/deny/release/cleanup
- Per-user and per-IP isolation
- Scope-based classification (default/expensive/heavy/auth)
- Composed check (user + IP both must pass)
- X-RateLimit-* response headers
- Retry-After header on 429
- Fail-open when limiter errors
- Thread safety under concurrent load
"""

import asyncio
import os
import threading
import time
from unittest.mock import patch

import pytest


# ── SlidingWindowLimiter ──

class TestSlidingWindowBasic:
    def test_allows_under_limit(self):
        from agent_system.core.rate_limit import SlidingWindowLimiter
        lim = SlidingWindowLimiter(limit=5, window_seconds=60)
        for i in range(5):
            d = lim.check("shared-key")
            assert d.allowed is True
            assert d.remaining == 5 - (i + 1)

    def test_denies_over_limit(self):
        from agent_system.core.rate_limit import SlidingWindowLimiter
        lim = SlidingWindowLimiter(limit=3, window_seconds=60)
        for _ in range(3):
            assert lim.check("k").allowed is True
        d = lim.check("k")
        assert d.allowed is False
        assert d.remaining == 0
        assert d.retry_after > 0

    def test_releases_after_window(self):
        from agent_system.core.rate_limit import SlidingWindowLimiter
        lim = SlidingWindowLimiter(limit=2, window_seconds=1.0)
        assert lim.check("k").allowed is True
        assert lim.check("k").allowed is True
        assert lim.check("k").allowed is False
        # Move "now" forward past the window
        future = time.time() + 1.5
        assert lim.check("k", now=future).allowed is True

    def test_per_key_isolation(self):
        from agent_system.core.rate_limit import SlidingWindowLimiter
        lim = SlidingWindowLimiter(limit=2, window_seconds=60)
        assert lim.check("A").allowed is True
        assert lim.check("A").allowed is True
        assert lim.check("A").allowed is False
        # B is independent
        assert lim.check("B").allowed is True
        assert lim.check("B").allowed is True
        assert lim.check("B").allowed is False

    def test_retry_after_decreases_with_time(self):
        from agent_system.core.rate_limit import SlidingWindowLimiter
        lim = SlidingWindowLimiter(limit=1, window_seconds=10.0)
        lim.check("k")
        d1 = lim.check("k")
        assert d1.allowed is False
        # 5 seconds later, retry_after should be ~5
        d2 = lim.check("k", now=time.time() + 5.0)
        assert d2.allowed is False
        assert d2.retry_after < d1.retry_after

    def test_invalid_limit_raises(self):
        from agent_system.core.rate_limit import SlidingWindowLimiter
        with pytest.raises(ValueError):
            SlidingWindowLimiter(limit=0, window_seconds=60)
        with pytest.raises(ValueError):
            SlidingWindowLimiter(limit=60, window_seconds=0)

    def test_reset_specific_key(self):
        from agent_system.core.rate_limit import SlidingWindowLimiter
        lim = SlidingWindowLimiter(limit=1, window_seconds=60)
        lim.check("A")
        assert lim.check("A").allowed is False
        lim.reset("A")
        assert lim.check("A").allowed is True

    def test_reset_all_keys(self):
        from agent_system.core.rate_limit import SlidingWindowLimiter
        lim = SlidingWindowLimiter(limit=1, window_seconds=60)
        lim.check("A")
        lim.check("B")
        lim.reset()
        assert lim.check("A").allowed is True
        assert lim.check("B").allowed is True

    def test_peek_does_not_record(self):
        from agent_system.core.rate_limit import SlidingWindowLimiter
        lim = SlidingWindowLimiter(limit=2, window_seconds=60)
        lim.check("k")
        # Peek twice — should not consume slots
        assert lim.peek("k").allowed is True
        assert lim.peek("k").allowed is True
        # Now actually check — only 1 real slot used; 1 more allowed
        assert lim.check("k").allowed is True
        assert lim.check("k").allowed is False

    def test_concurrent_access_thread_safe(self):
        from agent_system.core.rate_limit import SlidingWindowLimiter
        lim = SlidingWindowLimiter(limit=50, window_seconds=60)
        allowed_count = [0]
        lock = threading.Lock()

        def hit():
            for _ in range(20):
                if lim.check("shared").allowed:
                    with lock:
                        allowed_count[0] += 1

        threads = [threading.Thread(target=hit) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Exactly 50 allowed (5 threads × 20 attempts, only first 50 fit)
        assert allowed_count[0] == 50


# ── Scope classification ──

class TestScopeClassification:
    def test_auth_scope(self):
        from agent_system.core.rate_limit import classify_scope
        assert classify_scope("/api/auth/token") == "auth"
        assert classify_scope("/api/auth/refresh") == "auth"

    def test_heavy_scope(self):
        from agent_system.core.rate_limit import classify_scope
        assert classify_scope("/api/admin/users") == "heavy"
        assert classify_scope("/api/audit/query") == "heavy"

    def test_expensive_scope(self):
        from agent_system.core.rate_limit import classify_scope
        assert classify_scope("/api/tasks") == "expensive"
        assert classify_scope("/api/tasks/abc-123") == "expensive"
        assert classify_scope("/api/discussions") == "expensive"

    def test_default_scope(self):
        from agent_system.core.rate_limit import classify_scope
        assert classify_scope("/api/agents") == "default"
        assert classify_scope("/api/graph/stats") == "default"
        assert classify_scope("/") == "default"


# ── LimiterRegistry (composed check) ──

class TestLimiterRegistry:
    def _check(self, reg, user, ip, scope):
        """Sync wrapper around the now-async check_request."""
        import asyncio
        return asyncio.run(reg.check_request(user, ip, scope))

    def test_user_and_ip_both_must_pass(self):
        from agent_system.core.rate_limit import LimiterRegistry, ScopeConfig
        scopes = {
            "default": ScopeConfig(user_limit=10, ip_limit=2),
        }
        reg = LimiterRegistry(scopes=scopes)
        # 2 IP requests succeed (within IP limit), then IP blocks
        allowed1, _, dim1 = self._check(reg, "user-A", "1.1.1.1", "default")
        allowed2, _, dim2 = self._check(reg, "user-B", "1.1.1.1", "default")
        allowed3, decision3, dim3 = self._check(reg, "user-C", "1.1.1.1", "default")
        assert allowed1 and allowed2
        assert not allowed3
        assert dim3 == "ip"

    def test_per_user_isolation(self):
        from agent_system.core.rate_limit import LimiterRegistry, ScopeConfig
        scopes = {"default": ScopeConfig(user_limit=2, ip_limit=100)}
        reg = LimiterRegistry(scopes=scopes)
        # User A exhausts their quota
        assert self._check(reg, "A", "1.1.1.1", "default")[0]
        assert self._check(reg, "A", "1.1.1.1", "default")[0]
        a_blocked, _, _ = self._check(reg, "A", "1.1.1.1", "default")
        assert a_blocked is False
        # User B is independent
        assert self._check(reg, "B", "1.1.1.1", "default")[0]
        assert self._check(reg, "B", "1.1.1.1", "default")[0]

    def test_anonymous_falls_back_to_ip_only(self):
        from agent_system.core.rate_limit import LimiterRegistry, ScopeConfig
        scopes = {"default": ScopeConfig(user_limit=2, ip_limit=3)}
        reg = LimiterRegistry(scopes=scopes)
        # No user_id — only IP limiter applies
        assert self._check(reg, None, "1.1.1.1", "default")[0]
        assert self._check(reg, None, "1.1.1.1", "default")[0]
        assert self._check(reg, None, "1.1.1.1", "default")[0]
        allowed, _, dim = self._check(reg, None, "1.1.1.1", "default")
        assert allowed is False
        assert dim == "ip"

    def test_env_config_loads(self, monkeypatch):
        monkeypatch.setenv("AGENT_RATE_LIMIT_SCOPE_DEFAULT_USER", "50")
        monkeypatch.setenv("AGENT_RATE_LIMIT_SCOPE_AUTH_USER", "3")
        from agent_system.core.rate_limit import load_scope_config_from_env
        scopes = load_scope_config_from_env()
        assert scopes["default"].user_limit == 50
        assert scopes["auth"].user_limit == 3


# ── Middleware integration ──

class TestRateLimitMiddleware:
    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from agent_system.core.security_middleware import SlidingWindowRateLimitMiddleware
        from agent_system.core.rate_limit import reset_limiter_registry

        reset_limiter_registry()
        monkeypatch.setenv("AGENT_RATE_LIMIT_SCOPE_DEFAULT_USER", "5")
        monkeypatch.setenv("AGENT_RATE_LIMIT_SCOPE_DEFAULT_IP", "100")
        # Reload registry with new env
        reset_limiter_registry()

        app = FastAPI()
        app.add_middleware(SlidingWindowRateLimitMiddleware)

        @app.get("/api/agents")
        async def agents():
            return {"agents": []}

        @app.get("/api/tasks")
        async def tasks():
            return {"tasks": []}

        yield TestClient(app)
        reset_limiter_registry()

    def test_allows_under_limit_attaches_headers(self, client):
        r = client.get("/api/agents")
        assert r.status_code == 200
        assert "X-RateLimit-Limit" in r.headers
        assert "X-RateLimit-Remaining" in r.headers
        assert "X-RateLimit-Scope" in r.headers
        assert r.headers["X-RateLimit-Scope"] == "default"

    def test_returns_429_after_exceeding_user_limit(self, client):
        # Make user_id available via a custom state injection:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from agent_system.core.security_middleware import SlidingWindowRateLimitMiddleware
        from agent_system.core.rate_limit import reset_limiter_registry

        reset_limiter_registry()
        app = FastAPI()
        app.add_middleware(SlidingWindowRateLimitMiddleware)

        class FakeUser:
            user_id = "alice"

        @app.middleware("http")
        async def inject_user(request, call_next):
            request.state.user = FakeUser()
            return await call_next(request)

        @app.get("/api/agents")
        async def agents():
            return {"agents": []}

        client2 = TestClient(app)
        # First 5 requests allowed (limit=5)
        for i in range(5):
            r = client2.get("/api/agents")
            assert r.status_code == 200, f"request {i} failed"
        # 6th request blocked
        r = client2.get("/api/agents")
        assert r.status_code == 429
        assert "Retry-After" in r.headers
        assert int(r.headers["Retry-After"]) >= 1

    def test_health_endpoint_exempt(self, client):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from agent_system.core.security_middleware import SlidingWindowRateLimitMiddleware

        app = FastAPI()
        app.add_middleware(SlidingWindowRateLimitMiddleware)

        @app.get("/api/health")
        async def health():
            return {"status": "ok"}

        c = TestClient(app)
        r = c.get("/api/health")
        assert r.status_code == 200
        assert "X-RateLimit-Limit" not in r.headers  # exempt path → no headers

    def test_scope_changes_per_path(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from agent_system.core.security_middleware import SlidingWindowRateLimitMiddleware
        from agent_system.core.rate_limit import reset_limiter_registry

        reset_limiter_registry()
        monkeypatch.setenv("AGENT_RATE_LIMIT_SCOPE_HEAVY_USER", "10")
        reset_limiter_registry()

        app = FastAPI()
        app.add_middleware(SlidingWindowRateLimitMiddleware)

        @app.get("/api/agents")
        async def agents():
            return {}

        @app.get("/api/audit/query")
        async def audit():
            return {}

        client = TestClient(app)
        r1 = client.get("/api/agents")
        r2 = client.get("/api/audit/query")
        assert r1.headers["X-RateLimit-Scope"] == "default"
        assert r2.headers["X-RateLimit-Scope"] == "heavy"

    def test_fail_open_on_limiter_error(self, monkeypatch):
        """If limiter raises, request is allowed (fail-open)."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from agent_system.core.security_middleware import SlidingWindowRateLimitMiddleware
        from agent_system.core.rate_limit import get_limiter_registry

        app = FastAPI()
        app.add_middleware(SlidingWindowRateLimitMiddleware)

        @app.get("/api/agents")
        async def agents():
            return {}

        with patch.object(
            get_limiter_registry(), "check_request", side_effect=RuntimeError("boom")
        ):
            client = TestClient(app)
            r = client.get("/api/agents")
            assert r.status_code == 200  # fail-open → allowed

    def test_fail_closed_when_configured(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from agent_system.core.security_middleware import SlidingWindowRateLimitMiddleware
        from agent_system.core.rate_limit import get_limiter_registry

        app = FastAPI()
        app.add_middleware(SlidingWindowRateLimitMiddleware, fail_mode="closed")

        @app.get("/api/agents")
        async def agents():
            return {}

        with patch.object(
            get_limiter_registry(), "check_request", side_effect=RuntimeError("boom")
        ):
            client = TestClient(app)
            r = client.get("/api/agents")
            assert r.status_code == 503  # fail-closed → service unavailable