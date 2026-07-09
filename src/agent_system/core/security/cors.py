"""
CORS hardening — environment-aware allowed origins.

Production rule: NEVER use `*` wildcard with `allow_credentials=True`.
This module reads CORS_ALLOWED_ORIGINS env var (comma-separated) and
falls back to environment-appropriate defaults.

Env vars:
  ENVIRONMENT=production|staging|development (default: development)
  CORS_ALLOWED_ORIGINS=  (comma-separated full URLs; no trailing slash)
  CORS_ALLOW_CREDENTIALS=  (default true; must be true for cookies)
  CORS_MAX_AGE=  (preflight cache seconds, default 600)

Behavior by environment:

  production:
    - Only origins from CORS_ALLOWED_ORIGINS (no implicit defaults)
    - Deny `*` even if explicitly set (raises on init)
    - No localhost
    - require https:// or http://localhost (dev only)

  staging:
    - CORS_ALLOWED_ORIGINS + staging.example.com defaults

  development:
    - localhost:5173 / 127.0.0.1:5173 + CORS_ALLOWED_ORIGINS + CORS_DEV_ORIGINS
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CORSConfig:
    allowed_origins: List[str]
    allow_credentials: bool
    allow_methods: List[str]
    allow_headers: List[str]
    max_age: int
    environment: str

    def to_fastapi_kwargs(self) -> dict:
        """Return kwargs to pass to fastapi.middleware.cors.CORSMiddleware."""
        return {
            "allow_origins": self.allowed_origins,
            "allow_credentials": self.allow_credentials,
            "allow_methods": self.allow_methods,
            "allow_headers": self.allow_headers,
            "max_age": self.max_age,
        }


def _is_https_or_localhost(origin: str) -> bool:
    """Production-only validation: must be https:// or http://localhost."""
    o = origin.strip()
    if o.startswith("https://"):
        return True
    if o.startswith("http://localhost") or o.startswith("http://127.0.0.1"):
        return True
    return False


def build_cors_config(
    environment: Optional[str] = None,
    allowed_origins_env: Optional[str] = None,
    dev_origins_env: Optional[str] = None,
    allow_credentials: Optional[bool] = None,
    max_age: Optional[int] = None,
) -> CORSConfig:
    """
    Build a CORSConfig for the current environment.

    Strict in production: only explicit origins, all must be https:// or localhost.
    """
    env = (environment or os.environ.get("ENVIRONMENT", "development")).strip().lower()
    raw = (allowed_origins_env if allowed_origins_env is not None
           else os.environ.get("CORS_ALLOWED_ORIGINS", "")).strip()
    dev_raw = (dev_origins_env if dev_origins_env is not None
               else os.environ.get("CORS_DEV_ORIGINS", "")).strip()
    creds = (allow_credentials if allow_credentials is not None
             else os.environ.get("CORS_ALLOW_CREDENTIALS", "true").lower()
             in ("1", "true", "yes", "on"))
    cache_max = max_age if max_age is not None else int(os.environ.get("CORS_MAX_AGE", "600"))

    # Parse origins
    explicit = [o.strip() for o in raw.split(",") if o.strip()]
    dev_extra = [o.strip() for o in dev_raw.split(",") if o.strip()]

    if env == "production":
        # Production: no implicit defaults, no localhost unless explicitly added
        origins = explicit
        if not origins:
            logger.warning(
                "CORS in production: CORS_ALLOWED_ORIGINS is empty. "
                "All cross-origin browser requests will be rejected. "
                "Set CORS_ALLOWED_ORIGINS=https://app.example.com,https://admin.example.com"
            )
        # Deny wildcard in production (incompatible with credentials anyway)
        if "*" in origins:
            raise ValueError(
                "CORS wildcard `*` is not allowed in production. "
                "Set CORS_ALLOWED_ORIGINS to explicit https:// origins."
            )
        # Validate that all origins are https:// or localhost
        bad = [o for o in origins if not _is_https_or_localhost(o)]
        if bad:
            raise ValueError(
                f"CORS origins must be https:// or localhost in production; got: {bad}"
            )

    elif env == "staging":
        origins = list(explicit) + [
            "https://staging.example.com",
        ]
        # If CORS_ALLOWED_ORIGINS empty, add common staging defaults
        if not explicit:
            logger.info("CORS in staging with no explicit origins; using staging.example.com default")
        origins.extend(dev_extra)

    else:  # development
        origins = list(explicit)
        if not origins:
            origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
        origins.extend(dev_extra)

    # Dedupe (preserving order)
    seen = set()
    deduped = []
    for o in origins:
        if o not in seen:
            seen.add(o)
            deduped.append(o)

    return CORSConfig(
        allowed_origins=deduped,
        allow_credentials=creds,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Idempotency-Key"],
        max_age=cache_max,
        environment=env,
    )


def is_origin_allowed(origin: str, config: CORSConfig) -> bool:
    """Test if a specific origin would be allowed (for tests / debugging)."""
    return origin in config.allowed_origins
