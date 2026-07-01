"""
Tests: Auth — JWT + RBAC + middleware
"""

import time
import pytest

from agent_system.core.auth import (
    AuthService,
    AuthMiddleware,
    require_auth,
    user_can,
    GlobalRole,
    Permission,
    User,
    RBAC,
)


# ── AuthService ──

class TestAuthService:
    def test_issue_and_verify(self):
        svc = AuthService(secret="test-secret")
        token = svc.issue_token("alice", tenant_id="acme", role="user")
        assert isinstance(token, str)

        payload = svc.verify_token(token)
        assert payload is not None
        assert payload.sub == "alice"
        assert payload.tenant_id == "acme"
        assert payload.role == "user"

    def test_wrong_secret_rejects(self):
        svc_a = AuthService(secret="secret-a")
        svc_b = AuthService(secret="secret-b")
        token = svc_a.issue_token("alice")
        # Verify with different secret fails
        assert svc_b.verify_token(token) is None

    def test_expired_token_rejected(self):
        svc = AuthService(secret="x")
        # Issue with negative TTL
        token = svc.issue_token("alice", ttl=-1)
        # Already expired
        assert svc.verify_token(token) is None

    def test_invalid_token_rejected(self):
        svc = AuthService(secret="x")
        assert svc.verify_token("not-a-jwt") is None
        assert svc.verify_token("") is None

    def test_user_from_payload(self):
        svc = AuthService(secret="x")
        token = svc.issue_token("alice", tenant_id="acme", role="tenant_admin")
        payload = svc.verify_token(token)
        user = svc.user_from_payload(payload)
        assert user.id == "alice"
        assert user.tenant_id == "acme"
        assert user.global_role == GlobalRole.TENANT_ADMIN

    def test_default_ttl(self):
        svc = AuthService(secret="x", default_ttl=60)
        token = svc.issue_token("alice")
        payload = svc.verify_token(token)
        # Default TTL means ~60s, so exp should be > iat by 59-61s
        assert 55 <= (payload.exp - payload.iat) <= 65


# ── Middleware ──

class TestAuthMiddleware:
    @pytest.mark.asyncio
    async def test_no_token_passes_through(self):
        """Without a token, the middleware doesn't inject a user context."""
        scope = {
            "type": "http",
            "headers": [],
            "method": "GET",
            "path": "/",
        }
        captured = {}
        async def app(scope, receive, send):
            from agent_system.core.auth.context import get_current_tenant
            ctx = get_current_tenant()
            captured["ctx"] = ctx

        svc = AuthService(secret="x")
        mw = AuthMiddleware(app, auth_service=svc)
        await mw(scope, None, None)
        # No user set
        assert captured.get("ctx") is None or captured.get("ctx").user is None

    @pytest.mark.asyncio
    async def test_valid_token_injects_user(self):
        svc = AuthService(secret="x")
        token = svc.issue_token("alice", tenant_id="acme", role="user")

        scope = {
            "type": "http",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "method": "GET",
            "path": "/",
        }
        captured = {}
        async def app(scope, receive, send):
            from agent_system.core.auth.context import get_current_tenant
            from agent_system.core.auth.jwt import get_current_user
            ctx = get_current_tenant()
            user = get_current_user()
            captured["ctx"] = ctx
            captured["user"] = user

        mw = AuthMiddleware(app, auth_service=svc)
        await mw(scope, None, None)
        assert captured["user"] is not None
        assert captured["user"].id == "alice"
        assert captured["user"].tenant_id == "acme"
        assert captured["ctx"].tenant_id == "acme"

    @pytest.mark.asyncio
    async def test_invalid_token_passes_through_without_user(self):
        scope = {
            "type": "http",
            "headers": [(b"authorization", b"Bearer invalid-token")],
        }
        captured = {}
        async def app(scope, receive, send):
            from agent_system.core.auth.jwt import get_current_user
            captured["user"] = get_current_user()
        mw = AuthMiddleware(app, auth_service=AuthService(secret="x"))
        await mw(scope, None, None)
        assert captured["user"] is None

    @pytest.mark.asyncio
    async def test_non_http_passes_through(self):
        scope = {"type": "websocket"}
        async def app(scope, receive, send):
            pass
        mw = AuthMiddleware(app)
        await mw(scope, None, None)  # should not raise


# ── RBAC ──

class TestUserCan:
    def test_admin_can_audit(self):
        user = User(id="a1", tenant_id="t", global_role=GlobalRole.TENANT_ADMIN)
        assert user_can(user, Permission.AUDIT) is True

    def test_user_cannot_audit(self):
        user = User(id="u1", tenant_id="t", global_role=GlobalRole.USER)
        assert user_can(user, Permission.AUDIT) is False

    def test_viewer_only_reads(self):
        user = User(id="v1", tenant_id="t", global_role=GlobalRole.VIEWER)
        assert user_can(user, Permission.READ) is True
        assert user_can(user, Permission.WRITE) is False
        assert user_can(user, Permission.DELETE) is False

    def test_custom_rbac_matrix(self):
        custom = {GlobalRole.USER: {Permission.READ, Permission.EXPORT}}
        rbac = RBAC(custom_matrix=custom)
        user = User(id="u1", tenant_id="t", global_role=GlobalRole.USER)
        assert rbac.user_can(user, Permission.EXPORT) is True
        assert rbac.user_can(user, Permission.WRITE) is False


# ── require_auth dependency ──

class TestRequireAuth:
    def test_no_header_raises(self):
        from fastapi import HTTPException
        dep = require_auth(AuthService(secret="x"))
        with pytest.raises(HTTPException) as exc:
            dep(authorization=None)
        assert exc.value.status_code == 401

    def test_wrong_scheme_raises(self):
        from fastapi import HTTPException
        dep = require_auth(AuthService(secret="x"))
        with pytest.raises(HTTPException):
            dep(authorization="Basic abcdef")

    def test_valid_token_returns_user(self):
        svc = AuthService(secret="x")
        token = svc.issue_token("alice", tenant_id="acme", role="tenant_admin")
        dep = require_auth(svc)
        user = dep(authorization=f"Bearer {token}")
        assert user.id == "alice"
        assert user.tenant_id == "acme"
        assert user.global_role == GlobalRole.TENANT_ADMIN

    def test_expired_token_raises(self):
        from fastapi import HTTPException
        svc = AuthService(secret="x")
        token = svc.issue_token("alice", ttl=-1)
        dep = require_auth(svc)
        with pytest.raises(HTTPException):
            dep(authorization=f"Bearer {token}")


# ── Global ──

class TestGlobalAuth:
    def test_singleton(self):
        from agent_system.core.auth import get_auth_service
        a = get_auth_service()
        b = get_auth_service()
        assert a is b
