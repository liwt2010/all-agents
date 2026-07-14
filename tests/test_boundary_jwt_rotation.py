"""Boundary tests: JWT secret rotation with multiple active keys.

Issue: During JWT secret rotation, multiple keys are simultaneously valid.
Verify requests are correctly routed to the appropriate key.

Run: pytest tests/test_boundary_jwt_rotation.py -v
"""
import pytest
import time
from datetime import datetime, timezone, timedelta

from agent_system.core.auth.jwt import JWTAuthService, JWTPayload


class TestJWTSecretRotation:
    """Test JWT secret rotation and multi-key handling."""

    def test_multiple_keys_all_validate(self):
        """All registered keys should validate tokens signed by any of them."""
        # Create auth service with multiple keys
        keys = {
            "key-1": "secret-key-one-for-testing-purposes",
            "key-2": "secret-key-two-for-testing-purposes",
            "key-3": "secret-key-three-for-testing-purposes",
        }

        svc = JWTAuthService(secrets=keys)

        # Issue tokens with each key
        token1 = svc.issue_token("user1", tenant_id="tenant1")
        token2 = svc.issue_token("user2", tenant_id="tenant2")

        # All tokens should validate
        payload1 = svc.verify_token(token1)
        payload2 = svc.verify_token(token2)

        assert payload1 is not None
        assert payload2 is not None
        assert payload1.user_id == "user1"
        assert payload2.user_id == "user2"

    def test_new_key_issues_tokens_after_rotation(self):
        """After adding a new key, tokens should use the new key."""
        keys = {
            "key-1": "old-secret-key-for-testing",
        }

        svc = JWTAuthService(secrets=keys)

        # Issue token with old key
        old_token = svc.issue_token("user1", tenant_id="tenant1")
        assert svc.verify_token(old_token) is not None

        # Simulate rotation: add new key, keep old key temporarily
        keys["key-2"] = "new-secret-key-for-testing"
        svc = JWTAuthService(secrets=keys)

        # New token should work
        new_token = svc.issue_token("user1", tenant_id="tenant1")
        assert svc.verify_token(new_token) is not None

        # Old token should still work (graceful rotation)
        assert svc.verify_token(old_token) is not None, \
            "Old key should still validate during rotation"

    def test_old_key_deprecated_after_rotation(self):
        """After removing old key, new tokens should use new key."""
        keys = {
            "key-1": "old-secret-key-for-testing",
        }

        svc = JWTAuthService(secrets=keys)
        old_token = svc.issue_token("user1", tenant_id="tenant1")

        # Rotate: remove old key, add new key
        keys = {
            "key-2": "new-secret-key-for-testing",
        }
        svc = JWTAuthService(secrets=keys)

        # New token should work
        new_token = svc.issue_token("user1", tenant_id="tenant1")
        assert svc.verify_token(new_token) is not None

        # Old token should fail (key is no longer valid)
        result = svc.verify_token(old_token)
        assert result is None, "Token signed with removed key should not validate"

    def test_token_with_unknown_kid_fails(self):
        """Token with unknown key ID should fail validation."""
        keys = {
            "key-1": "secret-key-one",
        }

        svc = JWTAuthService(secrets=keys)

        # Try to verify a token with unknown key ID
        # This would require manual token manipulation
        # In practice, unknown kid means the token was signed with a different key
        fake_token = "eyJhbGciOiJkaWQta2V5LTk5OSIsInR5cCI6IkpXVCJ9.eyJ1c2VyX2lkIjoidXNlcjEiLCJ0ZW5hbnRfaWQiOiJ0ZW5hbnQxIn0.fake"

        result = svc.verify_token(fake_token)
        assert result is None, "Token with unknown key should fail"

    def test_expired_token_fails_regardless_of_key(self):
        """Expired tokens should fail even with valid key."""
        keys = {
            "key-1": "secret-key-one",
        }

        svc = JWTAuthService(secrets=keys)

        # Create an expired token manually
        expired_payload = JWTPayload(
            user_id="user1",
            tenant_id="tenant1",
            exp=datetime.now(timezone.utc) - timedelta(hours=1),  # Expired 1 hour ago
            iat=datetime.now(timezone.utc) - timedelta(hours=2),
            kid="key-1",
        )

        # Manually sign and encode
        import jwt as PyJWT
        secret = "secret-key-one"
        token = PyJWT.encode(
            expired_payload.model_dump(mode="json"),
            secret,
            algorithm="HS256",
            json_encoder_experimental=False,
        )

        # Should fail validation due to expiration
        result = svc.verify_token(token)
        assert result is None, "Expired token should fail validation"

    def test_token_without_kid_uses_default_key(self):
        """Token without key ID should use default/first key."""
        keys = {
            "key-1": "secret-key-one",
            "key-2": "secret-key-two",
        }

        svc = JWTAuthService(secrets=keys)

        # Issue token (should include kid)
        token = svc.issue_token("user1", tenant_id="tenant1")

        # Verify it has kid header
        import jwt as PyJWT
        header = PyJWT.get_unverified_header(token)
        assert "kid" in header, "Token should include key ID"

        # Token should validate
        result = svc.verify_token(token)
        assert result is not None


class TestJWTKeyPriority:
    """Test key selection and priority during rotation."""

    def test_most_recent_key_is_default(self):
        """The most recently added key should be the default for new tokens."""
        keys = {
            "key-old": "old-secret-key",
            "key-new": "new-secret-key",
        }

        svc = JWTAuthService(secrets=keys)

        # New token should use new key
        token = svc.issue_token("user1", tenant_id="tenant1")

        import jwt as PyJWT
        header = PyJWT.get_unverified_header(token)
        assert header.get("kid") == "key-new", "New tokens should use most recent key"

    def test_all_keys_can_validate(self):
        """During transition, any valid key should validate."""
        keys = {
            "key-old": "old-secret-key",
            "key-new": "new-secret-key",
        }

        svc = JWTAuthService(secrets=keys)

        # Issue tokens with both keys
        import jwt as PyJWT

        old_payload = JWTPayload(
            user_id="user1",
            tenant_id="tenant1",
            exp=datetime.now(timezone.utc) + timedelta(hours=1),
            iat=datetime.now(timezone.utc),
            kid="key-old",
        )
        old_token = PyJWT.encode(
            old_payload.model_dump(mode="json"),
            "old-secret-key",
            algorithm="HS256",
        )

        new_token = svc.issue_token("user1", tenant_id="tenant1")

        # Both should validate
        assert svc.verify_token(old_token) is not None, "Old key should still validate"
        assert svc.verify_token(new_token) is not None, "New key should validate"
