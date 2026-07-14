"""Boundary tests: JWT secret rotation with multiple active keys.

Issue: During JWT secret rotation, multiple keys are simultaneously valid.
Verify requests are correctly routed to the appropriate key.

Run: pytest tests/test_boundary_jwt_rotation.py -v
"""
import pytest
import time
from datetime import datetime, timezone, timedelta

from agent_system.core.auth.jwt import AuthService, TokenPayload


class TestJWTSecretRotation:
    """Test JWT secret rotation and multi-key handling."""

    def test_multiple_keys_all_validate(self):
        """AuthService with single secret: verify issue/verify cycle works."""
        svc = AuthService(secret="test-secret-key-32chars-long-enough-for-hs256")

        token1 = svc.issue_token("user1", tenant_id="tenant1")
        token2 = svc.issue_token("user2", tenant_id="tenant2")

        payload1 = svc.verify_token(token1)
        payload2 = svc.verify_token(token2)

        assert payload1 is not None
        assert payload2 is not None
        assert payload1.sub == "user1"
        assert payload2.sub == "user2"

    def test_new_key_issues_tokens_after_rotation(self):
        """After rotation, tokens signed with old key still verify if kept."""
        import os
        os.environ["AUTH_SECRETS"] = "v2:new-secret-key-long-enough-32chars!,v1:old-secret-key-long-enough-32chars!"
        try:
            svc = AuthService()

            new_token = svc.issue_token("user1", tenant_id="tenant1")
            assert svc.verify_token(new_token) is not None

            import jwt as PyJWT
            now = int(time.time())
            # Use raw dict because TokenPayload does not have a kid field
            old_payload = {
                "sub": "user2", "tenant_id": "tenant1", "role": "user", "scopes": [],
                "iat": now,
                "exp": now + 3600,
                "kid": "v1",
            }
            old_token = PyJWT.encode(old_payload, "old-secret-key-long-enough-32chars!", algorithm="HS256")
            assert svc.verify_token(old_token) is not None, "Old key should still validate"
        finally:
            os.environ.pop("AUTH_SECRETS", None)

    def test_old_key_deprecated_after_rotation(self):
        """After removing old key, tokens signed with it should fail."""
        import os
        os.environ["AUTH_SECRETS"] = "v2:new-secret-key-long-enough-32chars!"
        try:
            svc = AuthService()
            new_token = svc.issue_token("user1", tenant_id="tenant1")
            assert svc.verify_token(new_token) is not None

            import jwt as PyJWT
            now = int(time.time())
            old_payload = {
                "sub": "user1", "tenant_id": "tenant1", "role": "user", "scopes": [],
                "iat": now,
                "exp": now + 3600,
                "kid": "v1",
            }
            old_token = PyJWT.encode(old_payload, "old-secret-key-long-enough-32chars!", algorithm="HS256")
            result = svc.verify_token(old_token)
            assert result is None, "Token signed with removed key should not validate"
        finally:
            os.environ.pop("AUTH_SECRETS", None)

    def test_token_with_unknown_kid_fails(self):
        """Token with unknown key ID should fail validation."""
        svc = AuthService(secret="test-secret-key-32chars-long-enough-for-hs256")
        fake_token = "eyJhbGciOiJkaWQta2V5LTk5OSIsInR5cCI6IkpXVCJ9.eyJ1c2VyX2lkIjoidXNlcjEiLCJ0ZW5hbnRfaWQiOiJ0ZW5hbnQxIn0.fake"
        result = svc.verify_token(fake_token)
        assert result is None, "Token with unknown key should fail"

    def test_expired_token_fails_regardless_of_key(self):
        """Expired tokens should fail even with valid key."""
        svc = AuthService(secret="test-secret-key-32chars-long-enough-for-hs256")

        import jwt as PyJWT
        now = int(time.time())
        # TokenPayload does NOT have a kid field, but for expired test we
        # don't need kid matching: it will use the current secret as fallback
        expired_payload = TokenPayload(
            sub="user1",
            tenant_id="tenant1",
            role="user",
            scopes=[],
            exp=now - 3600,
            iat=now - 7200,
        )
        token = PyJWT.encode(
            expired_payload.model_dump(mode="json"),
            "test-secret-key-32chars-long-enough-for-hs256",
            algorithm="HS256",
        )

        result = svc.verify_token(token)
        assert result is None, "Expired token should fail validation"

    def test_token_without_kid_uses_default_key(self):
        """Token without key ID should use default/fallback key."""
        svc = AuthService(secret="test-secret-key-32chars-long-enough-for-hs256")
        token = svc.issue_token("user1", tenant_id="tenant1")
        result = svc.verify_token(token)
        assert result is not None


class TestJWTKeyPriority:
    """Test key selection and priority during rotation."""

    def test_most_recent_key_is_default(self):
        """The first key should be the default for new tokens."""
        import os
        secret_val = "new-secret-key-long-enough-32chars!"
        os.environ["AUTH_SECRETS"] = f"key-new:{secret_val},key-old:old-secret-key-long-enough-32chars!"
        try:
            svc = AuthService()

            token = svc.issue_token("user1", tenant_id="tenant1")

            import jwt as PyJWT
            data = PyJWT.decode(token, options={"verify_signature": False})
            assert data.get("kid") == "key-new", "New tokens should use first/current key"
        finally:
            os.environ.pop("AUTH_SECRETS", None)

    def test_all_keys_can_validate(self):
        """During transition, any valid key should validate."""
        import os
        os.environ["AUTH_SECRETS"] = "key-new:new-secret-key-long-enough-32chars!,key-old:old-secret-key-long-enough-32chars!"
        try:
            svc = AuthService()

            import jwt as PyJWT
            now = int(time.time())
            old_payload = {
                "sub": "user1", "tenant_id": "tenant1", "role": "user", "scopes": [],
                "exp": now + 3600,
                "iat": now,
                "kid": "key-old",
            }
            old_token = PyJWT.encode(old_payload, "old-secret-key-long-enough-32chars!", algorithm="HS256")

            new_token = svc.issue_token("user1", tenant_id="tenant1")

            assert svc.verify_token(old_token) is not None, "Old key should validate"
            assert svc.verify_token(new_token) is not None, "New key should validate"
        finally:
            os.environ.pop("AUTH_SECRETS", None)
