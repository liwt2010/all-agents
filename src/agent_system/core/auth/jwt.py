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
from typing import Any, Dict, List, Optional, Tuple
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
    scopes: list[str] = Field(default_factory=list)
    iat: int = 0
    exp: int = 0


class AuthService:
    """JWT token issuer + verifier.

    Secret rotation support (PR-16):
      - AUTH_SECRETS env var (comma-separated, format "kid:secret,kid:secret,...")
        - First entry is the CURRENT signing key (used for new tokens)
        - All entries are valid for VERIFY (so old tokens still work after rotation)
      - AUTH_SECRET env var (single key) is still supported for backward compat
        - Auto-converted to a single-key AUTH_SECRETS with kid="default"
      - Tokens get a `kid` claim so the verifier picks the right key on rotation

    Example rotation scenario:
      Day 0: AUTH_SECRETS=v1:secret-v1-long-enough-32chars,v0:secret-v0-long-enough-32chars
              (current=v1, old v0 still verifies)
      Day 7: AUTH_SECRETS=v2:secret-v2-long-enough-32chars,v1:secret-v1-long-enough-32chars
              (current=v2, v1 still verifies until all old tokens expire)
      Day 30: AUTH_SECRETS=v2:secret-v2-long-enough-32chars
              (old key removed; rotation complete)
    """

    def __init__(self, secret: str | None = None, default_ttl: int = 3600):
        # Resolve secrets: explicit arg > AUTH_SECRETS env > AUTH_SECRET env
        auth_secrets_env = os.environ.get("AUTH_SECRETS", "").strip()
        auth_secret_env = os.environ.get("AUTH_SECRET", "").strip()

        if secret:
            # Explicit single secret -> single-key store with kid="default"
            self._secrets: list[tuple[str, str]] = [("default", secret)]
        elif auth_secrets_env:
            self._secrets = self._parse_auth_secrets(auth_secrets_env)
        elif auth_secret_env:
            # Backward compat: single AUTH_SECRET
            self._secrets = [("default", auth_secret_env)]
        else:
            raise RuntimeError(
                "AUTH_SECRET (or AUTH_SECRETS) is not set. "
                "In production, set a 32+ character random secret. "
                "For local dev only, set AUTH_SECRET to any non-empty value."
            )

        # Production guard: require strong secrets
        for kid, s in self._secrets:
            if len(s) < 32:
                import logging as _lg
                _lg.warning(
                    f"AUTH_SECRET[{kid}] is only {len(s)} characters long "
                    f"(recommended: 32+). For production use a longer secret."
                )
        if not self._secrets:
            raise RuntimeError("No valid secrets configured")

        self.current_kid = self._secrets[0][0]
        self.current_secret = self._secrets[0][1]
        self.algorithm = "HS256"
        self.default_ttl = default_ttl
        logger.info(
            "AuthService: %d secret(s) configured, current kid=%s",
            len(self._secrets), self.current_kid,
        )

    @staticmethod
    def _parse_auth_secrets(raw: str) -> list[tuple[str, str]]:
        """Parse 'kid1:secret1,kid2:secret2' -> [('kid1','secret1'), ...]"""
        result: list[tuple[str, str]] = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" not in entry:
                # No kid prefix; treat whole thing as secret with auto kid
                kid = f"key{len(result)}"
                result.append((kid, entry))
                continue
            kid, secret = entry.split(":", 1)
            kid = kid.strip()
            secret = secret.strip()
            if not kid or not secret:
                raise ValueError(
                    f"Invalid AUTH_SECRETS entry: {entry!r}. "
                    f"Expected format 'kid:secret'"
                )
            result.append((kid, secret))
        if not result:
            raise ValueError("AUTH_SECRETS is empty")
        return result

    def get_keys_for_rotation(self) -> list[tuple[str, str]]:
        """Return all (kid, secret) pairs — for ops/audit."""
        return list(self._secrets)

    def issue_token(
        self,
        user_id: str,
        tenant_id: str = "default",
        role: str = "user",
        scopes: list[str] | None = None,
        ttl: int | None = None,
    ) -> str:
        now = int(time.time())
        payload = {
            "sub": user_id,
            "tenant_id": tenant_id,
            "role": role,
            "scopes": scopes or [],
            "iat": now,
            "exp": now + (ttl or self.default_ttl),
            "kid": self.current_kid,
        }
        return jwt.encode(payload, self.current_secret, algorithm=self.algorithm)

    def verify_token(self, token: str) -> TokenPayload | None:
        # Peek at the kid in the (unverified) claims to pick the right key.
        # We store `kid` in the claims payload (not the JWS header) so
        # jwt.get_unverified_claims is the right accessor.
        kid = None
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
            kid = unverified.get("kid")
        except Exception:
            kid = None

        # Find the right secret
        secret = None
        if kid:
            for k, s in self._secrets:
                if k == kid:
                    secret = s
                    break
        if not secret:
            # Fall back to current secret (handles tokens issued before rotation
            # or tokens without a kid claim)
            secret = self.current_secret

        try:
            data = jwt.decode(token, secret, algorithms=[self.algorithm])
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

_current_user: ContextVar[User | None] = ContextVar("_current_user", default=None)


def set_current_user(user: User | None) -> object:
    return _current_user.set(user)


def reset_current_user(token: object) -> None:
    _current_user.reset(token)


def get_current_user() -> User | None:
    return _current_user.get()


# ── FastAPI dependencies ──

def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    if authorization.lower().startswith("bearer "):
        return authorization[7:]
    return authorization


def require_auth(auth_service: AuthService | None = None):
    """FastAPI dependency: require a valid Bearer token."""
    svc = auth_service or AuthService()

    def _dep(
        authorization: str | None = Header(default=None, alias="Authorization"),
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


def user_can(user: User, permission: Permission, rbac: RBAC | None = None) -> bool:
    check_rbac = rbac or RBAC()
    return check_rbac.user_can(user, permission)


# ── Middleware ──

class AuthMiddleware:
    """Sets a TenantContext in the contextvar from the Authorization header."""

    def __init__(self, app, auth_service: AuthService | None = None):
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
_default_service: AuthService | None = None


def get_auth_service() -> AuthService:
    global _default_service
    if _default_service is None:
        _default_service = AuthService()
    return _default_service
