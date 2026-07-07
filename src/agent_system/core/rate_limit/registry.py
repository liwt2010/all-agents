"""
Rate limit registry — composes multiple SlidingWindowLimiters keyed by scope.

Provides:
- Per-scope limits (default / expensive / heavy / auth)
- Composed user + IP + global checks
- env var configuration
"""

import logging
import os
from dataclasses import dataclass
from typing import Dict, Optional

from agent_system.core.rate_limit.sliding_window import (
    LimitDecision,
    SlidingWindowLimiter,
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
DEFAULT_SCOPES: Dict[str, ScopeConfig] = {
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


def load_scope_config_from_env() -> Dict[str, ScopeConfig]:
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
    Holds one SlidingWindowLimiter per (scope, dimension) pair.

    Dimensions:
      - user:{user_id}:{scope}
      - ip:{ip}:{scope}
    """

    def __init__(self, scopes: Optional[Dict[str, ScopeConfig]] = None):
        self.scopes = scopes or load_scope_config_from_env()
        self._user_limiters: Dict[str, SlidingWindowLimiter] = {}
        self._ip_limiters: Dict[str, SlidingWindowLimiter] = {}

    def _get_user_limiter(self, scope: str) -> SlidingWindowLimiter:
        if scope not in self._user_limiters:
            cfg = self.scopes.get(scope, self.scopes["default"])
            self._user_limiters[scope] = SlidingWindowLimiter(
                limit=cfg.user_limit,
                window_seconds=cfg.user_window_seconds,
                scope=scope,
            )
        return self._user_limiters[scope]

    def _get_ip_limiter(self, scope: str) -> SlidingWindowLimiter:
        if scope not in self._ip_limiters:
            cfg = self.scopes.get(scope, self.scopes["default"])
            self._ip_limiters[scope] = SlidingWindowLimiter(
                limit=cfg.ip_limit,
                window_seconds=cfg.ip_window_seconds,
                scope=scope,
            )
        return self._ip_limiters[scope]

    def check_request(
        self, user_id: Optional[str], ip: str, scope: str
    ) -> tuple[bool, LimitDecision, str]:
        """
        Check all applicable limiters. Returns (allowed, decision, dimension).

        If user_id is provided, both user and IP limiters must pass.
        If user_id is None, only IP limiter applies.
        """
        if user_id:
            user_limiter = self._get_user_limiter(scope)
            user_decision = user_limiter.check(f"user:{user_id}")
            if not user_decision.allowed:
                return False, user_decision, "user"

        ip_limiter = self._get_ip_limiter(scope)
        ip_decision = ip_limiter.check(f"ip:{ip}")
        if not ip_decision.allowed:
            return False, ip_decision, "ip"

        # All passed — return the most restrictive remaining count
        if user_id:
            user_limiter = self._get_user_limiter(scope)
            user_decision = user_limiter.peek(f"user:{user_id}")
            return True, ip_decision, "all" if user_decision.remaining > ip_decision.remaining else "user"
        return True, ip_decision, "ip"

    def reset_all(self) -> None:
        for lim in self._user_limiters.values():
            lim.reset()
        for lim in self._ip_limiters.values():
            lim.reset()


# Global singleton
_registry: Optional[LimiterRegistry] = None


def get_limiter_registry() -> LimiterRegistry:
    global _registry
    if _registry is None:
        _registry = LimiterRegistry()
    return _registry


def reset_limiter_registry() -> None:
    global _registry
    _registry = None