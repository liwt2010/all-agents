"""
Rate limit registry — composes per-(scope, dimension) limiters on top
of a pluggable backend.

Provides:
- Per-scope limits (default / expensive / heavy / auth)
- Composed user + IP + global checks
- env var configuration
- Pluggable backend (in-memory or Redis) for single-replica or
  multi-replica deployments
"""

import logging
import os
from dataclasses import dataclass

from agent_system.core.rate_limit.sliding_window import LimitDecision
from agent_system.core.rate_limit.backend import (
    InMemoryBackend,
    RateLimiterBackend,
    create_backend_from_env,
)

logger = logging.getLogger(__name__)


@dataclass
class ScopeConfig:
    """Limits for one scope category."""
    user_limit: int
    ip_limit: int
    user_window_seconds: float = 60.0
    ip_window_seconds: float = 60.0


# Default scope configurations
DEFAULT_SCOPES: dict[str, ScopeConfig] = {
    # Read-mostly endpoints
    "default": ScopeConfig(user_limit=120, ip_limit=240),
    # LLM-calling endpoints — strict
    "expensive": ScopeConfig(user_limit=20, ip_limit=60),
    # Admin / audit — very strict
    "heavy": ScopeConfig(user_limit=10, ip_limit=30),
    # Auth — anti-brute-force
    "auth": ScopeConfig(user_limit=5, ip_limit=30),
}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"Invalid int env var {name}={raw!r}, using default {default}")
        return default


def load_scope_config_from_env() -> dict[str, ScopeConfig]:
    """Build scope configs from environment variables (with defaults)."""
    return {
        "default": ScopeConfig(
            user_limit=_int_env("AGENT_RATE_LIMIT_SCOPE_DEFAULT_USER", 120),
            ip_limit=_int_env("AGENT_RATE_LIMIT_SCOPE_DEFAULT_IP", 240),
        ),
        "expensive": ScopeConfig(
            user_limit=_int_env("AGENT_RATE_LIMIT_SCOPE_EXPENSIVE_USER", 20),
            ip_limit=_int_env("AGENT_RATE_LIMIT_SCOPE_EXPENSIVE_IP", 60),
        ),
        "heavy": ScopeConfig(
            user_limit=_int_env("AGENT_RATE_LIMIT_SCOPE_HEAVY_USER", 10),
            ip_limit=_int_env("AGENT_RATE_LIMIT_SCOPE_HEAVY_IP", 30),
        ),
        "auth": ScopeConfig(
            user_limit=_int_env("AGENT_RATE_LIMIT_SCOPE_AUTH_USER", 5),
            ip_limit=_int_env("AGENT_RATE_LIMIT_SCOPE_AUTH_IP", 30),
        ),
    }


def classify_scope(path: str) -> str:
    """
    Map a request path to its rate-limit scope.

    Order matters — more specific paths first.
    """
    if path.startswith("/api/auth/"):
        return "auth"
    if path.startswith("/api/admin/") or path.startswith("/api/audit/"):
        return "heavy"
    if (
        path.startswith("/api/tasks")
        or path.startswith("/api/discussions")
        or path.startswith("/api/custom-agents")
    ):
        return "expensive"
    return "default"


class LimiterRegistry:
    """
    Holds one logical limiter per (scope, dimension) pair.

    Dimensions:
      - user:{user_id}:{scope}
      - ip:{ip}:{scope}

    All check/peek calls go through the shared `backend`. The registry
    just owns the scope→limit mapping and the dimension key format.
    """

    def __init__(
        self,
        scopes: dict[str, ScopeConfig] | None = None,
        backend: RateLimiterBackend | None = None,
    ):
        self.scopes = scopes or load_scope_config_from_env()
        self.backend: RateLimiterBackend = backend or InMemoryBackend()

    def _user_cfg(self, scope: str) -> ScopeConfig:
        return self.scopes.get(scope, self.scopes["default"])

    async def check_request(
        self, user_id: str | None, ip: str, scope: str
    ) -> tuple[bool, LimitDecision, str]:
        """
        Check all applicable limiters. Returns (allowed, decision, dimension).

        If user_id is provided, both user and IP limiters must pass.
        If user_id is None, only IP limiter applies.
        """
        if user_id:
            cfg = self._user_cfg(scope)
            user_decision = await self.backend.check(
                f"user:{user_id}",
                limit=cfg.user_limit,
                window_seconds=cfg.user_window_seconds,
                scope=scope,
            )
            if not user_decision.allowed:
                return False, user_decision, "user"

        cfg = self._user_cfg(scope)
        ip_decision = await self.backend.check(
            f"ip:{ip}",
            limit=cfg.ip_limit,
            window_seconds=cfg.ip_window_seconds,
            scope=scope,
        )
        if not ip_decision.allowed:
            return False, ip_decision, "ip"

        # All passed — return the most restrictive remaining count
        if user_id:
            user_decision = await self.backend.peek(
                f"user:{user_id}",
                limit=cfg.user_limit,
                window_seconds=cfg.user_window_seconds,
                scope=scope,
            )
            return True, ip_decision, "all" if user_decision.remaining > ip_decision.remaining else "user"
        return True, ip_decision, "ip"

    async def reset_all(self) -> None:
        await self.backend.reset(None)


# Global singleton (lazy + env-driven)
_registry: LimiterRegistry | None = None


def get_limiter_registry() -> LimiterRegistry:
    global _registry
    if _registry is None:
        # Lazy backend init: avoid connecting to Redis at import time so
        # tests and dev runs without REDIS_URL still work.
        backend = InMemoryBackend()
        _registry = LimiterRegistry(backend=backend)
    return _registry


async def init_limiter_registry() -> LimiterRegistry:
    """Async init: probe REDIS_URL and pick the right backend.

    Call once at server startup. After this, `get_limiter_registry()`
    returns the same registry with the chosen backend.
    """
    global _registry
    if _registry is None:
        backend = await create_backend_from_env()
        _registry = LimiterRegistry(backend=backend)
    return _registry


def reset_limiter_registry() -> None:
    global _registry
    _registry = None