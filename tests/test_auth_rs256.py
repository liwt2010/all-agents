"""
RS256 JWT + JWKS endpoint tests (PR-RS256).

Verifies:
  - Algorithm auto-selection: AUTH_PRIVATE_KEY -> RS256; otherwise HS256
  - RS256 sign + verify round-trip
  - External verifier (PyJWT + raw public key PEM) can verify our tokens
  - JWKS endpoint returns valid RFC 7517 document with all public keys
  - JWKS for HS256 returns empty key set (no secret leakage)
  - Key rotation: tokens signed by old private key still verify via
    a retained public key entry in AUTH_PUBLIC_KEYS
  - Existing HS256 path is untouched (back-compat with v0.1.x)
"""
from __future__ import annotations

import base64
import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import jwt as pyjwt

from agent_system.core.auth.jwt import AuthService
from agent_system.api.state import get_auth_service_singleton


# ── Helpers ──

@pytest.fixture
def rsa_keypair():
    """Generate a fresh RSA keypair for one test."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return priv_pem, pub_pem, priv.public_key()


@pytest.fixture
def second_keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return priv_pem, pub_pem


# ── Algorithm auto-selection ──

class TestAlgorithmSelection:
    def test_hs256_default(self, monkeypatch):
        monkeypatch.delenv("AUTH_PRIVATE_KEY", raising=False)
        monkeypatch.setenv("AUTH_SECRET", "a" * 32)
        svc = AuthService()
        assert svc.algorithm == "HS256"

    def test_rs256_when_private_key_set(self, monkeypatch, rsa_keypair):
        priv_pem, _, _ = rsa_keypair
        monkeypatch.setenv("AUTH_PRIVATE_KEY", priv_pem)
        monkeypatch.delenv("AUTH_PUBLIC_KEYS", raising=False)
        svc = AuthService()
        assert svc.algorithm == "RS256"
        assert svc.current_kid == "current"

    def test_explicit_secret_arg_still_works(self):
        svc = AuthService(secret="a" * 32)
        assert svc.algorithm == "HS256"


# ── RS256 sign/verify round-trip ──

class TestRS256RoundTrip:
    def test_issue_and_verify(self, monkeypatch, rsa_keypair):
        priv_pem, _, _ = rsa_keypair
        monkeypatch.setenv("AUTH_PRIVATE_KEY", priv_pem)
        svc = AuthService()
        tok = svc.issue_token("alice", tenant_id="acme", role="user")
        assert isinstance(tok, str) and tok.count(".") == 2

        payload = svc.verify_token(tok)
        assert payload is not None
        assert payload.sub == "alice"
        assert payload.tenant_id == "acme"

    def test_external_pyjwt_can_verify(self, monkeypatch, rsa_keypair):
        """External service using only the public PEM can verify our token."""
        priv_pem, pub_pem, _ = rsa_keypair
        monkeypatch.setenv("AUTH_PRIVATE_KEY", priv_pem)
        svc = AuthService()
        tok = svc.issue_token("bob", tenant_id="external-co")

        decoded = pyjwt.decode(
            tok,
            pub_pem.encode("utf-8"),
            algorithms=["RS256"],
        )
        assert decoded["sub"] == "bob"
        assert decoded["tenant_id"] == "external-co"

    def test_wrong_algorithm_rejected(self, monkeypatch, rsa_keypair):
        """A token signed by some HS256 secret must NOT verify via RS256 path."""
        priv_pem, _, _ = rsa_keypair
        monkeypatch.setenv("AUTH_PRIVATE_KEY", priv_pem)

        # Forge an HS256 token with a random secret
        forged = pyjwt.encode(
            {"sub": "evil", "exp": 9999999999, "iat": 0, "tenant_id": "x", "role": "user"},
            "some-secret",
            algorithm="HS256",
        )
        svc = AuthService()
        assert svc.verify_token(forged) is None

    def test_expired_token_rejected(self, monkeypatch, rsa_keypair):
        priv_pem, _, _ = rsa_keypair
        monkeypatch.setenv("AUTH_PRIVATE_KEY", priv_pem)
        svc = AuthService()
        tok = svc.issue_token("alice", ttl=-1)
        assert svc.verify_token(tok) is None

    def test_token_header_includes_kid(self, monkeypatch, rsa_keypair):
        priv_pem, _, _ = rsa_keypair
        monkeypatch.setenv("AUTH_PRIVATE_KEY", priv_pem)
        svc = AuthService()
        tok = svc.issue_token("alice")
        header = pyjwt.get_unverified_header(tok)
        assert header["alg"] == "RS256"
        assert header["kid"] == "current"


# ── Key rotation ──

class TestKeyRotation:
    def test_old_token_verifies_after_rotation(self, monkeypatch, rsa_keypair, second_keypair):
        """Token signed by v1 private key still verifies after rotation to v2,
        because the public key for v1 is retained in AUTH_PUBLIC_KEYS."""
        priv_v1, pub_v1, _ = rsa_keypair
        priv_v2, _ = second_keypair

        # Sign with v1 (kid=v1, public-key listed under v1 too)
        monkeypatch.setenv("AUTH_PRIVATE_KEY", priv_v1)
        monkeypatch.setenv("AUTH_SIGNING_KID", "v1")
        monkeypatch.setenv("AUTH_PUBLIC_KEYS", f"v1:{pub_v1}")
        svc_v1 = AuthService()
        tok_v1 = svc_v1.issue_token("alice", tenant_id="t1")
        assert svc_v1.current_kid == "v1"

        # Rotate: v2 signs new tokens, but v1 public key retained for verify
        monkeypatch.setenv("AUTH_PRIVATE_KEY", priv_v2)
        monkeypatch.setenv("AUTH_SIGNING_KID", "v2")
        monkeypatch.setenv("AUTH_PUBLIC_KEYS", f"v1:{pub_v1}")
        svc_v2 = AuthService()
        assert svc_v2.algorithm == "RS256"
        assert svc_v2.current_kid == "v2"

        # Old v1 token still verifies
        payload = svc_v2.verify_token(tok_v1)
        assert payload is not None
        assert payload.sub == "alice"
        assert payload.tenant_id == "t1"

        # New v2 token signs and verifies fine
        tok_v2 = svc_v2.issue_token("bob")
        assert svc_v2.verify_token(tok_v2) is not None

    def test_multi_public_keys(self, monkeypatch, rsa_keypair, second_keypair):
        """AUTH_PUBLIC_KEYS can register multiple unrelated public keys."""
        priv, pub_a, _ = rsa_keypair
        _, pub_b = second_keypair
        monkeypatch.setenv("AUTH_PRIVATE_KEY", priv)
        monkeypatch.setenv("AUTH_PUBLIC_KEYS", f"alpha:{pub_a},beta:{pub_b}")
        svc = AuthService()
        jwks = svc.get_jwks()
        kids = {k["kid"] for k in jwks["keys"]}
        assert "alpha" in kids
        assert "beta" in kids


# ── JWKS endpoint ──

class TestJWKS:
    def test_jwks_returns_rsa_keys_with_correct_fields(self, monkeypatch, rsa_keypair):
        priv_pem, pub_pem, _ = rsa_keypair
        monkeypatch.setenv("AUTH_PRIVATE_KEY", priv_pem)
        monkeypatch.setenv("AUTH_SIGNING_KID", "v1")
        monkeypatch.setenv("AUTH_PUBLIC_KEYS", "v1:" + pub_pem)
        svc = AuthService()

        jwks = svc.get_jwks()
        assert "keys" in jwks
        # The derived pubkey under "current" is the same PEM as v1, so
        # _verify_keys dedupes by material — only one key in JWKS.
        assert len(jwks["keys"]) == 1
        key = jwks["keys"][0]
        assert key["kty"] == "RSA"
        assert key["kid"] == "v1"
        assert key["use"] == "sig"
        assert key["alg"] == "RS256"
        assert key["e"] == "AQAB"
        padded = key["n"] + "=" * (-len(key["n"]) % 4)
        n_bytes = base64.urlsafe_b64decode(padded)
        assert int.from_bytes(n_bytes, "big") > 0

    def test_jwks_includes_extra_public_keys(self, monkeypatch, rsa_keypair, second_keypair):
        priv_pem, pub_a, _ = rsa_keypair
        _, pub_b = second_keypair
        monkeypatch.setenv("AUTH_PRIVATE_KEY", priv_pem)
        monkeypatch.setenv("AUTH_PUBLIC_KEYS", f"v1:{pub_a},remote:{pub_b}")
        svc = AuthService()
        jwks = svc.get_jwks()
        kids = {k["kid"] for k in jwks["keys"]}
        # current derived from private key + the two explicit entries
        assert {"current", "v1", "remote"} == kids

    def test_external_verifier_uses_jwks(self, monkeypatch, rsa_keypair):
        """Simulate an external service fetching /jwks and verifying a token."""
        priv_pem, _, _ = rsa_keypair
        monkeypatch.setenv("AUTH_PRIVATE_KEY", priv_pem)
        svc = AuthService()
        tok = svc.issue_token("carol")

        # External verifier reconstructs the public key from JWKS 'n' and 'e'
        jwks = svc.get_jwks()
        key = jwks["keys"][0]
        n = int.from_bytes(
            base64.urlsafe_b64decode(key["n"] + "=" * (-len(key["n"]) % 4)),
            "big",
        )
        e = int.from_bytes(
            base64.urlsafe_b64decode(key["e"] + "=" * (-len(key["e"]) % 4)),
            "big",
        )
        pub_numbers = rsa.RSAPublicNumbers(e=e, n=n).public_key()
        pub_pem = pub_numbers.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        decoded = pyjwt.decode(tok, pub_pem, algorithms=["RS256"])
        assert decoded["sub"] == "carol"

    def test_jwks_empty_for_hs256(self, monkeypatch):
        """HS256 services must not leak any keys (would defeat the secret)."""
        monkeypatch.delenv("AUTH_PRIVATE_KEY", raising=False)
        monkeypatch.setenv("AUTH_SECRET", "a" * 32)
        svc = AuthService()
        jwks = svc.get_jwks()
        assert jwks == {"keys": []}


# ── HS256 back-compat (regression guard) ──

class TestHS256BackCompat:
    def test_hs256_still_signs_and_verifies(self):
        svc = AuthService(secret="a" * 32)
        tok = svc.issue_token("legacy-user")
        payload = svc.verify_token(tok)
        assert payload is not None
        assert payload.sub == "legacy-user"

    def test_hs256_with_auth_secrets_env(self, monkeypatch):
        monkeypatch.setenv(
            "AUTH_SECRETS",
            f"v1:{'a' * 32},v0:{'b' * 32}",
        )
        svc = AuthService()
        assert svc.algorithm == "HS256"
        assert svc.current_kid == "v1"
        tok = svc.issue_token("alice")
        assert svc.verify_token(tok) is not None


# ── JWKS HTTP endpoint ──

class TestJWKSEndpoint:
    def test_get_jwks_via_http(self, monkeypatch, rsa_keypair):
        """The /api/auth/jwks route returns the same JSON as get_jwks()."""
        from fastapi.testclient import TestClient
        from agent_system.api.server import app
        import agent_system.api.state as state_mod

        priv_pem, _, _ = rsa_keypair
        monkeypatch.setenv("AUTH_PRIVATE_KEY", priv_pem)
        monkeypatch.setenv("AUTH_SIGNING_KID", "v1")

        # state._auth_service is module-cached and the route reads it
        # directly (not via Depends), so swap it for a fresh RS256 instance.
        original = state_mod._auth_service
        state_mod._auth_service = AuthService()
        try:
            client = TestClient(app)
            r = client.get("/api/auth/jwks")
            assert r.status_code == 200
            body = r.json()
            assert "keys" in body
            assert len(body["keys"]) >= 1
            assert body["keys"][0]["alg"] == "RS256"
            assert body["keys"][0]["kid"] == "v1"
        finally:
            state_mod._auth_service = original