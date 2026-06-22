"""Tests for OIDC authentication service."""

import asyncio
import base64
import json
import time

import pytest

from src.gateway.auth.oidc_service import OIDCConfig, OIDCService
from src.gateway.models import AuthMethod


def _make_jwt(header: dict, payload: dict, signature: str = "sig") -> str:
    """Create a fake JWT token (not cryptographically valid)."""
    h = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    p = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{h}.{p}.{signature}"


@pytest.fixture
def config():
    return OIDCConfig(
        issuer="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST",
        audience="axonllm-app",
        alb_region="us-east-1",
    )


@pytest.fixture
def service(config):
    return OIDCService(config=config)


class TestDecodeJWTHeader:
    def test_decodes_valid_header(self, service):
        header = {"alg": "RS256", "kid": "test-kid-123"}
        token = _make_jwt(header, {"sub": "user1"})
        result = service._decode_jwt_header(token)
        assert result == header

    def test_returns_none_for_garbage(self, service):
        result = service._decode_jwt_header("not.a.jwt")
        assert result is None

    def test_returns_none_for_empty(self, service):
        result = service._decode_jwt_header("")
        assert result is None


class TestClaimMapping:
    def test_maps_standard_claims(self, service):
        claims = {
            "sub": "user-123",
            "email": "user@example.com",
            "custom:project_id": "proj-1",
            "custom:tenant_id": "tenant-abc",
            "custom:business_unit": "engineering",
            "custom:roles": "admin,developer",
            "scope": "openid profile",
        }
        ctx = service._map_claims_to_context(claims)

        assert ctx.user_id == "user-123"
        assert ctx.email == "user@example.com"
        assert ctx.project_id == "proj-1"
        assert ctx.tenant_id == "tenant-abc"
        assert ctx.business_unit == "engineering"
        assert ctx.roles == ["admin", "developer"]
        assert ctx.scopes == ["openid", "profile"]
        assert ctx.auth_method == AuthMethod.OIDC_JWT

    def test_handles_missing_claims(self, service):
        claims = {"sub": "user-456"}
        ctx = service._map_claims_to_context(claims)

        assert ctx.user_id == "user-456"
        assert ctx.project_id == ""
        assert ctx.tenant_id is None
        assert ctx.email is None
        assert ctx.roles == []

    def test_handles_roles_as_list(self, service):
        claims = {"sub": "u", "custom:roles": ["admin", "viewer"]}
        ctx = service._map_claims_to_context(claims)
        assert ctx.roles == ["admin", "viewer"]


class TestValidateOIDCJWT:
    def test_rejects_expired_token(self, service):
        header = {"alg": "RS256", "kid": "k1"}
        payload = {
            "sub": "user-1",
            "exp": int(time.time()) - 3600,
            "iss": service._config.issuer,
            "aud": service._config.audience,
        }
        token = _make_jwt(header, payload)

        # Without python-jose, falls back to manual decode but checks exp
        result = asyncio.run(
            service.validate_oidc_jwt(token)
        )
        assert result is None

    def test_rejects_wrong_issuer(self, service):
        header = {"alg": "RS256", "kid": "k1"}
        payload = {
            "sub": "user-1",
            "exp": int(time.time()) + 3600,
            "iss": "https://wrong-issuer.com",
            "aud": service._config.audience,
        }
        token = _make_jwt(header, payload)

        # Pre-populate JWKS cache so it doesn't try to fetch
        service._jwks_cache = {"keys": [{"kid": "k1", "kty": "RSA"}]}
        service._jwks_fetched_at = time.time()

        result = asyncio.run(
            service.validate_oidc_jwt(token)
        )
        assert result is None

    def test_rejects_wrong_audience(self, service):
        header = {"alg": "RS256", "kid": "k1"}
        payload = {
            "sub": "user-1",
            "exp": int(time.time()) + 3600,
            "iss": service._config.issuer,
            "aud": "wrong-audience",
        }
        token = _make_jwt(header, payload)

        service._jwks_cache = {"keys": [{"kid": "k1", "kty": "RSA"}]}
        service._jwks_fetched_at = time.time()

        result = asyncio.run(
            service.validate_oidc_jwt(token)
        )
        assert result is None


class TestJWKSCache:
    def test_cache_used_within_ttl(self, service):
        service._jwks_cache = {"keys": [{"kid": "k1"}]}
        service._jwks_fetched_at = time.time()

        # _get_jwks should return cache without fetching
        result = asyncio.run(service._get_jwks())
        assert result == {"keys": [{"kid": "k1"}]}

    def test_find_key_by_kid(self, service):
        jwks = {"keys": [{"kid": "a"}, {"kid": "b"}, {"kid": "c"}]}
        assert service._find_key(jwks, "b") == {"kid": "b"}
        assert service._find_key(jwks, "x") is None

    def test_find_key_returns_first_when_no_kid(self, service):
        jwks = {"keys": [{"kid": "a"}, {"kid": "b"}]}
        assert service._find_key(jwks, None) == {"kid": "a"}
