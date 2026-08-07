"""Adversarial tests for AWS ALB-signed OIDC identity."""

from __future__ import annotations

import base64
import json
import time

import aiohttp
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jose import jwt as jose_jwt

from src.gateway.auth.oidc_service import (
    MAX_ALB_KEY_BYTES,
    MAX_ALB_KEY_CACHE_ENTRIES,
    MAX_ALB_KEY_CACHE_TTL_SECONDS,
    OIDCConfig,
    OIDCService,
)

REGION = "us-east-1"
SIGNER_ARN = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/axon-prod/50dc6c495c0c9188"
CLIENT_ID = "oidc-client-123"
ALB_ISSUER = "https://public-keys.auth.elb.us-east-1.amazonaws.com"
KEY_ID = "12345678-1234-1234-1234-123456789012"
SUBJECT = "user-123"


@pytest.fixture(scope="module")
def alb_signing_material() -> tuple[bytes, str]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_pem, public_pem


@pytest.fixture
def alb_config() -> OIDCConfig:
    return OIDCConfig(
        issuer="https://idp.example.test",
        audience="direct-client",
        alb_region=REGION,
        alb_signer_arn=SIGNER_ARN,
        alb_client_id=CLIENT_ID,
        alb_issuer=ALB_ISSUER,
    )


def _alb_token(
    private_pem: bytes,
    *,
    header_overrides: dict | None = None,
    claim_overrides: dict | None = None,
    remove_claims: tuple[str, ...] = (),
) -> str:
    header = {
        "kid": KEY_ID,
        "signer": SIGNER_ARN,
        "client": CLIENT_ID,
        "iss": ALB_ISSUER,
        "exp": int(time.time()) + 300,
    }
    header.update(header_overrides or {})
    claims = {
        "sub": SUBJECT,
        "email": "user@example.test",
        "custom:tenant_id": "tenant-1",
        "custom:project_id": "project-1",
        "custom:roles": ["member"],
        "scope": "openid chat:invoke",
    }
    claims.update(claim_overrides or {})
    for claim in remove_claims:
        claims.pop(claim, None)
    return jose_jwt.encode(
        claims,
        private_pem,
        algorithm="ES256",
        headers=header,
    )


def _unsigned_token(header_json: str, payload: dict | None = None) -> str:
    def encode(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return ".".join(
        (
            encode(header_json.encode()),
            encode(json.dumps(payload or {"sub": SUBJECT}).encode()),
            encode(b"invalid-signature"),
        )
    )


def _install_key_fetch(
    service: OIDCService,
    monkeypatch,
    public_pem: str,
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def fetch(kid: str, key_base_url: str) -> str:
        calls.append((kid, key_base_url))
        return public_pem

    monkeypatch.setattr(service, "_fetch_alb_public_key", fetch)
    return calls


class TestALBJWTVerification:
    @pytest.mark.asyncio
    async def test_accepts_only_valid_alb_signed_identity(
        self,
        alb_config,
        alb_signing_material,
        monkeypatch,
    ):
        private_pem, public_pem = alb_signing_material
        service = OIDCService(alb_config)
        calls = _install_key_fetch(service, monkeypatch, public_pem)

        context = await service.validate_alb_jwt(
            _alb_token(private_pem),
            expected_subject=SUBJECT,
        )

        assert context is not None
        assert context.user_id == SUBJECT
        assert context.subject == SUBJECT
        assert context.issuer == ALB_ISSUER
        assert context.tenant_id == "tenant-1"
        assert calls == [(KEY_ID, ALB_ISSUER)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("signer", SIGNER_ARN + "-attacker"),
            ("client", CLIENT_ID + "-attacker"),
            ("iss", "https://public-keys.auth.elb.us-west-2.amazonaws.com"),
            ("exp", int(time.time()) - 1),
            ("exp", True),
            ("exp", "9999999999"),
            ("kid", "../metadata"),
            ("kid", ""),
        ],
    )
    async def test_rejects_untrusted_header_before_key_fetch(
        self,
        alb_config,
        alb_signing_material,
        monkeypatch,
        field,
        value,
    ):
        private_pem, _ = alb_signing_material
        service = OIDCService(alb_config)

        async def unexpected_fetch(*_args):
            raise AssertionError("untrusted metadata must not trigger key retrieval")

        monkeypatch.setattr(service, "_fetch_alb_public_key", unexpected_fetch)
        token = _alb_token(private_pem, header_overrides={field: value})

        assert await service.validate_alb_jwt(token, expected_subject=SUBJECT) is None

    @pytest.mark.asyncio
    async def test_rejects_algorithm_confusion_before_key_fetch(
        self,
        alb_config,
        monkeypatch,
    ):
        service = OIDCService(alb_config)

        async def unexpected_fetch(*_args):
            raise AssertionError("wrong algorithm must not trigger key retrieval")

        monkeypatch.setattr(service, "_fetch_alb_public_key", unexpected_fetch)
        header = json.dumps(
            {
                "alg": "HS256",
                "kid": KEY_ID,
                "signer": SIGNER_ARN,
                "client": CLIENT_ID,
                "iss": ALB_ISSUER,
                "exp": int(time.time()) + 300,
            }
        )

        assert (
            await service.validate_alb_jwt(
                _unsigned_token(header),
                expected_subject=SUBJECT,
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_rejects_duplicate_protected_header_members(
        self,
        alb_config,
        monkeypatch,
    ):
        service = OIDCService(alb_config)

        async def unexpected_fetch(*_args):
            raise AssertionError("ambiguous header must not trigger key retrieval")

        monkeypatch.setattr(service, "_fetch_alb_public_key", unexpected_fetch)
        header = (
            '{"alg":"ES256","kid":"first","kid":"second",'
            f'"signer":"{SIGNER_ARN}","client":"{CLIENT_ID}",'
            f'"iss":"{ALB_ISSUER}","exp":{int(time.time()) + 300}}}'
        )

        assert (
            await service.validate_alb_jwt(
                _unsigned_token(header),
                expected_subject=SUBJECT,
            )
            is None
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("claim_overrides", "remove_claims", "expected_subject"),
        [
            ({}, ("sub",), SUBJECT),
            ({"sub": ""}, (), "companion-subject"),
            ({"sub": True}, (), "True"),
            ({"sub": SUBJECT}, (), "different-user"),
            ({"exp": int(time.time()) - 1}, (), SUBJECT),
        ],
    )
    async def test_rejects_invalid_or_mismatched_signed_subject_and_expiry(
        self,
        alb_config,
        alb_signing_material,
        monkeypatch,
        claim_overrides,
        remove_claims,
        expected_subject,
    ):
        private_pem, public_pem = alb_signing_material
        service = OIDCService(alb_config)
        _install_key_fetch(service, monkeypatch, public_pem)
        token = _alb_token(
            private_pem,
            claim_overrides=claim_overrides,
            remove_claims=remove_claims,
        )

        assert (
            await service.validate_alb_jwt(
                token,
                expected_subject=expected_subject,
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_rejects_signature_from_another_key(
        self,
        alb_config,
        alb_signing_material,
        monkeypatch,
    ):
        _, trusted_public_pem = alb_signing_material
        attacker_key = ec.generate_private_key(ec.SECP256R1())
        attacker_private_pem = attacker_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        service = OIDCService(alb_config)
        _install_key_fetch(service, monkeypatch, trusted_public_pem)

        assert (
            await service.validate_alb_jwt(
                _alb_token(attacker_private_pem),
                expected_subject=SUBJECT,
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_unconfigured_or_malformed_trust_fails_closed(
        self,
        alb_signing_material,
        monkeypatch,
    ):
        private_pem, _ = alb_signing_material
        service = OIDCService(
            OIDCConfig(
                alb_region=REGION,
                alb_signer_arn="not-an-alb-arn",
                alb_client_id=CLIENT_ID,
                alb_issuer=ALB_ISSUER,
            )
        )

        async def unexpected_fetch(*_args):
            raise AssertionError("invalid trust config must not fetch a key")

        monkeypatch.setattr(service, "_fetch_alb_public_key", unexpected_fetch)

        assert (
            await service.validate_alb_jwt(
                _alb_token(private_pem),
                expected_subject=SUBJECT,
            )
            is None
        )


class TestALBKeyRetrieval:
    @pytest.mark.asyncio
    async def test_cache_is_bounded_and_evicts_oldest_key(
        self,
        alb_config,
        alb_signing_material,
        monkeypatch,
    ):
        _, public_pem = alb_signing_material
        service = OIDCService(alb_config)
        _install_key_fetch(service, monkeypatch, public_pem)

        for index in range(MAX_ALB_KEY_CACHE_ENTRIES + 3):
            key = await service._get_alb_public_key(
                f"key-{index}",
                ALB_ISSUER,
            )
            assert key == public_pem

        assert len(service._alb_key_cache) == MAX_ALB_KEY_CACHE_ENTRIES
        assert "key-0" not in service._alb_key_cache
        assert f"key-{MAX_ALB_KEY_CACHE_ENTRIES + 2}" in service._alb_key_cache

    @pytest.mark.asyncio
    async def test_stale_key_is_not_used_when_refresh_fails(
        self,
        alb_config,
        alb_signing_material,
        monkeypatch,
    ):
        _, public_pem = alb_signing_material
        alb_config.alb_key_cache_ttl = 10
        service = OIDCService(alb_config)
        service._alb_key_cache[KEY_ID] = (
            public_pem,
            time.monotonic() - 11,
        )

        async def failed_fetch(*_args):
            return None

        monkeypatch.setattr(service, "_fetch_alb_public_key", failed_fetch)

        assert await service._get_alb_public_key(KEY_ID, ALB_ISSUER) is None
        assert KEY_ID not in service._alb_key_cache

    @pytest.mark.asyncio
    async def test_cache_ttl_is_capped(
        self,
        alb_config,
        alb_signing_material,
        monkeypatch,
    ):
        _, public_pem = alb_signing_material
        alb_config.alb_key_cache_ttl = MAX_ALB_KEY_CACHE_TTL_SECONDS * 100
        service = OIDCService(alb_config)
        service._alb_key_cache[KEY_ID] = (
            public_pem,
            time.monotonic() - MAX_ALB_KEY_CACHE_TTL_SECONDS - 1,
        )

        async def failed_fetch(*_args):
            return None

        monkeypatch.setattr(service, "_fetch_alb_public_key", failed_fetch)

        assert await service._get_alb_public_key(KEY_ID, ALB_ISSUER) is None

    @pytest.mark.asyncio
    async def test_oversized_key_response_is_rejected_without_redirects(
        self,
        alb_config,
        monkeypatch,
    ):
        captured = {}

        class Response:
            status = 200
            headers = {}

            class Content:
                async def iter_chunked(self, _size):
                    yield b"x" * (MAX_ALB_KEY_BYTES + 1)

            content = Content()

        class Stream:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return None

        class Client:
            def __init__(self, **kwargs):
                captured["client_kwargs"] = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def get(self, url, **kwargs):
                captured["request"] = (url, kwargs)
                return Stream()

        monkeypatch.setattr(aiohttp, "ClientSession", Client)
        service = OIDCService(alb_config)

        assert await service._fetch_alb_public_key(KEY_ID, ALB_ISSUER) is None
        assert captured["client_kwargs"]["trust_env"] is False
        assert captured["client_kwargs"]["auto_decompress"] is False
        assert captured["client_kwargs"]["timeout"].total == 5.0
        assert captured["request"][0] == f"{ALB_ISSUER}/{KEY_ID}"
        assert captured["request"][1]["allow_redirects"] is False

    def test_only_p256_public_keys_are_accepted(
        self,
        alb_signing_material,
    ):
        _, public_pem = alb_signing_material
        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        rsa_public_pem = rsa_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        assert OIDCService._validate_alb_public_key(public_pem.encode()) is not None
        assert OIDCService._validate_alb_public_key(rsa_public_pem) is None
