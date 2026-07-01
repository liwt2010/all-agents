"""
Tests: Security middleware (rate limit, headers, secrets, body limit)
"""

import asyncio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from agent_system.core.security_middleware import (
    RateLimiter, scan_for_secrets,
    SecurityHeadersMiddleware, RateLimitMiddleware,
    RequestSizeLimitMiddleware, SecretsInRequestMiddleware,
)


# ── RateLimiter ──

class TestRateLimiter:
    def test_allows_under_limit(self):
        limiter = RateLimiter(rate=5, window=1)
        for _ in range(5):
            assert limiter.check("1.1.1.1") is True

    def test_blocks_over_limit(self):
        limiter = RateLimiter(rate=3, window=1)
        for _ in range(3):
            assert limiter.check("1.1.1.1") is True
        # 4th should fail
        assert limiter.check("1.1.1.1") is False

    def test_separate_ips(self):
        limiter = RateLimiter(rate=2, window=1)
        assert limiter.check("1.1.1.1") is True
        assert limiter.check("1.1.1.1") is True
        assert limiter.check("1.1.1.1") is False
        # Different IP not affected
        assert limiter.check("2.2.2.2") is True
        assert limiter.check("2.2.2.2") is True

    def test_reset(self):
        limiter = RateLimiter(rate=2, window=1)
        for _ in range(2):
            limiter.check("1.1.1.1")
        assert limiter.check("1.1.1.1") is False
        limiter.reset("1.1.1.1")
        assert limiter.check("1.1.1.1") is True


# ── Secrets detection ──

class TestSecretsDetection:
    def test_clean_text_no_match(self):
        assert scan_for_secrets("hello world") == []

    def test_anthropic_key(self):
        matches = scan_for_secrets("api key = sk-1234567890abcdefghij")
        assert any("Anthropic" in m[0] or "OpenAI" in m[0] for m in matches)

    def test_github_pat(self):
        matches = scan_for_secrets("token: ghp_1234567890abcdefghij")
        assert any("GitHub" in m[0] for m in matches)

    def test_aws_key(self):
        matches = scan_for_secrets("AKIAIOSFODNN7EXAMPLE")
        assert any("AWS" in m[0] for m in matches)

    def test_jwt_token(self):
        matches = scan_for_secrets("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.abc123def")
        assert any("JWT" in m[0] for m in matches)

    def test_private_key(self):
        matches = scan_for_secrets("-----BEGIN RSA PRIVATE KEY-----\nMIIE...")
        assert any("Private key" in m[0] for m in matches)

    def test_long_input_skipped(self):
        assert scan_for_secrets("x" * 200_000) == []

    def test_safe_content(self):
        # Should not flag regular URLs, emails, etc
        assert scan_for_secrets("Visit https://example.com or email alice@example.com") == []


# ── Middleware integration ──

def _make_test_app(is_production: bool = False) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware, is_production=is_production)

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/echo")
    async def echo(body: dict = {}):
        return {"got": body}

    return app


class TestSecurityHeaders:
    def test_default_headers_added(self):
        app = _make_test_app(is_production=False)
        client = TestClient(app)
        r = client.get("/api/health")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        # HSTS only in production
        assert "Strict-Transport-Security" not in r.headers

    def test_production_includes_hsts(self):
        app = _make_test_app(is_production=True)
        client = TestClient(app)
        r = client.get("/api/health")
        assert "Strict-Transport-Security" in r.headers
        assert "max-age=31536000" in r.headers["Strict-Transport-Security"]


class TestRateLimitMiddleware:
    def test_blocks_after_threshold(self):
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware, rate_per_minute=2, exempt_paths=[])
        @app.get("/api/test")
        async def test():
            return {"ok": True}
        client = TestClient(app)
        # First 2 allowed
        assert client.get("/api/test").status_code == 200
        assert client.get("/api/test").status_code == 200
        # 3rd is rate-limited
        r = client.get("/api/test")
        assert r.status_code == 429
        assert "Retry-After" in r.headers

    def test_health_endpoint_exempt(self):
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware, rate_per_minute=1, exempt_paths=["/api/health"])
        @app.get("/api/health")
        async def health():
            return {"ok": True}
        client = TestClient(app)
        # Multiple calls — all allowed
        for _ in range(5):
            assert client.get("/api/health").status_code == 200


class TestRequestSizeLimit:
    def test_rejects_oversized(self):
        app = FastAPI()
        app.add_middleware(RequestSizeLimitMiddleware, max_bytes=100)
        @app.post("/api/test")
        async def test(body: dict = {}):
            return {"ok": True}
        client = TestClient(app)
        # Body over 100 bytes
        big = "x" * 200
        r = client.post("/api/test", json={"data": big})
        assert r.status_code == 413
        assert r.json()["error"] == "payload_too_large"

    def test_allows_under_limit(self):
        app = FastAPI()
        app.add_middleware(RequestSizeLimitMiddleware, max_bytes=10000)
        @app.post("/api/test")
        async def test(body: dict = {}):
            return {"ok": True}
        client = TestClient(app)
        r = client.post("/api/test", json={"data": "x" * 100})
        assert r.status_code == 200


class TestSecretsMiddleware:
    def test_blocks_with_secret(self):
        app = FastAPI()
        app.add_middleware(SecretsInRequestMiddleware)
        @app.post("/api/test")
        async def test(body: dict = {}):
            return {"ok": True}
        client = TestClient(app)
        r = client.post(
            "/api/test",
            json={"key": "sk-1234567890abcdefghij"},
        )
        assert r.status_code == 400
        assert r.json()["error"] == "secrets_detected"

    def test_allows_clean(self):
        app = FastAPI()
        app.add_middleware(SecretsInRequestMiddleware)
        @app.post("/api/test")
        async def test(body: dict = {}):
            return {"ok": True}
        client = TestClient(app)
        r = client.post("/api/test", json={"key": "clean value"})
        assert r.status_code == 200

    def test_exempts_health(self):
        app = FastAPI()
        app.add_middleware(SecretsInRequestMiddleware, exempt_paths=["/api/test"])
        @app.post("/api/test")
        async def test(body: dict = {}):
            return {"ok": True}
        client = TestClient(app)
        r = client.post("/api/test", json={"key": "sk-1234567890abcdefghij"})
        assert r.status_code == 200
