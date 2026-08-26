"""Tests for OIDC authentication service."""

import asyncio
import base64
import json
import logging
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from src.gateway.auth.oidc_service import (
    MAX_DIRECT_OIDC_HEADER_BYTES,
    MAX_DIRECT_OIDC_JWT_BYTES,
    MAX_DIRECT_OIDC_KID_BYTES,
    MAX_JWKS_CACHE_TTL_SECONDS,
    MAX_OIDC_DISCOVERY_BYTES,
    MAX_OIDC_JWKS_BYTES,
    MAX_OIDC_JWKS_KEYS,
    OIDC_HTTP_CONNECT_TIMEOUT_SECONDS,
    OIDC_HTTP_TOTAL_TIMEOUT_SECONDS,
    OIDCConfig,
    OIDCService,
)
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


@pytest.fixture(scope="module")
def rsa_signing_material():
    return _new_rsa_signing_material("k1")


def _new_rsa_signing_material(kid: str):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_jwk = RSAAlgorithm.to_jwk(
        private_key.public_key(),
        as_dict=True,
    )
    public_jwk.update({"alg": "RS256", "kid": kid, "use": "sig"})
    return private_pem, public_jwk


def _valid_claims(config: OIDCConfig, **overrides) -> dict:
    claims = {
        "iss": config.issuer,
        "aud": config.audience,
        "exp": int(time.time()) + 3600,
        "sub": "user-1",
        "custom:project_id": "project-1",
        "custom:tenant_id": "tenant-1",
        "custom:roles": ["member"],
        "scope": "openid chat:invoke",
    }
    claims.update(overrides)
    return claims


def _signed_token(
    config: OIDCConfig,
    signing_material,
    *,
    headers: dict | None = None,
    remove_claims: tuple[str, ...] = (),
    **claim_overrides,
) -> str:
    private_pem, _ = signing_material
    claims = _valid_claims(config, **claim_overrides)
    for claim in remove_claims:
        claims.pop(claim, None)
    return jwt.encode(
        claims,
        private_pem,
        algorithm="RS256",
        headers={"kid": "k1"} if headers is None else headers,
    )


def _cache_signing_key(service: OIDCService, signing_material) -> None:
    _, public_jwk = signing_material
    service._install_jwks(
        {"keys": [public_jwk]},
        service._config.issuer,
    )


def _install_oidc_transport(monkeypatch, handler):
    real_async_client = httpx.AsyncClient
    requests = []
    client_options = []

    def recording_handler(request):
        requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(recording_handler)

    def client_factory(**kwargs):
        client_options.append(kwargs)
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    return requests, client_options


def _json_response(payload, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


class _AsyncChunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes, delay: float = 0) -> None:
        self._chunks = chunks
        self._delay = delay

    async def __aiter__(self):
        for chunk in self._chunks:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield chunk

    async def aclose(self) -> None:
        return None


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
    def test_accepts_valid_signed_token(self, service, rsa_signing_material):
        _cache_signing_key(service, rsa_signing_material)
        token = _signed_token(service._config, rsa_signing_material)

        context = asyncio.run(service.validate_oidc_jwt(token))

        assert context is not None
        assert context.user_id == "user-1"
        assert context.project_id == "project-1"
        assert context.tenant_id == "tenant-1"
        assert context.roles == ["member"]
        assert context.scopes == ["openid", "chat:invoke"]
        assert context.issuer == service._config.issuer
        assert context.subject == "user-1"

    def test_id_token_requires_matching_nonce(
        self,
        service,
        rsa_signing_material,
    ):
        _cache_signing_key(service, rsa_signing_material)
        token = _signed_token(
            service._config,
            rsa_signing_material,
            nonce="one-time-nonce",
            token_use="id",
        )

        accepted = asyncio.run(
            service.validate_id_token(
                token,
                expected_nonce="one-time-nonce",
            )
        )
        rejected = asyncio.run(
            service.validate_id_token(
                token,
                expected_nonce="different-nonce",
            )
        )

        assert accepted is not None
        assert rejected is None

    def test_id_token_rejects_explicit_access_token(
        self,
        service,
        rsa_signing_material,
    ):
        _cache_signing_key(service, rsa_signing_material)
        token = _signed_token(
            service._config,
            rsa_signing_material,
            nonce="one-time-nonce",
            token_use="access",
        )

        assert (
            asyncio.run(
                service.validate_id_token(
                    token,
                    expected_nonce="one-time-nonce",
                )
            )
            is None
        )

    @pytest.mark.parametrize("subject", ["", "   ", True])
    def test_id_token_rejects_invalid_subject(
        self,
        service,
        rsa_signing_material,
        subject,
    ):
        _cache_signing_key(service, rsa_signing_material)
        token = _signed_token(
            service._config,
            rsa_signing_material,
            sub=subject,
            nonce="one-time-nonce",
            token_use="id",
        )

        assert (
            asyncio.run(
                service.validate_id_token(
                    token,
                    expected_nonce="one-time-nonce",
                )
            )
            is None
        )

    def test_preserves_valid_es256_flow(self, service):
        private_key = ec.generate_private_key(ec.SECP256R1())
        private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public_jwk = ECAlgorithm.to_jwk(
            private_key.public_key(),
            as_dict=True,
        )
        public_jwk.update({"alg": "ES256", "kid": "ec1", "use": "sig"})
        service._install_jwks(
            {"keys": [public_jwk]},
            service._config.issuer,
        )
        token = jwt.encode(
            _valid_claims(service._config),
            private_pem,
            algorithm="ES256",
            headers={"kid": "ec1"},
        )

        assert asyncio.run(service.validate_oidc_jwt(token)) is not None

    @pytest.mark.asyncio
    async def test_refreshes_once_for_concurrent_valid_rotated_key(
        self,
        service,
        rsa_signing_material,
        monkeypatch,
    ):
        _cache_signing_key(service, rsa_signing_material)
        rotated_material = _new_rsa_signing_material("k2")
        token = _signed_token(
            service._config,
            rotated_material,
            headers={"kid": "k2"},
        )
        calls = 0

        async def fetch_rotated_jwks():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return {
                "keys": [
                    rsa_signing_material[1],
                    rotated_material[1],
                ]
            }

        monkeypatch.setattr(service, "_fetch_jwks", fetch_rotated_jwks)

        contexts = await asyncio.gather(*(service.validate_oidc_jwt(token) for _ in range(12)))

        assert all(context is not None for context in contexts)
        assert calls == 1

    @pytest.mark.asyncio
    async def test_unknown_kid_flood_is_globally_rate_limited(
        self,
        service,
        rsa_signing_material,
        monkeypatch,
    ):
        _cache_signing_key(service, rsa_signing_material)
        calls = 0

        async def fetch_unchanged_jwks():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return {"keys": [rsa_signing_material[1]]}

        monkeypatch.setattr(service, "_fetch_jwks", fetch_unchanged_jwks)
        tokens = [
            _make_jwt(
                {"alg": "RS256", "kid": f"attacker-{index}"},
                _valid_claims(service._config),
            )
            for index in range(32)
        ]

        results = await asyncio.gather(*(service.validate_oidc_jwt(token) for token in tokens))
        later_result = await service.validate_oidc_jwt(
            _make_jwt(
                {"alg": "RS256", "kid": "another-unknown"},
                _valid_claims(service._config),
            )
        )

        assert all(result is None for result in results)
        assert later_result is None
        assert calls == 1

    @pytest.mark.parametrize(
        "token",
        [
            "x" * (MAX_DIRECT_OIDC_JWT_BYTES + 1),
            _make_jwt(
                {
                    "alg": "RS256",
                    "kid": "k1",
                    "padding": "x" * MAX_DIRECT_OIDC_HEADER_BYTES,
                },
                {"sub": "user-1"},
            ),
            _make_jwt(
                {
                    "alg": "RS256",
                    "kid": "k" * (MAX_DIRECT_OIDC_KID_BYTES + 1),
                },
                {"sub": "user-1"},
            ),
        ],
        ids=("oversized-token", "oversized-header", "oversized-kid"),
    )
    def test_rejects_bounded_token_fields_before_jwks_access(
        self,
        service,
        monkeypatch,
        token,
    ):
        async def unexpected_get_jwks():
            raise AssertionError("invalid token metadata must not access JWKS")

        monkeypatch.setattr(service, "_get_jwks", unexpected_get_jwks)

        assert asyncio.run(service.validate_oidc_jwt(token)) is None

    @pytest.mark.parametrize("algorithm", ["HS256", "RS512", "none"])
    def test_rejects_algorithm_confusion_before_key_fetch(self, service, algorithm, monkeypatch):
        async def unexpected_fetch():
            raise AssertionError("unsupported algorithms must not trigger JWKS fetch")

        monkeypatch.setattr(service, "_fetch_jwks", unexpected_fetch)
        token = _make_jwt(
            {"alg": algorithm, "kid": "k1"},
            _valid_claims(service._config),
        )

        assert asyncio.run(service.validate_oidc_jwt(token)) is None

    @pytest.mark.parametrize("headers", [{}, {"kid": ""}, {"kid": True}])
    def test_rejects_missing_or_malformed_kid(self, service, headers):
        token = _make_jwt(
            {"alg": "RS256", **headers},
            _valid_claims(service._config),
        )

        assert asyncio.run(service.validate_oidc_jwt(token)) is None

    def test_rejects_unknown_kid(
        self,
        service,
        rsa_signing_material,
        monkeypatch,
    ):
        _cache_signing_key(service, rsa_signing_material)
        token = _signed_token(
            service._config,
            rsa_signing_material,
            headers={"kid": "unknown"},
        )

        async def fetch_unchanged_jwks():
            return {"keys": [rsa_signing_material[1]]}

        monkeypatch.setattr(service, "_fetch_jwks", fetch_unchanged_jwks)

        assert asyncio.run(service.validate_oidc_jwt(token)) is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("kty", "oct"),
            ("alg", "HS256"),
            ("use", "enc"),
            ("key_ops", ["sign"]),
        ],
    )
    def test_rejects_incompatible_exact_kid_jwk(self, service, rsa_signing_material, field, value):
        _, public_jwk = rsa_signing_material
        confused_jwk = {**public_jwk, field: value}
        service._install_jwks(
            {"keys": [confused_jwk]},
            service._config.issuer,
        )
        token = _signed_token(service._config, rsa_signing_material)

        assert asyncio.run(service.validate_oidc_jwt(token)) is None

    @pytest.mark.parametrize("claim", ["iss", "aud", "exp", "sub"])
    def test_rejects_missing_required_claim(self, service, rsa_signing_material, claim):
        _cache_signing_key(service, rsa_signing_material)
        token = _signed_token(
            service._config,
            rsa_signing_material,
            remove_claims=(claim,),
        )

        assert asyncio.run(service.validate_oidc_jwt(token)) is None

    @pytest.mark.parametrize("subject", ["", "   ", True, {"id": "user-1"}])
    def test_rejects_invalid_subject(self, service, rsa_signing_material, subject):
        _cache_signing_key(service, rsa_signing_material)
        token = _signed_token(
            service._config,
            rsa_signing_material,
            sub=subject,
        )

        assert asyncio.run(service.validate_oidc_jwt(token)) is None

    def test_rejects_expired_token(self, service, rsa_signing_material):
        _cache_signing_key(service, rsa_signing_material)
        token = _signed_token(
            service._config,
            rsa_signing_material,
            exp=int(time.time()) - 3600,
        )

        assert asyncio.run(service.validate_oidc_jwt(token)) is None

    def test_rejects_wrong_issuer(self, service, rsa_signing_material):
        _cache_signing_key(service, rsa_signing_material)
        token = _signed_token(
            service._config,
            rsa_signing_material,
            iss="https://wrong-issuer.example",
        )

        assert asyncio.run(service.validate_oidc_jwt(token)) is None

    def test_rejects_wrong_audience(self, service, rsa_signing_material):
        _cache_signing_key(service, rsa_signing_material)
        token = _signed_token(
            service._config,
            rsa_signing_material,
            aud="wrong-audience",
        )

        assert asyncio.run(service.validate_oidc_jwt(token)) is None

    def test_accepts_expected_audience_in_array(self, service, rsa_signing_material):
        _cache_signing_key(service, rsa_signing_material)
        token = _signed_token(
            service._config,
            rsa_signing_material,
            aud=["another-service", service._config.audience],
        )

        assert asyncio.run(service.validate_oidc_jwt(token)) is not None

    def test_accepts_a_dedicated_certification_audience(
        self,
        config,
        rsa_signing_material,
    ):
        multi_config = OIDCConfig(
            issuer=config.issuer,
            audience="axonllm-app,axonllm-certification",
        )
        service = OIDCService(multi_config)
        _cache_signing_key(service, rsa_signing_material)
        token = _signed_token(
            multi_config,
            rsa_signing_material,
            aud="axonllm-certification",
        )

        assert asyncio.run(service.validate_oidc_jwt(token)) is not None

    @pytest.mark.parametrize(
        "audience",
        [
            "axonllm-app,",
            "axonllm-app,axonllm-app",
            ",".join(f"client-{index}" for index in range(9)),
        ],
    )
    def test_rejects_malformed_configured_audience_lists(
        self,
        config,
        rsa_signing_material,
        audience,
    ):
        configured = OIDCConfig(
            issuer=config.issuer,
            audience=audience,
        )
        service = OIDCService(configured)
        token = _signed_token(
            configured,
            rsa_signing_material,
            aud="axonllm-app",
        )

        assert asyncio.run(service.validate_oidc_jwt(token)) is None

    @pytest.mark.parametrize(
        "audience",
        [
            ["another-service"],
            [True, "axonllm-app"],
            {"service": "axonllm-app"},
        ],
    )
    def test_rejects_invalid_audience_array(self, service, rsa_signing_material, audience):
        _cache_signing_key(service, rsa_signing_material)
        token = _signed_token(
            service._config,
            rsa_signing_material,
            aud=audience,
        )

        assert asyncio.run(service.validate_oidc_jwt(token)) is None

    @pytest.mark.parametrize(
        ("claim", "value"),
        [
            ("custom:roles", {"admin": False}),
            ("custom:roles", True),
            ("custom:roles", ["member", False]),
            ("custom:roles", ["member", ""]),
            ("scope", ["openid", "profile"]),
            ("scope", {"admin": True}),
            ("scope", False),
            ("custom:tenant_id", {"id": "tenant-1"}),
            ("custom:tenant_id", True),
            ("custom:tenant_id", ["tenant-1"]),
            ("custom:project_id", {"id": "project-1"}),
            ("custom:project_id", False),
            ("custom:project_id", ["project-1"]),
        ],
    )
    def test_rejects_type_confused_application_claims(self, service, rsa_signing_material, claim, value):
        _cache_signing_key(service, rsa_signing_material)
        token = _signed_token(
            service._config,
            rsa_signing_material,
            **{claim: value},
        )

        assert asyncio.run(service.validate_oidc_jwt(token)) is None

    @pytest.mark.parametrize(
        "config",
        [
            OIDCConfig(issuer="", audience="axonllm-app"),
            OIDCConfig(issuer="https://issuer.example", audience=""),
        ],
    )
    def test_rejects_unconfigured_issuer_or_audience(self, config, rsa_signing_material):
        service = OIDCService(config)
        token = _signed_token(config, rsa_signing_material)

        assert asyncio.run(service.validate_oidc_jwt(token)) is None


class TestOIDCDiscoveryTransport:
    @pytest.mark.asyncio
    async def test_fetches_valid_jwks_with_hardened_http_client(
        self,
        service,
        rsa_signing_material,
        monkeypatch,
    ):
        jwks_uri = f"{service._config.issuer}/jwks.json"

        def handler(request):
            if request.url.path.endswith("openid-configuration"):
                return _json_response(
                    {
                        "issuer": service._config.issuer,
                        "jwks_uri": jwks_uri,
                    }
                )
            assert str(request.url) == jwks_uri
            return _json_response({"keys": [rsa_signing_material[1]]})

        requests, client_options = _install_oidc_transport(
            monkeypatch,
            handler,
        )

        jwks = await service._fetch_jwks()

        assert jwks == {"keys": [rsa_signing_material[1]]}
        assert len(requests) == 2
        assert client_options[0]["trust_env"] is False
        assert client_options[0]["follow_redirects"] is False
        assert client_options[0]["timeout"].connect == OIDC_HTTP_CONNECT_TIMEOUT_SECONDS
        assert client_options[0]["timeout"].read == OIDC_HTTP_TOTAL_TIMEOUT_SECONDS
        assert all(request.headers["accept-encoding"] == "identity" for request in requests)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "issuer",
        [
            "http://issuer.example.test",
            "https://localhost",
            "https://127.0.0.1",
            "https://[::1]",
            "https://2130706433",
            "https://0177.0.0.1",
            "https://0x7f000001",
            "https://user:password@issuer.example.test",
            "https://issuer.example.test/path?query=1",
            "https://issuer.example.test/path#fragment",
            " https://issuer.example.test",
        ],
    )
    async def test_rejects_unsafe_issuer_before_network(
        self,
        issuer,
        monkeypatch,
    ):
        def unexpected_client(**_kwargs):
            raise AssertionError("unsafe issuer must not create an HTTP client")

        monkeypatch.setattr(httpx, "AsyncClient", unexpected_client)
        service = OIDCService(
            OIDCConfig(
                issuer=issuer,
                audience="axonllm-app",
            )
        )

        assert await service._fetch_jwks() is None

    @pytest.mark.asyncio
    async def test_requires_exact_discovery_issuer(
        self,
        service,
        monkeypatch,
    ):
        def handler(_request):
            return _json_response(
                {
                    "issuer": f"{service._config.issuer}/",
                    "jwks_uri": f"{service._config.issuer}/jwks.json",
                }
            )

        requests, _ = _install_oidc_transport(monkeypatch, handler)

        assert await service._fetch_jwks() is None
        assert len(requests) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "jwks_uri",
        [
            "http://cognito-idp.us-east-1.amazonaws.com/jwks.json",
            "https://keys.example.test/jwks.json",
            "https://127.0.0.1/jwks.json",
            "https://user@cognito-idp.us-east-1.amazonaws.com/jwks.json",
        ],
        ids=("http", "cross-origin", "ip-literal", "userinfo"),
    )
    async def test_rejects_unsafe_discovered_jwks_uri(
        self,
        service,
        monkeypatch,
        jwks_uri,
    ):
        def handler(_request):
            return _json_response(
                {
                    "issuer": service._config.issuer,
                    "jwks_uri": jwks_uri,
                }
            )

        requests, _ = _install_oidc_transport(monkeypatch, handler)

        assert await service._fetch_jwks() is None
        assert len(requests) == 1

    @pytest.mark.asyncio
    async def test_does_not_follow_discovery_redirect(
        self,
        service,
        monkeypatch,
    ):
        def handler(_request):
            return httpx.Response(
                302,
                headers={"location": (f"{service._config.issuer}/redirected-discovery")},
            )

        requests, client_options = _install_oidc_transport(
            monkeypatch,
            handler,
        )

        assert await service._fetch_jwks() is None
        assert len(requests) == 1
        assert client_options[0]["follow_redirects"] is False

    @pytest.mark.asyncio
    async def test_enforces_end_to_end_discovery_deadline(
        self,
        service,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "src.gateway.auth.oidc_service.OIDC_HTTP_TOTAL_TIMEOUT_SECONDS",
            0.01,
        )

        def handler(_request):
            return httpx.Response(
                200,
                stream=_AsyncChunks(
                    b'{"issuer":"never-read"}',
                    delay=1,
                ),
                headers={"content-type": "application/json"},
            )

        requests, _ = _install_oidc_transport(monkeypatch, handler)

        assert await service._fetch_jwks() is None
        assert len(requests) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kind",
        [
            "malformed",
            "duplicate",
            "oversized",
            "wrong-content-type",
            "compressed",
        ],
    )
    async def test_rejects_malformed_or_unbounded_discovery(
        self,
        service,
        monkeypatch,
        kind,
    ):
        valid_body = json.dumps(
            {
                "issuer": service._config.issuer,
                "jwks_uri": f"{service._config.issuer}/jwks.json",
            }
        ).encode()
        bodies = {
            "malformed": b"{",
            "duplicate": (b'{"issuer":"first","issuer":"second","jwks_uri":"https://issuer.example.test/jwks"}'),
            "oversized": b"x" * (MAX_OIDC_DISCOVERY_BYTES + 1),
            "wrong-content-type": valid_body,
            "compressed": valid_body,
        }
        headers = {"content-type": "application/json"}
        if kind == "wrong-content-type":
            headers["content-type"] = "text/html"
        if kind == "compressed":
            headers["content-encoding"] = "gzip"

        def handler(_request):
            if kind == "oversized":
                return httpx.Response(
                    200,
                    stream=_AsyncChunks(bodies[kind]),
                    headers=headers,
                )
            return httpx.Response(
                200,
                content=bodies[kind],
                headers=headers,
            )

        requests, _ = _install_oidc_transport(monkeypatch, handler)

        assert await service._fetch_jwks() is None
        assert len(requests) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kind",
        ["malformed", "duplicate", "oversized", "too-many-keys"],
    )
    async def test_rejects_malformed_or_unbounded_jwks(
        self,
        service,
        monkeypatch,
        kind,
    ):
        jwks_uri = f"{service._config.issuer}/jwks.json"

        def handler(request):
            if request.url.path.endswith("openid-configuration"):
                return _json_response(
                    {
                        "issuer": service._config.issuer,
                        "jwks_uri": jwks_uri,
                    }
                )
            if kind == "too-many-keys":
                return _json_response({"keys": [{"kid": f"key-{index}"} for index in range(MAX_OIDC_JWKS_KEYS + 1)]})
            bodies = {
                "malformed": b"{",
                "duplicate": (b'{"keys":[{"kid":"first"}],"keys":[{"kid":"second"}]}'),
                "oversized": b"x" * (MAX_OIDC_JWKS_BYTES + 1),
            }
            if kind == "oversized":
                return httpx.Response(
                    200,
                    stream=_AsyncChunks(bodies[kind]),
                    headers={"content-type": "application/jwk-set+json"},
                )
            return httpx.Response(
                200,
                content=bodies[kind],
                headers={"content-type": "application/jwk-set+json"},
            )

        requests, _ = _install_oidc_transport(monkeypatch, handler)

        assert await service._fetch_jwks() is None
        assert len(requests) == 2


class TestJWKSCache:
    def test_cache_used_within_ttl(self, service, monkeypatch):
        service._install_jwks(
            {"keys": [{"kid": "k1"}]},
            service._config.issuer,
        )

        async def unexpected_fetch():
            raise AssertionError("fresh cache should not be refreshed")

        monkeypatch.setattr(service, "_fetch_jwks", unexpected_fetch)

        result = asyncio.run(service._get_jwks())
        assert result == {"keys": [{"kid": "k1"}]}

    def test_find_key_by_kid(self, service):
        jwks = {"keys": [{"kid": "a"}, {"kid": "b"}, {"kid": "c"}]}
        assert service._find_key(jwks, "b") == {"kid": "b"}
        assert service._find_key(jwks, "x") is None

    def test_find_key_rejects_missing_kid(self, service):
        jwks = {"keys": [{"kid": "a"}, {"kid": "b"}]}
        assert service._find_key(jwks, None) is None

    def test_find_key_rejects_duplicate_kid(self, service):
        jwks = {"keys": [{"kid": "a"}, {"kid": "a"}]}
        assert service._find_key(jwks, "a") is None

    def test_stale_cache_is_not_used_when_refresh_fails(self, service, rsa_signing_material, monkeypatch):
        _, public_jwk = rsa_signing_material
        service._install_jwks(
            {"keys": [public_jwk]},
            service._config.issuer,
        )
        service._jwks_fetched_at = time.monotonic() - service._config.jwks_cache_ttl - 1

        async def failed_fetch():
            return None

        monkeypatch.setattr(service, "_fetch_jwks", failed_fetch)
        token = _signed_token(service._config, rsa_signing_material)

        assert asyncio.run(service.validate_oidc_jwt(token)) is None
        assert service._jwks_cache == {}

    def test_fetch_exception_fails_closed_without_raw_error(
        self,
        service,
        monkeypatch,
        caplog,
    ):
        async def failed_fetch():
            raise RuntimeError("provider-response-secret")

        monkeypatch.setattr(service, "_fetch_jwks", failed_fetch)

        with caplog.at_level(logging.DEBUG):
            assert asyncio.run(service._get_jwks()) is None
        assert service._jwks_cache == {}
        assert "provider-response-secret" not in caplog.text

    def test_cache_is_bound_to_issuer(
        self,
        service,
        rsa_signing_material,
        monkeypatch,
    ):
        _cache_signing_key(service, rsa_signing_material)
        service._config.issuer = "https://replacement-issuer.example"

        async def failed_fetch():
            return None

        monkeypatch.setattr(service, "_fetch_jwks", failed_fetch)

        assert asyncio.run(service._get_jwks()) is None
        assert service._jwks_cache == {}

    def test_cache_ttl_is_bounded(self, service, monkeypatch):
        service._config.jwks_cache_ttl = MAX_JWKS_CACHE_TTL_SECONDS * 100
        service._install_jwks(
            {"keys": [{"kid": "k1"}]},
            service._config.issuer,
        )
        service._jwks_fetched_at = time.monotonic() - MAX_JWKS_CACHE_TTL_SECONDS - 1

        async def failed_fetch():
            return None

        monkeypatch.setattr(service, "_fetch_jwks", failed_fetch)

        assert asyncio.run(service._get_jwks()) is None
