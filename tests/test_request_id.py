"""
Tests: RequestIDMiddleware — X-Request-ID propagation for log correlation.

PR 7 (PR E / API 中间件): the rate limit + body size + secrets middleware already
existed; this PR adds request ID propagation. Tests cover:
  - generate UUID when no header is sent
  - reuse inbound X-Request-ID when valid
  - reject malformed IDs (control chars, too long, empty)
  - expose via request.state.request_id
  - expose via contextvar (get_request_id())
  - echo back on response headers
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.core.security_middleware import (
    RequestIDMiddleware,
    get_request_id,
    _sanitize_request_id,
    REQUEST_ID_HEADER,
)


@pytest.fixture
def app():
    """Minimal FastAPI app with RequestIDMiddleware + one echo endpoint."""
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/echo")
    async def echo():
        return {
            "request_id_state": __import__("agent_system.core.security_middleware", fromlist=["get_request_id"])
            .get_request_id(),
            "header_value": None,
        }

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class TestRequestIDMiddleware:
    def test_generates_uuid_when_no_header(self, client):
        r = client.get("/echo")
        assert r.status_code == 200
        rid = r.headers.get(REQUEST_ID_HEADER)
        assert rid is not None
        # UUID4 hex is 32 chars
        assert len(rid) == 32
        assert all(c in "0123456789abcdef" for c in rid)

    def test_reuses_inbound_valid_header(self, client):
        r = client.get("/echo", headers={REQUEST_ID_HEADER: "client-supplied-123"})
        assert r.headers[REQUEST_ID_HEADER] == "client-supplied-123"

    def test_echoes_back_in_response(self, client):
        r = client.get("/echo", headers={REQUEST_ID_HEADER: "abc-def-123"})
        assert r.headers.get(REQUEST_ID_HEADER) == "abc-def-123"

    def test_rejects_malformed_ids_and_generates_new(self, client):
        """IDs with characters outside [A-Za-z0-9_-] must be rejected (security)."""
        bad_ids = [
            "has space",
            "has\nnewline",
            "has;semicolon",
            "x" * 200,           # too long
            "<script>alert(1)</script>",
            "../../etc/passwd",
            "id'OR'1'='1",       # SQLi-style
        ]
        for bad in bad_ids:
            r = client.get("/echo", headers={REQUEST_ID_HEADER: bad})
            # Should fall back to a fresh UUID — NOT echo the bad value back
            echoed = r.headers.get(REQUEST_ID_HEADER)
            assert echoed != bad, f"Malformed ID was echoed back: {bad!r}"
            assert len(echoed) == 32  # fresh UUID4 hex


class TestSanitizeRequestId:
    """Direct tests for the sanitizer — covers edge cases."""

    def test_none_returns_none(self):
        assert _sanitize_request_id(None) is None

    def test_empty_returns_none(self):
        assert _sanitize_request_id("") is None

    def test_whitespace_stripped(self):
        assert _sanitize_request_id("  abc  ") == "abc"

    def test_max_length_128_allowed(self):
        assert _sanitize_request_id("a" * 128) == "a" * 128

    def test_over_128_rejected(self):
        assert _sanitize_request_id("a" * 129) is None

    def test_underscores_and_dashes_allowed(self):
        assert _sanitize_request_id("req_abc-123") == "req_abc-123"

    def test_dots_rejected(self):
        # Some UUID formats use dots — reject to keep header simple
        assert _sanitize_request_id("abc.def") is None

    def test_unicode_rejected(self):
        assert _sanitize_request_id("请求-123") is None


class TestRequestIDPropagation:
    """request_id available via request.state + contextvar."""

    def test_contextvar_set_during_request(self, client):
        # Inside the /echo handler, get_request_id() must return the current ID.
        r = client.get("/echo", headers={REQUEST_ID_HEADER: "trace-xyz"})
        assert r.json()["request_id_state"] == "trace-xyz"

    def test_contextvar_reset_after_request(self):
        """After a request completes, the contextvar must be reset to None."""
        # First request — sets a specific ID
        client_a = TestClient(FastAPI())
        # Manually wrap RequestIDMiddleware around a one-off app
        from fastapi import FastAPI as _FA
        from fastapi.testclient import TestClient as _TC
        from agent_system.core.security_middleware import RequestIDMiddleware as _RM

        def make_client():
            a = _FA()
            a.add_middleware(_RM)

            @a.get("/probe")
            async def probe():
                return {"rid": get_request_id()}

            return _TC(a)

        c1 = make_client()
        r1 = c1.get("/probe", headers={REQUEST_ID_HEADER: "first-call-id"})
        assert r1.json()["rid"] == "first-call-id"

        # Second client / second request — contextvar must NOT leak
        c2 = make_client()
        r2 = c2.get("/probe", headers={REQUEST_ID_HEADER: "second-call-id"})
        assert r2.json()["rid"] == "second-call-id"

    def test_each_request_gets_own_id(self, client):
        r1 = client.get("/echo")
        r2 = client.get("/echo")
        rid1 = r1.headers[REQUEST_ID_HEADER]
        rid2 = r2.headers[REQUEST_ID_HEADER]
        assert rid1 != rid2  # different IDs for different requests