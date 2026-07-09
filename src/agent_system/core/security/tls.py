"""
TLS / HTTPS enforcement middleware.

Three components, each independently toggleable:

  1. HTTPSRedirectMiddleware — redirects HTTP→HTTPS (status 301)
     - Off by default; enable when not behind a TLS-terminating LB
     - Configurable via TLS_REDIRECT_ENABLED=true

  2. HSTSHeaderMiddleware — adds Strict-Transport-Security
     - On by default in production
     - max-age=31536000; includeSubDomains; preload

  3. SecureCookieChecker — middleware that flags cookies missing Secure flag
     - Off by default; enable in production to catch regressions
     - Returns 500 on offending cookies (or logs warning if WARN_ONLY)

Env vars:
  TLS_REDIRECT_ENABLED=         (default false; set true when not behind LB)
  TLS_HSTS_ENABLED=             (default true in production)
  TLS_HSTS_MAX_AGE=             (default 31536000 = 1 year)
  TLS_HSTS_INCLUDE_SUBDOMAINS=  (default true)
  TLS_HSTS_PRELOAD=             (default false; submit to hstspreload.org only when ready)
  TLS_SECURE_COOKIES=           (default false; enable to enforce)
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def is_production() -> bool:
    return os.environ.get("ENVIRONMENT", "development").strip().lower() == "production"


# ── HTTPS redirect ────────────────────────────────────────────────


class HTTPSRedirectMiddleware:
    """
    Redirect HTTP requests to HTTPS. Returns 301 Moved Permanently.

    Pure ASGI middleware to avoid BaseHTTPMiddleware's known issues
    with streaming responses.

    Bypassed when:
      - TLS_REDIRECT_ENABLED is not "true" / "1"
      - Request already came in as https (check x-forwarded-proto)
      - Request is to /health (LB health checks use HTTP)
    """

    def __init__(self, app):
        self.app = app
        self.enabled = os.environ.get("TLS_REDIRECT_ENABLED", "false").lower() in ("1", "true", "yes")

    async def __call__(self, scope, receive, send):
        if not self.enabled:
            await self.app(scope, receive, send)
            return

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Don't redirect health checks (LB probes)
        path = scope.get("path", "/")
        if path in ("/api/health", "/api/ready", "/health", "/ready"):
            await self.app(scope, receive, send)
            return

        # Check for https via x-forwarded-proto (set by LB) or scheme
        forwarded_proto = None
        for k, v in scope.get("headers", []):
            if k == b"x-forwarded-proto":
                forwarded_proto = v.decode("latin-1").lower()
                break
        scheme = (forwarded_proto or scope.get("scheme", "http")).lower()
        if scheme == "https":
            await self.app(scope, receive, send)
            return

        # Redirect to https
        host = None
        for k, v in scope.get("headers", []):
            if k == b"host":
                host = v.decode("latin-1")
                break
        host = host or "localhost"
        new_url = f"https://{host}{path}"
        if scope.get("query_string"):
            new_url += "?" + scope["query_string"].decode("latin-1")
        logger.info(f"TLS redirect: {scheme}://{host}{path} -> https://{host}{path}")
        await send({
            "type": "http.response.start",
            "status": 301,
            "headers": [(b"location", new_url.encode("latin-1"))],
        })
        await send({"type": "http.response.body", "body": b""})


# ── HSTS ────────────────────────────────────────────────────────────


class HSTSHeaderMiddleware:
    """
    Add Strict-Transport-Security header to all responses.
    """

    def __init__(self, app):
        self.app = app
        # Default on in production, off elsewhere
        default = "true" if is_production() else "false"
        self.enabled = os.environ.get("TLS_HSTS_ENABLED", default).lower() in ("1", "true", "yes")
        self.max_age = int(os.environ.get("TLS_HSTS_MAX_AGE", "31536000"))
        self.include_subdomains = os.environ.get("TLS_HSTS_INCLUDE_SUBDOMAINS", "true").lower() in ("1", "true", "yes")
        self.preload = os.environ.get("TLS_HSTS_PRELOAD", "false").lower() in ("1", "true", "yes")
        self._hsts_value = self._build_value()

    def _build_value(self) -> str:
        parts = [f"max-age={self.max_age}"]
        if self.include_subdomains:
            parts.append("includeSubDomains")
        if self.preload:
            parts.append("preload")
        return "; ".join(parts)

    async def __call__(self, scope, receive, send):
        if not self.enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                # Remove any existing HSTS to avoid duplicates
                headers = [(k, v) for k, v in headers if k.lower() != b"strict-transport-security"]
                headers.append((b"strict-transport-security", self._hsts_value.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ── Secure cookie enforcement ──────────────────────────────────────


class SecureCookieChecker:
    """
    Check Set-Cookie headers for the Secure flag. Logs warnings, optionally 500s.

    Off by default. Enable in production to catch cookie regressions.
    """

    def __init__(self, app):
        self.app = app
        self.enabled = os.environ.get("TLS_SECURE_COOKIES", "false").lower() in ("1", "true", "yes")
        self.warn_only = os.environ.get("TLS_SECURE_COOKIES_WARN_ONLY", "true").lower() in ("1", "true", "yes")

    async def __call__(self, scope, receive, send):
        if not self.enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                for k, v in message.get("headers", []):
                    if k.lower() == b"set-cookie":
                        cookie_str = v.decode("latin-1").lower()
                        if "secure" not in cookie_str:
                            msg = f"Cookie missing Secure flag: {cookie_str[:80]}"
                            if self.warn_only:
                                logger.warning(msg)
                            else:
                                logger.error(msg)
                                # Hard fail
                                message["status"] = 500
                                message["headers"] = [
                                    (b"content-type", b"application/json"),
                                ]
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ── Helper: add all to a FastAPI app ──────────────────────────────


def install_tls_middlewares(app):
    """
    Install all TLS-related middlewares in the correct order.
    Order: HTTPS redirect (outermost) -> HSTS -> SecureCookie (innermost)
    """
    app.add_middleware(SecureCookieChecker)
    app.add_middleware(HSTSHeaderMiddleware)
    app.add_middleware(HTTPSRedirectMiddleware)
