"""
GitHub App webhook tests (PR v0.3.0).

Verifies:
  - HMAC-SHA256 signature verification (constant-time, valid/invalid)
  - Replay protection: same delivery_id returns 200 duplicate
  - Missing/invalid signature returns 401
  - pull_request events with action=opened/synchronize/reopened are queued
  - other actions and other event types return 200 ignored
  - Endpoint returns 503 when GITHUB_WEBHOOK_SECRET not configured
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest


SECRET = "test-github-webhook-secret"


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)


def _sign(body: bytes, secret: str = SECRET) -> str:
    """Compute the X-Hub-Signature-256 value for the given body."""
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()


def _post(client, body: dict, *, event="pull_request", delivery="d-1",
          sig_secret=SECRET, omit_sig=False, wrong_sig=False):
    raw = json.dumps(body).encode("utf-8")
    sig = "" if omit_sig else _sign(raw, sig_secret)
    if wrong_sig:
        sig = _sign(raw, "wrong-secret")
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
    }
    if sig:
        headers["X-Hub-Signature-256"] = sig
    return client.post("/api/webhooks/github", content=raw, headers=headers)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from agent_system.api.server import app
    return TestClient(app)


# ── Signature verification ──

class TestSignatureVerification:
    def test_valid_signature_accepted(self, client):
        body = {
            "action": "opened",
            "pull_request": {"number": 1, "title": "T", "body": "B", "html_url": "u"},
            "repository": {"full_name": "o/r"},
        }
        r = _post(client, body, event="push", delivery="d-sig-1")
        assert r.status_code == 200

    def test_missing_signature_returns_401(self, client):
        body = {"action": "opened", "pull_request": {}, "repository": {}}
        r = _post(client, body, delivery="d-sig-2", omit_sig=True)
        assert r.status_code == 401

    def test_wrong_signature_returns_401(self, client):
        body = {"action": "opened", "pull_request": {}, "repository": {}}
        r = _post(client, body, delivery="d-sig-3", wrong_sig=True)
        assert r.status_code == 401

    def test_missing_delivery_returns_400(self, client):
        body = {"action": "opened"}
        raw = json.dumps(body).encode("utf-8")
        r = client.post(
            "/api/webhooks/github",
            content=raw,
            headers={
                "X-GitHub-Event": "pull_request",
                # no X-GitHub-Delivery
                "X-Hub-Signature-256": _sign(raw),
            },
        )
        assert r.status_code == 400


# ── Secret configuration ──

class TestSecretRequired:
    def test_missing_secret_returns_503(self, client, monkeypatch):
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET")
        body = {"action": "opened"}
        raw = json.dumps(body).encode("utf-8")
        r = client.post(
            "/api/webhooks/github",
            content=raw,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "d-no-secret",
                "X-Hub-Signature-256": _sign(raw),
            },
        )
        assert r.status_code == 503


# ── Replay protection ──

class TestReplayProtection:
    def test_same_delivery_id_returns_duplicate(self, client):
        body = {"zen": "hello"}  # ping event
        r1 = _post(client, body, event="ping", delivery="d-replay-1")
        r2 = _post(client, body, event="ping", delivery="d-replay-1")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["status"] != "duplicate"
        assert r2.json()["status"] == "duplicate"


# ── Event dispatch ──

class TestEventDispatch:
    def _pr_body(self, action="opened", number=42):
        return {
            "action": action,
            "pull_request": {
                "number": number,
                "title": "Add login screen",
                "body": "Implements OAuth flow",
                "html_url": f"https://github.com/o/r/pull/{number}",
            },
            "repository": {"full_name": "o/r"},
            "sender": {"login": "alice"},
        }

    @pytest.mark.parametrize("action", ["opened", "synchronize", "reopened"])
    def test_pr_action_queued(self, client, action):
        r = _post(client, self._pr_body(action=action), delivery=f"d-{action}")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "queued"
        assert body["action"] == action

    @pytest.mark.parametrize("action", ["closed", "edited", "assigned"])
    def test_pr_other_action_ignored(self, client, action):
        r = _post(client, self._pr_body(action=action), delivery=f"d-other-{action}")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ignored"
        assert body["action"] == action

    def test_push_event_ignored(self, client):
        body = {"ref": "refs/heads/main", "commits": []}
        r = _post(client, body, event="push", delivery="d-push")
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"

    def test_ping_event_ignored(self, client):
        body = {"zen": "Speak like a human"}
        r = _post(client, body, event="ping", delivery="d-ping")
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"


# ── HMAC helper ──

class TestVerifySignature:
    def test_roundtrip(self):
        from agent_system.api.routes.github_webhook import verify_signature
        body = b'{"hello":"world"}'
        sig = _sign(body)
        assert verify_signature(SECRET, body, sig) is True

    def test_tampered_body_fails(self):
        from agent_system.api.routes.github_webhook import verify_signature
        body = b'{"hello":"world"}'
        sig = _sign(body)
        assert verify_signature(SECRET, body + b"!", sig) is False

    def test_missing_algorithm_prefix_fails(self):
        from agent_system.api.routes.github_webhook import verify_signature
        body = b"x"
        assert verify_signature(SECRET, body, "no-sha256-prefix") is False

    def test_none_signature_fails(self):
        from agent_system.api.routes.github_webhook import verify_signature
        assert verify_signature(SECRET, b"x", None) is False