"""
Auth — PLATFORM §7.7, §28

JWT-based auth + RBAC enforcement. Provides:

  - AuthService: token issue / verify (HS256 OR RS256, auto-detected)
  - require_auth / require_permission dependencies for FastAPI
  - AuthMiddleware: extracts user from Authorization header
  - UserContextToken: contextvar-based access to current user

Algorithm selection (PR RS256):
  - If `AUTH_PRIVATE_KEY` env is set (PEM), AuthService uses RS256
    (asymmetric). Public keys for verify are loaded from
    `AUTH_PUBLIC_KEYS` (comma-separated "kid:public_pem" entries) —
    missing that, the matching public key is derived from the
    private key.
  - Otherwise, falls back to HS256 (symmetric, legacy). Secret rotation
    via `AUTH_SECRETS` (PR-16) is still honoured.

Key rotation is symmetric across both algorithms: each entry is
`(kid, key_material)`. For RS256 the key_material is the PEM string
(public for verify, private for sign). A given kid can be present in
both signing keyring and verifying keyring; the verifier accepts any
registered kid and matches it to its public key.
"""

import logging
import os
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from typing import Any
from fastapi import Header

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey, RSAPrivateKey
from pydantic import BaseModel, Field

from agent_system.core.auth.models import (
    GlobalRole, Permission, User, RBAC,
)
from agent_system.core.auth.context import (
    TenantContext, _UserStub, set_tenant_context, reset_tenant_context, get_current_tenant,
)

logger = logging.getLogger(__name__)


# ── TokenPayload ──

class TokenPayload(BaseModel):
    sub: str
    tenant_id: str = "default"
    role: str = "user"
    scopes: list[str] = Field(default_factory=list)
    iat: int = 0
    exp: int = 0


# ── Key material types ──

class _Key:
    """One registered signing or verifying key, identified by kid."""

    __slots__ = ("kid", "material", "is_private")

    def __init__(self, kid: str, material: Any, is_private: bool):
        self.kid = kid
        self.material = material  # str PEM bytes OR raw secret bytes (HS256)
        self.is_private = is_private


def _looks_like_pem(s: str) -> bool:
    return "BEGIN " in s and "PRIVATE KEY" in s or "PUBLIC KEY" in s


def _load_private_pem(pem: str) -> RSAPrivateKey:
    return serialization.load_pem_private_key(
        pem.encode("utf-8") if isinstance(pem, str) else pem,
        password=None,
    )


def _load_public_pem(pem: str) -> RSAPublicKey:
    return serialization.load_pem_public_key(
        pem.encode("utf-8") if isinstance(pem, str) else pem,
    )


def _public_pem_from_private(priv_pem: str) -> str:
    """Derive the matching public PEM for a given private PEM (RS256 convenience)."""
    priv = _load_private_pem(priv_pem)
    pub = priv.public_key()
    return pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")


# ── AuthService ──

class AuthService:
    """JWT token issuer + verifier.

    Secret / key rotation support (PR-16 + PR-RS256):
      - Algorithm auto-detected: if AUTH_PRIVATE_KEY is set, RS256 is used.
        Otherwise, HS256 (legacy) with `AUTH_SECRETS` or `AUTH_SECRET`.

      - AUTH_SECRETS env (HS256): comma-separated "kid:secret" — current
        key is the FIRST entry, all entries verify. Token `kid` claim
        picks the right verify key.

      - AUTH_PRIVATE_KEY env (RS256): single PEM, signs new tokens.
        Use AUTH_PUBLIC_KEYS="kid1:pem1,kid2:pem2" to register extra
        public keys (e.g. a remote issuer's key). If AUTH_PUBLIC_KEYS
        is omitted, the public key derived from AUTH_PRIVATE_KEY is
        the only verifier — useful when the same process signs and
        verifies.

      - Rotation for RS256: publish a new AUTH_PRIVATE_KEY alongside
        the previous public key in AUTH_PUBLIC_KEYS. Old tokens (signed
        by the previous private key, now retired) still verify via the
        retained public key. When all old tokens have expired, drop
        the stale public key entry.
    """

    def __init__(
        self,
        secret: str | None = None,
        default_ttl: int = 3600,
        private_key_pem: str | None = None,
        public_keys_pem: list[tuple[str, str]] | None = None,
        signing_kid: str | None = None,
    ):
        # ── 1. Resolve algorithm + key material ──
        explicit_private = private_key_pem
        env_private = os.environ.get("AUTH_PRIVATE_KEY", "").strip() or None
        env_public = os.environ.get("AUTH_PUBLIC_KEYS", "").strip() or None
        env_signing_kid = os.environ.get("AUTH_SIGNING_KID", "").strip() or None
        effective_signing_kid = signing_kid or env_signing_kid or "current"

        if explicit_private or env_private:
            # ── RS256 path ──
            priv_pem = explicit_private or env_private
            assert priv_pem is not None
            priv_key = _load_private_pem(priv_pem)
            if not isinstance(priv_key, RSAPrivateKey):
                raise TypeError("AUTH_PRIVATE_KEY is not an RSA private key")

            # Build verify keyring: any explicit public_keys_pem first,
            # then anything in AUTH_PUBLIC_KEYS. The derived pubkey from
            # the private key is ALWAYS added under `effective_signing_kid`
            # so newly-signed tokens are verifiable even when the operator
            # didn't list their own key in AUTH_PUBLIC_KEYS.
            verify_keys: list[_Key] = []
            seen_kids: set[str] = set()
            if public_keys_pem:
                for kid, pem in public_keys_pem:
                    if kid in seen_kids:
                        continue
                    verify_keys.append(_Key(kid=kid, material=pem, is_private=False))
                    seen_kids.add(kid)
            if env_public:
                for kid, pem in self._parse_kv_pem_list(env_public):
                    if kid in seen_kids:
                        continue
                    verify_keys.append(_Key(kid=kid, material=pem, is_private=False))
                    seen_kids.add(kid)
            # Always derive the matching public key from the current
            # private key so newly-signed tokens are verifiable even when
            # the operator didn't list their own key in AUTH_PUBLIC_KEYS.
            # Skip if the signing kid is already registered with the same PEM.
            derived_pub_pem = _public_pem_from_private(priv_pem)
            need_derived = (
                effective_signing_kid not in seen_kids
                or not any(k.material == derived_pub_pem for k in verify_keys)
            )
            if need_derived:
                verify_keys.append(_Key(kid=effective_signing_kid, material=derived_pub_pem, is_private=False))
                seen_kids.add(effective_signing_kid)

            self.algorithm = "RS256"
            self._signing_key = _Key(kid=effective_signing_kid, material=priv_pem, is_private=True)
            self._signing_kid = effective_signing_kid
            self._verify_keys: list[_Key] = verify_keys

        else:
            # ── HS256 path (legacy / dev) ──
            auth_secrets_env = os.environ.get("AUTH_SECRETS", "").strip()
            auth_secret_env = os.environ.get("AUTH_SECRET", "").strip()

            secrets: list[tuple[str, str]]
            if secret:
                secrets = [("default", secret)]
            elif auth_secrets_env:
                secrets = self._parse_auth_secrets(auth_secrets_env)
            elif auth_secret_env:
                secrets = [("default", auth_secret_env)]
            else:
                raise RuntimeError(
                    "No JWT credentials configured. Set one of: "
                    "AUTH_PRIVATE_KEY (RS256), AUTH_SECRETS (HS256 multi-key), "
                    "AUTH_SECRET (HS256 single-key)."
                )

            # Production guard: require strong secrets
            for kid, s in secrets:
                if len(s) < 32:
                    logger.warning(
                        "AUTH_SECRET[%s] is only %d characters long "
                        "(recommended: 32+). For production use a longer secret.",
                        kid, len(s),
                    )
            if not secrets:
                raise RuntimeError("No valid secrets configured")

            self.algorithm = "HS256"
            self._signing_kid = secrets[0][0]
            self._signing_key = _Key(kid=self._signing_kid, material=secrets[0][1], is_private=True)
            self._verify_keys = [
                _Key(kid=kid, material=secret, is_private=False) for kid, secret in secrets
            ]

        # Public attributes (back-compat with v0.1.x callers — kept after
        # the algorithm branches above so HS256-only callers can still
        # introspect current_kid / current_secret).
        self.current_kid = self._signing_kid
        self.current_secret = (
            self._signing_key.material if self.algorithm == "HS256" else None
        )

        self.default_ttl = default_ttl
        logger.info(
            "AuthService: algorithm=%s, current kid=%s, %d verify key(s)",
            self.algorithm, self._signing_kid, len(self._verify_keys),
        )

    # ── Parsing helpers ──

    @staticmethod
    def _parse_auth_secrets(raw: str) -> list[tuple[str, str]]:
        """Parse 'kid1:secret1,kid2:secret2' -> [('kid1','secret1'), ...]"""
        result: list[tuple[str, str]] = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" not in entry:
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

    @staticmethod
    def _parse_kv_pem_list(raw: str) -> list[tuple[str, str]]:
        """Parse 'kid1:-----BEGIN PUBLIC KEY-----\\n...\\n...,kid2:...' -> [(kid, pem), ...]

        PEM bodies contain colons freely, so we split on the FIRST colon only.
        Continuation lines (no `:` and not the start of a PEM header) are
        appended to the previous entry's PEM.
        """
        out: list[tuple[str, str]] = []
        current_kid: str | None = None
        current_pem_lines: list[str] = []
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if chunk.startswith("-----") and current_pem_lines:
                # Continuation of previous PEM (no kid prefix on this chunk)
                current_pem_lines.append(chunk)
                continue
            # Flush previous entry
            if current_kid is not None:
                out.append((current_kid, "\n".join(current_pem_lines)))
            if ":" in chunk and not chunk.startswith("-----"):
                kid, rest = chunk.split(":", 1)
                current_kid = kid.strip()
                current_pem_lines = [rest.strip()]
            else:
                # Standalone PEM chunk with no kid — attach to previous or
                # skip if there's no prior entry.
                if current_kid is not None:
                    current_pem_lines.append(chunk)
        if current_kid is not None:
            out.append((current_kid, "\n".join(current_pem_lines)))
        return out

    # ── Public API ──

    def get_jwks(self) -> dict[str, list[dict[str, str]]]:
        """Return the JWKS (RFC 7517) document for the public verify keys.

        Only RS256 verify keys appear here; HS256 keys are NOT exposed
        (they're symmetric and would defeat the purpose).
        """
        keys: list[dict[str, str]] = []
        seen_kids: set[str] = set()
        for k in self._verify_keys:
            if self.algorithm != "RS256" or k.is_private:
                continue
            if k.kid in seen_kids:
                continue
            seen_kids.add(k.kid)
            pub = _load_public_pem(k.material)
            numbers = pub.public_numbers()
            keys.append({
                "kty": "RSA",
                "kid": k.kid,
                "use": "sig",
                "alg": "RS256",
                "n": _b64url_uint(numbers.n),
                "e": _b64url_uint(numbers.e),
            })
        return {"keys": keys}

    def get_keys_for_rotation(self) -> list[tuple[str, str]]:
        """Return all (kid, material) pairs — for ops/audit."""
        return [(k.kid, k.material if isinstance(k.material, str) else "<binary>") for k in self._verify_keys]

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
            "kid": self._signing_kid,
        }
        if self.algorithm == "RS256":
            return jwt.encode(
                payload,
                self._signing_key.material,
                algorithm="RS256",
                headers={"kid": self._signing_kid},
            )
        return jwt.encode(payload, self._signing_key.material, algorithm=self.algorithm)

    def verify_token(self, token: str) -> TokenPayload | None:
        # Peek at the kid in unverified claims to pick the right verify key
        kid = None
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
            kid = unverified.get("kid")
        except Exception:
            kid = None

        # Find the matching verify key (or fall back to current)
        verify_key = self._resolve_verify_key(kid)

        try:
            data = jwt.decode(
                token,
                verify_key.material,
                algorithms=[self.algorithm],
                options={"verify_signature": True},
            )
            return TokenPayload(**data)
        except jwt.ExpiredSignatureError:
            logger.debug("Token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.debug(f"Invalid token: {e}")
            return None

    def _resolve_verify_key(self, kid: str | None) -> _Key:
        """Pick the verify key for `kid`. Falls back to current/only key."""
        if kid:
            for k in self._verify_keys:
                if k.kid == kid:
                    return k
        # Fall back to first verify key (handles tokens without kid)
        return self._verify_keys[0]

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


def _b64url_uint(n: int) -> str:
    """Base64url-encode a positive integer (RFC 7518 §6.3)."""
    import base64
    nbytes = (n.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(n.to_bytes(nbytes, "big")).rstrip(b"=").decode("ascii")


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