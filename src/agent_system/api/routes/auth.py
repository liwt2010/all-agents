"""Auth endpoints - JWT token issuance + JWKS public key distribution.

NOTE: /api/auth/token is for local development and integration testing only.
In production, replace with SSO/OIDC integration.

`/api/auth/jwks` exposes the public verify keys (RFC 7517) so external
services can verify tokens issued by this server. Only relevant when
AuthService is configured with RS256 keys (AUTH_PRIVATE_KEY env or
explicit `private_key_pem` arg).
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from agent_system.api.state import get_auth_service_singleton

router = APIRouter(tags=["auth"])


class TokenRequest(BaseModel):
    """Request body for JWT issuance."""

    user_id: str
    tenant_id: str = "default"
    role: str = "user"
    ttl: int | None = None


class TokenResponse(BaseModel):
    """JWT token response."""

    access_token: str
    expires_in: int


@router.post("/api/auth/token", response_model=TokenResponse)
async def issue_token(req: TokenRequest) -> TokenResponse:
    """Issue a JWT for the given user.

    For local dev/testing only. In production, replace with SSO/OIDC.
    """
    auth_service = get_auth_service_singleton()
    ttl = req.ttl or 3600
    token = auth_service.issue_token(
        user_id=req.user_id,
        tenant_id=req.tenant_id,
        role=req.role,
        ttl=ttl,
    )
    return TokenResponse(access_token=token, expires_in=ttl)


@router.get("/api/auth/jwks")
async def jwks() -> dict[str, list[dict[str, str]]]:
    """Return the JSON Web Key Set (RFC 7517) for the public verify keys.

    External services can fetch this once, cache the keys, and verify
    tokens issued by this server without sharing any secret. For HS256
    deployments this returns an empty key set — symmetric secrets must
    never be published.
    """
    return get_auth_service_singleton().get_jwks()
