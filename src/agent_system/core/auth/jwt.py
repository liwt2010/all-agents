"""
Auth — PLATFORM §7.7, §28

JWT-based auth + RBAC enforcement. Provides:

  - AuthService: token issue / verify (HS256 JWT)
  - require_auth / require_permission dependencies for FastAPI
  - AuthMiddleware: extracts user from Authorization header
  - UserContextToken: contextvar-based access to current user

For now uses HS256 with a configurable secret. Production should switch
to RS256 with key rotation.
"""

import logging
import os
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Dict, List, Optional
from fastapi import Header

import jwt
from pydantic import BaseModel, Field

from agent_system.core.auth.models import (
    GlobalRole, Permission, User, RBAC,
)
from agent_system.core.auth.context import (
    TenantContext, _UserStub, set_tenant_context, reset_tenant_context, get_current_tenant,
)

logger = logging.getLogger(__name__)


# ── AuthService ──

class TokenPayload(BaseModel):
    sub: str
    tenant_id: str = "default"
    role: str = "user"
    scopes: List[str] = Field(default_factory=list)
    iat: int = 0
    exp: int = 0


class AuthService:
    """JWT token issuer + verifier."""

    def __init__(self, secret: Optional[str] = None, default_ttl: int = 3600):
        env_secret = os.environ.get("AUTH_SECRET", "")
        resolved = secret or env_secret

        # Production guard: require a strong secret
        if not resolved:
            raise RuntimeError(
                "AUTH_SECRET is not set. "
                "In production, set a 32+ character random secret. "
                "For local dev only, set AUTH_SECRET to any non-empty value."
            )
        if len(resolved) < 32:
            import logging as _lg
            _lg.warning(
                f"AUTH_SECRET is only {len(resolved)} characters long "
                f"(recommended: 32+). For production use a longer secret."
            )

        self.secret = resolved
        self.algorithm = "HS256"
        self.default_ttl = default_ttl

    def issue_token(
        self,
        user_id: str,
        tenant_id: str = "default",
        role: str = "user",
        scopes: Optional[List[str]] = None,
        ttl: Optional[int] = None,
    ) -> str:
        now = int(time.time())
        payload = {
            "sub": user_id,
            "tenant_id": tenant_id,
            "role": role,
            "scopes": scopes or [],
            "iat": now,
            "exp": now + (ttl or self.default_ttl),
        }
        return jwt.encode(payload, self.secret, algorithm=self.algorithm)

    def verify_token(self, token: str) -> Optional[TokenPayload]:
        try:
            data = jwt.decode(token, self.secret, algorithms=[self.algorithm])
            return TokenPayload(**data)
        except jwt.ExpiredSignatureError:
            logger.debug("Token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.debug(f"Invalid token: {e}")
            return None

    def user_from_payload(self, payload: TokenPayload) -> User:
        return User(
            id=payload.sub,
            tenant_id=payload.tenant_id,
            global_role=_role_from_str(payload.role),
            group_ids=[],
        )


def _role_from_str(s: str) -> GlobalRole:
    try:
        return GlobalRole(s)
    except ValueError:
        return GlobalRole.USER


# ── Context-aware user access ──

_current_user: ContextVar[Optional[User]] = ContextVar("_current_user", default=None)


def set_current_user(user: Optional[User]) -> object:
    return _current_user.set(user)


def reset_current_user(token: object) -> None:
    _current_user.reset(token)


def get_current_user() -> Optional[User]:
    return _current_user.get()


# ── FastAPI dependencies ──

def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    if authorization.lower().startswith("bearer "):
        return authorization[7:]
    return authorization


def require_auth(auth_service: Optional[AuthService] = None):
    """FastAPI dependency: require a valid Bearer token."""
    svc = auth_service or AuthService()

    def _dep(
        authorization: Optional[str] = Header(default=None, alias="Authorization"),
    ) -> User:
        from fastapi import HTTPException
        token = _extract_bearer(authorization)
        if not token:
            raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
        payload = svc.verify_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return svc.user_from_payload(payload)
    return _dep


def user_can(user: User, permission: Permission, rbac: Optional[RBAC] = None) -> bool:
    check_rbac = rbac or RBAC()
    return check_rbac.user_can(user, permission)


# ── Middleware ──

class AuthMiddleware:
    """Sets a TenantContext in the contextvar from the Authorization header."""

    def __init__(self, app, auth_service: Optional[AuthService] = None):
        self.app = app
        self.auth_service = auth_service or AuthService()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("latin-1", errors="ignore")
        token = _extract_bearer(auth_header)
        user = None
        if token:
            payload = self.auth_service.verify_token(token)
            if payload:
                user = self.auth_service.user_from_payload(payload)

        if user:
            ctx = TenantContext(
                user=_UserStub(
                    user_id=user.id,
                    tenant_id=user.tenant_id,
                    group_ids=list(user.group_ids),
                    perm_group_ids=list(user.perm_group_ids),
                    project_ids=list(user.project_ids),
                    global_role=user.global_role.value,
                ),
                tenant_id=user.tenant_id,
                group_ids=list(user.group_ids),
            )
            ctx_token = set_tenant_context(ctx)
            user_token = set_current_user(user)
            try:
                await self.app(scope, receive, send)
            finally:
                reset_tenant_context(ctx_token)
                reset_current_user(user_token)
        else:
            await self.app(scope, receive, send)


# Global default
_default_service: Optional[AuthService] = None


def get_auth_service() -> AuthService:
    global _default_service
    if _default_service is None:
        _default_service = AuthService()
    return _default_service
