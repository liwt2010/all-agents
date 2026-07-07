"""
Security middleware — production hardening

  - Security headers (CSP, HSTS, X-Frame-Options, X-Content-Type-Options)
  - Rate limiting per IP (token bucket)
  - Request body size limit
  - Generic error responses (no internal details leaked)
  - Secrets detection in inputs
  - Request ID propagation (X-Request-ID) — for log correlation
"""

import asyncio
import logging
import re
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Context var so downstream code (logging, audit, events) can attach the same request_id
# without threading it through every function signature.
_request_id_var: ContextVar[Optional[str]] = ContextVar("_request_id_var", default=None)


def get_request_id() -> Optional[str]:
    """Return the current request's ID, or None outside a request context."""
    return _request_id_var.get()


# ── Security headers ──

DEFAULT_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "X-XSS-Protection": "1; mode=block",
}

# HSTS — only enable in production (when behind HTTPS)
PRODUCTION_SECURITY_HEADERS = {
    **DEFAULT_SECURITY_HEADERS,
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


# ── Rate limiter (per IP, sliding window) ──

@dataclass
class _Bucket:
    """Token bucket for rate limiting."""
    tokens: float
    last_refill: float


class RateLimiter:
    """
    Per-IP rate limiter using a token bucket.
    Default: 60 requests / 60 seconds per IP.
    """

    def __init__(self, rate: int = 60, window: int = 60, burst: Optional[int] = None):
        self.rate = float(rate)
        self.window = float(window)
        self.burst = float(burst) if burst else float(rate)
        self._buckets: Dict[str, _Bucket] = defaultdict(
            lambda: _Bucket(tokens=self.burst, last_refill=time.time())
        )
        self._cleanup_interval = 300.0
        self._last_cleanup = time.time()

    def _refill(self, bucket: _Bucket) -> None:
        now = time.time()
        elapsed = now - bucket.last_refill
        refill_rate = self.burst / self.window
        bucket.tokens = min(self.burst, bucket.tokens + elapsed * refill_rate)
        bucket.last_refill = now

    def _maybe_cleanup(self) -> None:
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        # Drop stale entries
        stale = []
        for ip, b in self._buckets.items():
            if now - b.last_refill > self.window * 2:
                stale.append(ip)
        for ip in stale:
            del self._buckets[ip]

    def check(self, ip: str) -> bool:
        """Check if request is allowed. False = rate-limited."""
        self._maybe_cleanup()
        bucket = self._buckets[ip]
        self._refill(bucket)
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return True
        return False

    def reset(self, ip: Optional[str] = None) -> None:
        if ip:
            self._buckets.pop(ip, None)
        else:
            self._buckets.clear()


# ── Secrets detection ──

import re as _re
_SECRETS_PATTERNS_COMPILED = [
    (_re.compile(r"sk-[a-zA-Z0-9]{20,}"), "Anthropic/OpenAI-style key"),
    (_re.compile(r"sk-ant-[a-zA-Z0-9-]{20,}"), "Anthropic admin key"),
    (_re.compile(r"sk-proj-[a-zA-Z0-9]{20,}"), "OpenAI project key"),
    (_re.compile(r"ghp_[a-zA-Z0-9]{20,}"), "GitHub PAT"),
    (_re.compile(r"gho_[a-zA-Z0-9]{20,}"), "GitHub OAuth"),
    (_re.compile(r"github_pat_[a-zA-Z0-9_]{20,}"), "GitHub fine-grained PAT"),
    (_re.compile(r"xoxb-[a-zA-Z0-9-]+"), "Slack bot token"),
    (_re.compile(r"xoxp-[a-zA-Z0-9-]+"), "Slack user token"),
    (_re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (_re.compile(r"AIza[0-9A-Za-z_-]{35}"), "Google API key"),
    (_re.compile(r"ya29\.[0-9A-Za-z_-]+"), "Google OAuth token"),
    (_re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"), "Private key"),
    (_re.compile(r"eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+"), "JWT token"),
    (_re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key ID"),
    (_re.compile(r"ASIA[0-9A-Z]{16}"), "AWS STS token"),
    (_re.compile(r"AIza[0-9A-Za-z_-]{35}"), "Google API key"),
    (_re.compile(r"ya29\.[0-9A-Za-z_-]+"), "Google OAuth"),
]
SECRETS_PATTERNS = [(p.pattern, n) for p, n in _SECRETS_PATTERNS_COMPILED]

SECRETS_CHECK_TIMEOUT = 2.0  # seconds


def scan_for_secrets(text: str) -> list:
    """
    Scan text for known secret patterns using pre-compiled regex.
    Empty list = no secrets detected.
    """
    if not text or len(text) > 100_000:
        return []
    matches = []
    for compiled, name in _SECRETS_PATTERNS_COMPILED:
        try:
            for m in compiled.finditer(text):
                matches.append((name, m.group(0)[:8] + "..."))
        except Exception:
            continue
    return matches


# ── Middleware ──

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every response."""

    def __init__(self, app, is_production: bool = False):
        super().__init__(app)
        self.headers = PRODUCTION_SECURITY_HEADERS if is_production else DEFAULT_SECURITY_HEADERS

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        for k, v in self.headers.items():
            if k not in response.headers:
                response.headers[k] = v
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP rate limiting using a token bucket."""

    def __init__(self, app, rate_per_minute: int = 60, exempt_paths: Optional[list] = None):
        super().__init__(app)
        self.limiter = RateLimiter(rate=rate_per_minute, window=60)
        self.exempt_paths = set(exempt_paths or ["/api/health", "/api/ready"])

    async def dispatch(self, request, call_next):
        if request.url.path in self.exempt_paths:
            return await call_next(request)

        ip = request.client.host if request.client else "unknown"
        if not self.limiter.check(ip):
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "message": "Too many requests. Please retry later.",
                },
                headers={"Retry-After": "60"},
            )
        return await call_next(request)


class RequestSizeLimitMiddleware:
    """Reject requests with body > max_bytes (default 1 MB).
    Uses raw ASGI so we don't interfere with body reading."""

    def __init__(self, app, max_bytes: int = 1_048_576):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Check Content-Length header
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > self.max_bytes:
                        response = JSONResponse(
                            status_code=413,
                            content={"error": "payload_too_large",
                                     "message": f"Request body exceeds {self.max_bytes} bytes"},
                        )
                        await response(scope, receive, send)
                        return
                except ValueError:
                    pass
                break
        await self.app(scope, receive, send)


class SecretsInRequestMiddleware:
    """Reject requests whose body contains known secret patterns.

    Uses raw ASGI (NOT BaseHTTPMiddleware) so we can buffer the body
    and replay it for downstream handlers without losing data.
    """

    def __init__(self, app, exempt_paths: Optional[list] = None):
        self.app = app
        self.exempt_paths = set(exempt_paths or ["/api/health", "/api/ready"])

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path", "") in self.exempt_paths:
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        if method not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return

        # Drain body into buffer
        body = b""
        more = True
        while more:
            msg = await receive()
            if msg["type"] == "http.request":
                body += msg.get("body", b"")
                more = msg.get("more_body", False)
            elif msg["type"] == "http.disconnect":
                break

        if not body:
            await self.app(scope, _passthrough_receive(body), send)
            return

        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:
            await self.app(scope, _passthrough_receive(body), send)
            return

        loop = asyncio.get_event_loop()
        try:
            matches = await asyncio.wait_for(
                loop.run_in_executor(None, scan_for_secrets, text),
                timeout=SECRETS_CHECK_TIMEOUT,
            )
        except asyncio.TimeoutError:
            matches = []

        if matches:
            logger.warning(f"Secrets detected in {method} {scope.get('path', '')}")
            response = JSONResponse(
                status_code=400,
                content={
                    "error": "secrets_detected",
                    "message": "Request body contains what appears to be credentials. "
                               "Please remove secrets and use environment variables.",
                    "matches": [name for name, _ in matches],
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, _passthrough_receive(body), send)


def _passthrough_receive(body: bytes):
    """Create a receive() callable that replays the buffered body once."""
    state = {"sent": False}
    async def receive():
        if state["sent"]:
            return {"type": "http.disconnect"}
        state["sent"] = True
        return {"type": "http.request", "body": body, "more_body": False}
    return receive


# ── Generic exception handler ──

def mask_internal_error(exc: Exception) -> Dict[str, Any]:
    """
    Return a safe error response body. Never leak stack traces,
    SQL fragments, or file paths.
    """
    return {
        "error": "internal_error",
        "message": "An unexpected error occurred. Please contact support.",
    }


# ── Request ID propagation ──

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_MAX_LEN = 128
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")


def _sanitize_request_id(value: Optional[str]) -> Optional[str]:
    """Validate an incoming X-Request-ID. Reject anything not [A-Za-z0-9_-]{<=128}."""
    if value is None:
        return None
    value = value.strip()
    if not value or len(value) > _REQUEST_ID_MAX_LEN:
        return None
    if not _REQUEST_ID_PATTERN.match(value):
        return None
    return value


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Assign / propagate an X-Request-ID for each request.

    Behavior:
      - If the client sends X-Request-ID and it passes sanitization, reuse it.
      - Otherwise generate a fresh UUID4.
      - Expose it as `request.state.request_id` and a contextvar (`get_request_id()`)
        so logs/audit/events can correlate.
      - Echo it back on the response as X-Request-ID.

    Skips for non-HTTP (e.g. WebSocket upgrade) traffic.
    """

    async def dispatch(self, request: Request, call_next):
        incoming = _sanitize_request_id(request.headers.get(REQUEST_ID_HEADER))
        request_id = incoming or uuid.uuid4().hex
        request.state.request_id = request_id
        token = _request_id_var.set(request_id)
        try:
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            _request_id_var.reset(token)
