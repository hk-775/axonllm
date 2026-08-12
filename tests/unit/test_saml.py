"""Security contract for Cognito-managed enterprise SAML federation."""

from __future__ import annotations

from dataclasses import replace

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.gateway.auth.principal import (
    CanonicalPrincipalResolver,
    InMemoryPrincipalRepository,
)
from src.gateway.auth.saml_routes import SamlAPI, create_saml_routes
from src.gateway.auth.saml_service import (
    MANAGED_COGNITO_MODE,
    SamlConfig,
    SamlError,
    SamlService,
    load_saml_config,
)
from src.gateway.middleware.auth import AuthMiddleware
from src.gateway.models import (
    AuthMethod,
    Principal,
    RequestContext,
    TenantRole,
)

COGNITO_ISSUER = (
    "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Example"
)
ALB_KEY_ISSUER = (
    "https://public-keys.auth.elb.us-east-1.amazonaws.com"
)
ALB_SIGNER_ARN = (
    "arn:aws:elasticloadbalancing:us-east-1:123456789012:"
    "loadbalancer/app/axon/1234"
)
ALB_CLIENT_ID = "control-plane-client"


def _managed_config(**overrides: object) -> SamlConfig:
    values: dict[str, object] = {
        "federation_mode": MANAGED_COGNITO_MODE,
        "login_path": "/admin/dashboard",
        "deployment_profile": "production",
        "auth_mode": "ENFORCE",
        "canonical_identity_required": True,
        "control_plane_only": True,
        "aws_region": "us-east-1",
        "oidc_issuer": COGNITO_ISSUER,
        "oidc_audience": ALB_CLIENT_ID,
        "alb_signer_arn": ALB_SIGNER_ARN,
        "alb_client_id": ALB_CLIENT_ID,
        "alb_issuer": ALB_KEY_ISSUER,
        "legacy_direct_configuration": False,
    }
    values.update(overrides)
    return SamlConfig(**values)


def _client(service: SamlService) -> TestClient:
    app = Starlette(routes=create_saml_routes(SamlAPI(service)))
    return TestClient(app)


class TestManagedConfiguration:
    def test_complete_managed_contract_is_enabled(self):
        config = _managed_config()

        assert config.enabled is True
        assert config.configuration_error is None

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("federation_mode", ""),
            ("federation_mode", "direct"),
            ("deployment_profile", "development"),
            ("auth_mode", "LOG_ONLY"),
            ("canonical_identity_required", False),
            ("control_plane_only", False),
            ("oidc_issuer", "https://login.example.com"),
            ("oidc_audience", "different-client"),
            ("alb_signer_arn", ""),
            ("alb_issuer", "http://public-keys.example.com"),
            ("login_path", "/saml/acs"),
            ("legacy_direct_configuration", True),
        ],
    )
    def test_incomplete_or_conflicting_contract_is_disabled(
        self,
        field_name,
        value,
    ):
        config = replace(_managed_config(), **{field_name: value})

        assert config.enabled is False
        assert config.configuration_error

    def test_loader_detects_retired_direct_sp_inputs(self):
        config = load_saml_config(
            deployment_profile="production",
            auth_mode="ENFORCE",
            canonical_identity_required=True,
            control_plane_only=True,
            aws_region="us-east-1",
            oidc_issuer=COGNITO_ISSUER,
            oidc_audience=ALB_CLIENT_ID,
            alb_signer_arn=ALB_SIGNER_ARN,
            alb_client_id=ALB_CLIENT_ID,
            alb_issuer=ALB_KEY_ISSUER,
            environ={
                "AXON_SAML_FEDERATION_MODE": MANAGED_COGNITO_MODE,
                "AXON_SAML_IDP_CERT_FILE": "/not/read/by/axonllm",
            },
        )

        assert config.legacy_direct_configuration is True
        assert config.enabled is False
        assert "direct-SP" in (config.configuration_error or "")

    def test_loader_uses_only_managed_switch_and_safe_target(self):
        config = load_saml_config(
            deployment_profile="production",
            auth_mode="ENFORCE",
            canonical_identity_required=True,
            control_plane_only=True,
            aws_region="us-east-1",
            oidc_issuer=COGNITO_ISSUER,
            oidc_audience=ALB_CLIENT_ID,
            alb_signer_arn=ALB_SIGNER_ARN,
            alb_client_id=ALB_CLIENT_ID,
            alb_issuer=ALB_KEY_ISSUER,
            environ={
                "AXON_SAML_FEDERATION_MODE": MANAGED_COGNITO_MODE,
                "AXON_SAML_LOGIN_PATH": "/chat",
            },
        )

        assert config.enabled is True
        assert SamlService(config).login_target() == "/chat"


class TestManagedLogin:
    def test_disabled_handoff_fails_closed(self):
        response = _client(SamlService(SamlConfig())).get("/saml/login")

        assert response.status_code == 503
        assert response.json()["error"]["code"] == (
            "managed_cognito_federation_required"
        )
        assert response.headers["cache-control"] == "no-store"

    def test_handoff_redirects_to_protected_same_origin_path(self):
        response = _client(SamlService(_managed_config())).get(
            "/saml/login",
            params={"return_to": "/chat?project=alpha"},
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["location"] == "/chat?project=alpha"
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers

    @pytest.mark.parametrize(
        "return_to",
        [
            "https://evil.example/login",
            "//evil.example/login",
            "relative/path",
            "/\\evil.example/login",
            "/saml/acs",
            "/%2573aml/acs",
            "/scim/v2/Users",
            "/oauth2/idpresponse",
            "/safe/../saml/acs",
            "/safe/%252e%252e/saml/acs",
            "/chat%250dredirect",
            "/chat%ZZ",
            "/",
            "/health",
        ],
    )
    def test_unsafe_return_target_is_rejected(self, return_to):
        response = _client(SamlService(_managed_config())).get(
            "/saml/login",
            params={"return_to": return_to},
            follow_redirects=False,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_return_to"
        assert "location" not in response.headers

    @pytest.mark.parametrize(
        "query",
        [
            "return_to=%2Fchat&return_to=%2Fadmin%2Fdashboard",
            "RelayState=https%3A%2F%2Fevil.example",
            "relay_state=%2Fchat",
            "SAMLRequest=not-an-authn-request",
        ],
    )
    def test_ambiguous_or_saml_protocol_parameters_are_rejected(self, query):
        response = _client(SamlService(_managed_config())).get(
            f"/saml/login?{query}",
            follow_redirects=False,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == (
            "invalid_login_parameters"
        )


class TestDirectSpIsDisabled:
    def test_acs_never_parses_or_echoes_assertion(self):
        response = _client(SamlService(_managed_config())).post(
            "/saml/acs",
            content=b"SAMLResponse=secret-signed-assertion",
            headers={
                "content-type": "application/x-www-form-urlencoded",
            },
        )

        assert response.status_code == 410
        assert response.json()["error"]["code"] == (
            "managed_cognito_federation_required"
        )
        assert "secret-signed-assertion" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers

    def test_app_sp_metadata_is_permanently_unavailable(self):
        response = _client(SamlService(_managed_config())).get(
            "/saml/metadata"
        )

        assert response.status_code == 410
        assert response.json()["error"]["code"] == "use_cognito_sp_metadata"
        assert response.headers["content-type"].startswith(
            "application/json"
        )

    def test_service_api_cannot_be_used_as_a_direct_sp(self):
        service = SamlService(_managed_config())

        with pytest.raises(SamlError, match="direct SAML assertions"):
            service.handle_acs("assertion")
        with pytest.raises(SamlError, match="not a SAML service provider"):
            service.sp_metadata()


def test_only_reviewed_saml_paths_bypass_authentication():
    async def handler(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/saml/debug", handler)])
    app.add_middleware(AuthMiddleware, mode="ENFORCE")

    response = TestClient(app).get("/saml/debug")

    assert response.status_code == 401


def test_managed_identity_uses_canonical_principal_authority():
    subject = "cognito-subject"

    class OIDCVerifier:
        async def validate_alb_jwt(
            self,
            token: str,
            expected_subject: str,
        ) -> RequestContext:
            assert token == "alb-token"
            assert expected_subject == subject
            return RequestContext(
                user_id="untrusted-claim-user",
                project_id="project-a",
                roles=["platform_admin"],
                scopes=["*"],
                auth_method=AuthMethod.OIDC_JWT,
                tenant_id="tenant-a",
                issuer=COGNITO_ISSUER,
                subject=subject,
            )

    principal = Principal(
        principal_id="scim:user-a",
        tenant_id="tenant-a",
        subject=subject,
        issuer=COGNITO_ISSUER,
        roles=frozenset({TenantRole.TENANT_MEMBER}),
        auth_method=AuthMethod.OIDC_JWT,
        project_ids=frozenset({"project-a"}),
        authorization_version=7,
        email="alice@example.com",
    )
    resolver = CanonicalPrincipalResolver(
        InMemoryPrincipalRepository([principal])
    )

    async def whoami(request: Request) -> JSONResponse:
        context = request.state.context
        resolved = request.state.principal
        return JSONResponse(
            {
                "user_id": context.user_id,
                "tenant_id": context.tenant_id,
                "roles": context.roles,
                "authorization_version": context.authorization_version,
                "principal_id": resolved.principal_id,
            }
        )

    app = Starlette(routes=[Route("/protected", whoami)])
    app.add_middleware(
        AuthMiddleware,
        oidc_service=OIDCVerifier(),
        principal_resolver=resolver,
        require_canonical_principal=True,
        mode="ENFORCE",
    )

    response = TestClient(app).get(
        "/protected",
        headers={
            "x-amzn-oidc-data": "alb-token",
            "x-amzn-oidc-identity": subject,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "scim:user-a",
        "tenant_id": "tenant-a",
        "roles": ["tenant_member"],
        "authorization_version": 7,
        "principal_id": "scim:user-a",
    }
