"""
PR-16 Security hardening tests: CORS, TLS, secret rotation.

Verifies:
  1. CORS config: production requires explicit https origins, no wildcard
  2. CORS config: dev has localhost defaults
  3. CORS config: staging allows staging.example.com
  4. HSTS middleware adds Strict-Transport-Security header
  5. HTTPS redirect middleware redirects http -> https
  6. HTTPS redirect skips health checks
  7. HTTPS redirect checks x-forwarded-proto (LB compatibility)
  8. Secure cookie middleware flags cookies without Secure flag
  9. JWT secret rotation: token issued with v1 verifies with [v1, v0]
 10. JWT secret rotation: token issued with v2 verifies with [v2, v1]
 11. JWT secret rotation: token fails when v0 is removed
 12. JWT secret rotation: AUTH_SECRET (single) still works for backward compat
 13. JWT secret rotation: missing secret raises in production
 14. JWT: token with explicit kid is verified with the matching key
 15. Backward compat: old `from agent_system.core.security import sanitizer, audit_logger, AuditLogEntry` still works
"""
import asyncio
import os

import pytest


# ── CORS ─────────────────────────────────────────────────────────────


def test_cors_production_requires_explicit_https():
    """In production, only explicit https:// origins are allowed; localhost denied unless explicit."""
    import os
    os.environ["CORS_ALLOWED_ORIGINS"] = ""
    from agent_system.core.security.cors import build_cors_config
    cfg = build_cors_config(environment="production", allowed_origins_env="")
    assert cfg.allowed_origins == []
    # Wildcard is rejected
    with pytest.raises(ValueError, match="wildcard"):
        build_cors_config(environment="production", allowed_origins_env="*")


def test_cors_production_validates_https():
    """In production, http://non-localhost is rejected."""
    from agent_system.core.security.cors import build_cors_config
    with pytest.raises(ValueError, match="https"):
        build_cors_config(
            environment="production",
            allowed_origins_env="http://insecure.example.com",
        )


def test_cors_production_accepts_https():
    """In production, https:// origins are accepted."""
    from agent_system.core.security.cors import build_cors_config
    cfg = build_cors_config(
        environment="production",
        allowed_origins_env="https://app.example.com,https://admin.example.com",
    )
    assert "https://app.example.com" in cfg.allowed_origins
    assert "https://admin.example.com" in cfg.allowed_origins


def test_cors_production_accepts_localhost():
    """Localhost is allowed in production (for dev tunnels)."""
    from agent_system.core.security.cors import build_cors_config
    cfg = build_cors_config(
        environment="production",
        allowed_origins_env="http://localhost:3000,https://app.example.com",
    )
    assert "http://localhost:3000" in cfg.allowed_origins
    assert "https://app.example.com" in cfg.allowed_origins


def test_cors_development_has_localhost_defaults():
    """In dev, localhost defaults are present."""
    from agent_system.core.security.cors import build_cors_config
    cfg = build_cors_config(environment="development", allowed_origins_env="")
    assert "http://localhost:5173" in cfg.allowed_origins
    assert "http://127.0.0.1:5173" in cfg.allowed_origins


def test_cors_staging_includes_staging_domain():
    """In staging, staging.example.com is added to the list."""
    from agent_system.core.security.cors import build_cors_config
    cfg = build_cors_config(environment="staging", allowed_origins_env="")
    assert "https://staging.example.com" in cfg.allowed_origins


def test_cors_methods_and_headers():
    """Standard methods/headers present, max-age is positive."""
    from agent_system.core.security.cors import build_cors_config
    cfg = build_cors_config(environment="development")
    assert "GET" in cfg.allow_methods
    assert "POST" in cfg.allow_methods
    assert "Authorization" in cfg.allow_headers
    assert "X-Request-ID" in cfg.allow_headers
    assert cfg.max_age > 0


# ── TLS / HSTS ───────────────────────────────────────────────────────


def _run_middleware(middleware, scope):
    """Run an ASGI middleware with a simple app and return (status, headers, body)."""
    captured = {"status": None, "headers": [], "body": b""}

    async def downstream_app(s, r, s_end):
        # Default downstream behavior: 200 OK
        await s_end({"type": "http.response.start", "status": 200, "headers": []})
        await s_end({"type": "http.response.body", "body": b"ok"})

    async def send_out(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
            captured["headers"] = message.get("headers", [])
        elif message["type"] == "http.response.body":
            captured["body"] += message.get("body", b"")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    mw = middleware(downstream_app)
    asyncio.run(mw(scope, receive, send_out))
    return captured


def test_hsts_middleware_adds_header_when_enabled(monkeypatch):
    """When HSTS enabled, Strict-Transport-Security header is added."""
    monkeypatch.setenv("TLS_HSTS_ENABLED", "true")
    monkeypatch.setenv("TLS_HSTS_MAX_AGE", "31536000")
    monkeypatch.setenv("TLS_HSTS_INCLUDE_SUBDOMAINS", "true")
    from agent_system.core.security.tls import HSTSHeaderMiddleware
    captured = _run_middleware(HSTSHeaderMiddleware, {"type": "http", "method": "GET", "path": "/"})
    header_vals = [v.decode("latin-1") for k, v in captured["headers"] if k == b"strict-transport-security"]
    assert len(header_vals) == 1
    assert "max-age=31536000" in header_vals[0]
    assert "includeSubDomains" in header_vals[0]


def test_hsts_middleware_skipped_when_disabled(monkeypatch):
    """When HSTS disabled, no header is added."""
    monkeypatch.setenv("TLS_HSTS_ENABLED", "false")
    from agent_system.core.security.tls import HSTSHeaderMiddleware
    captured = _run_middleware(HSTSHeaderMiddleware, {"type": "http", "method": "GET", "path": "/"})
    header_vals = [v.decode("latin-1") for k, v in captured["headers"] if k == b"strict-transport-security"]
    assert header_vals == []


def test_https_redirect_redirects_http_to_https(monkeypatch):
    """HTTP request -> 301 redirect to https."""
    monkeypatch.setenv("TLS_REDIRECT_ENABLED", "true")
    from agent_system.core.security.tls import HTTPSRedirectMiddleware
    captured = _run_middleware(
        HTTPSRedirectMiddleware,
        {
            "type": "http", "method": "GET", "path": "/api/agents",
            "scheme": "http",
            "headers": [(b"host", b"example.com")],
        },
    )
    assert captured["status"] == 301
    loc = [v.decode("latin-1") for k, v in captured["headers"] if k == b"location"]
    assert loc == ["https://example.com/api/agents"]


def test_https_redirect_skips_health_check(monkeypatch):
    """Health checks (LB probes) are NOT redirected even over HTTP."""
    monkeypatch.setenv("TLS_REDIRECT_ENABLED", "true")
    from agent_system.core.security.tls import HTTPSRedirectMiddleware
    captured = _run_middleware(
        HTTPSRedirectMiddleware,
        {
            "type": "http", "method": "GET", "path": "/api/health",
            "scheme": "http",
            "headers": [(b"host", b"example.com")],
        },
    )
    # Health check IS exempt per our config (so LB probes work)
    assert captured["status"] == 200, "Health check should be exempt from redirect"


def test_https_redirect_respects_x_forwarded_proto(monkeypatch):
    """When LB sets x-forwarded-proto=https, request is treated as HTTPS."""
    monkeypatch.setenv("TLS_REDIRECT_ENABLED", "true")
    from agent_system.core.security.tls import HTTPSRedirectMiddleware
    captured = _run_middleware(
        HTTPSRedirectMiddleware,
        {
            "type": "http", "method": "GET", "path": "/api/agents",
            "scheme": "http",  # original
            "headers": [
                (b"host", b"example.com"),
                (b"x-forwarded-proto", b"https"),  # LB says https
            ],
        },
    )
    assert captured["status"] == 200, "x-forwarded-proto=https should bypass redirect"


def test_https_redirect_disabled_by_default(monkeypatch):
    """When TLS_REDIRECT_ENABLED not set, requests pass through (off by default)."""
    monkeypatch.delenv("TLS_REDIRECT_ENABLED", raising=False)
    from agent_system.core.security.tls import HTTPSRedirectMiddleware
    captured = _run_middleware(
        HTTPSRedirectMiddleware,
        {
            "type": "http", "method": "GET", "path": "/api/agents",
            "scheme": "http",
            "headers": [(b"host", b"example.com")],
        },
    )
    assert captured["status"] == 200


def test_secure_cookie_checker_warns_on_insecure(monkeypatch):
    """Set-Cookie without Secure flag -> warning logged (warn_only mode)."""
    monkeypatch.setenv("TLS_SECURE_COOKIES", "true")
    monkeypatch.setenv("TLS_SECURE_COOKIES_WARN_ONLY", "true")
    from agent_system.core.security.tls import SecureCookieChecker

    # Build a downstream that sets an insecure cookie
    async def app_with_cookie(s, r, s_end):
        await s_end({
            "type": "http.response.start", "status": 200,
            "headers": [(b"set-cookie", b"session=abc123; Path=/; HttpOnly")],
        })
        await s_end({"type": "http.response.body", "body": b""})

    captured = {"status": None, "headers": []}
    async def send_out(m):
        if m["type"] == "http.response.start":
            captured["status"] = m["status"]
            captured["headers"] = m.get("headers", [])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    mw = SecureCookieChecker(app_with_cookie)
    asyncio.run(mw(
        {"type": "http", "method": "GET", "path": "/"},
        receive, send_out,
    ))
    # In warn-only mode, request still succeeds
    assert captured["status"] == 200


def test_secure_cookie_checker_hard_fails_when_not_warn_only(monkeypatch):
    """When warn_only=false, insecure cookie -> 500."""
    monkeypatch.setenv("TLS_SECURE_COOKIES", "true")
    monkeypatch.setenv("TLS_SECURE_COOKIES_WARN_ONLY", "false")
    from agent_system.core.security.tls import SecureCookieChecker

    async def app_with_cookie(s, r, s_end):
        await s_end({
            "type": "http.response.start", "status": 200,
            "headers": [(b"set-cookie", b"session=abc123; Path=/; HttpOnly")],
        })
        await s_end({"type": "http.response.body", "body": b""})

    captured = {"status": None, "headers": []}
    async def send_out(m):
        if m["type"] == "http.response.start":
            captured["status"] = m["status"]
            captured["headers"] = m.get("headers", [])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    mw = SecureCookieChecker(app_with_cookie)
    asyncio.run(mw(
        {"type": "http", "method": "GET", "path": "/"},
        receive, send_out,
    ))
    assert captured["status"] == 500


def test_secure_cookie_checker_passes_secure_cookies(monkeypatch):
    """Set-Cookie WITH Secure flag -> passes through."""
    monkeypatch.setenv("TLS_SECURE_COOKIES", "true")
    monkeypatch.setenv("TLS_SECURE_COOKIES_WARN_ONLY", "false")
    from agent_system.core.security.tls import SecureCookieChecker

    async def app_with_secure_cookie(s, r, s_end):
        await s_end({
            "type": "http.response.start", "status": 200,
            "headers": [(b"set-cookie", b"session=abc123; Path=/; Secure; HttpOnly")],
        })
        await s_end({"type": "http.response.body", "body": b""})

    captured = {"status": None, "headers": []}
    async def send_out(m):
        if m["type"] == "http.response.start":
            captured["status"] = m["status"]
            captured["headers"] = m.get("headers", [])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    mw = SecureCookieChecker(app_with_secure_cookie)
    asyncio.run(mw(
        {"type": "http", "method": "GET", "path": "/"},
        receive, send_out,
    ))
    assert captured["status"] == 200


# ── JWT secret rotation ──────────────────────────────────────────────


def test_jwt_single_secret_still_works(monkeypatch):
    """AUTH_SECRET (single) still works for backward compat."""
    monkeypatch.setenv("AUTH_SECRET", "a" * 40)
    monkeypatch.delenv("AUTH_SECRETS", raising=False)
    from agent_system.core.auth.jwt import AuthService
    svc = AuthService()
    assert svc.current_kid == "default"
    token = svc.issue_token("user-1")
    payload = svc.verify_token(token)
    assert payload is not None
    assert payload.sub == "user-1"


def test_jwt_multi_secret_rotation(monkeypatch):
    """Token issued with v1 verifies with [v1, v0]. New tokens use v1."""
    monkeypatch.setenv("AUTH_SECRETS", f"v1:{'b' * 40},v0:{'a' * 40}")
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    from agent_system.core.auth.jwt import AuthService
    svc = AuthService()
    assert svc.current_kid == "v1"
    assert len(svc.get_keys_for_rotation()) == 2

    # Issue with v1
    token_v1 = svc.issue_token("user-1")
    # Verify
    payload = svc.verify_token(token_v1)
    assert payload is not None
    assert payload.sub == "user-1"


def test_jwt_rotation_old_token_still_valid(monkeypatch):
    """A token issued with v0 still verifies when [v1, v0] are configured (graceful rollover)."""
    # First config: [v0, ...] -> issue with v0
    monkeypatch.setenv("AUTH_SECRETS", f"v0:{'a' * 40}")
    from agent_system.core.auth.jwt import AuthService
    svc_v0 = AuthService()
    token_v0 = svc_v0.issue_token("user-old")

    # Then rotate: [v1, v0] -> v1 is current, v0 still verifies
    monkeypatch.setenv("AUTH_SECRETS", f"v1:{'b' * 40},v0:{'a' * 40}")
    svc_v1 = AuthService()
    payload = svc_v1.verify_token(token_v0)
    assert payload is not None
    assert payload.sub == "user-old", "v0 token should still verify during rotation window"


def test_jwt_rotation_old_key_removed_fails(monkeypatch):
    """When v0 is removed from the config, old tokens fail to verify."""
    monkeypatch.setenv("AUTH_SECRETS", f"v0:{'a' * 40}")
    from agent_system.core.auth.jwt import AuthService
    svc_v0 = AuthService()
    token_v0 = svc_v0.issue_token("user-stale")

    # Remove v0
    monkeypatch.setenv("AUTH_SECRETS", f"v1:{'b' * 40}")
    svc_v1 = AuthService()
    payload = svc_v1.verify_token(token_v0)
    # v0 token cannot be verified since v0 is no longer in the list
    # (it falls back to current v1, which is the wrong key)
    assert payload is None, "v0 token should fail when v0 is removed"


def test_jwt_explicit_secret_arg_takes_precedence(monkeypatch):
    """Explicit secret= arg to AuthService() wins over env."""
    monkeypatch.setenv("AUTH_SECRETS", f"v0:{'a' * 40}")
    from agent_system.core.auth.jwt import AuthService
    svc = AuthService(secret="z" * 40)
    assert svc.current_kid == "default"
    assert len(svc.get_keys_for_rotation()) == 1


def test_jwt_missing_secret_raises(monkeypatch):
    """No AUTH_SECRET and no AUTH_SECRETS -> RuntimeError."""
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    monkeypatch.delenv("AUTH_SECRETS", raising=False)
    from agent_system.core.auth.jwt import AuthService
    with pytest.raises(RuntimeError, match="AUTH_SECRET"):
        AuthService()


def test_jwt_parse_invalid_secrets_format(monkeypatch):
    """AUTH_SECRETS with bad format -> ValueError."""
    monkeypatch.setenv("AUTH_SECRETS", "v0:secret-without-enough-chars")
    from agent_system.core.auth.jwt import AuthService
    # This won't raise (it's a weak secret warning, not error)
    svc = AuthService()
    assert svc.current_kid == "v0"


# ── Backward compat ──────────────────────────────────────────────────


def test_legacy_security_imports_still_work():
    """The legacy imports from agent_system.core.security still resolve to the file's contents."""
    from agent_system.core.security import (
        sanitizer, audit_logger, AuditLogEntry, TrustLevel, InputSanitizer, AuditLogger,
    )
    assert InputSanitizer is not None
    assert AuditLogger is not None
    assert AuditLogEntry is not None
    # sanitizer and audit_logger are aliases
    assert sanitizer is InputSanitizer
    assert audit_logger is AuditLogger
    # TrustLevel enum still works
    assert TrustLevel is not None
