"""
Rate limiting — sliding window + per-user/per-scope composition (PR-12).

Public API:
- SlidingWindowLimiter — single-bucket limiter
- LimiterRegistry — composed multi-bucket check
- classify_scope — path → scope name
- get_limiter_registry — global singleton
"""

from agent_system.core.rate_limit.sliding_window import (
    LimitDecision,
    SlidingWindowLimiter,
)
from agent_system.core.rate_limit.registry import (
    LimiterRegistry,
    ScopeConfig,
    DEFAULT_SCOPES,
    classify_scope,
    get_limiter_registry,
    reset_limiter_registry,
    load_scope_config_from_env,
)

__all__ = [
    "LimitDecision",
    "SlidingWindowLimiter",
    "LimiterRegistry",
    "ScopeConfig",
    "DEFAULT_SCOPES",
    "classify_scope",
    "get_limiter_registry",
    "reset_limiter_registry",
    "load_scope_config_from_env",
]