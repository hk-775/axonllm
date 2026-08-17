from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from scripts.ci.validate_workflows import (
    WorkflowLoader,
    validate_workflow,
)
from src.gateway.agentcore_setup import AgentCoreSetupConfig
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    TenantRole,
)


ROOT = Path(__file__).resolve().parents[2]
OPERATIONS = ROOT / "scripts" / "operations"
sys.path.insert(0, str(OPERATIONS))

import certify_agentcore as launch_certification
import certify_external_oidc_agentcore as external_certification


WORKFLOW = ROOT / ".github" / "workflows" / "certify-agentcore-external-oidc.yml"
MANAGED_WORKFLOW = ROOT / ".github" / "workflows" / "deploy-agentcore-production.yml"
ACCOUNT = "123456789012"
REGION = "us-east-1"
DIGEST = "a" * 64
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/axonllm/agentcore@sha256:{DIGEST}"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/axonllm-AbCdEf1234"
CANDIDATE = "candidate_" + "b" * 32
ISSUER = "https://idp.example.com/oauth2/default"
MIXUP_ISSUER = "https://other-idp.example.net/oauth2/default"
AUDIENCE = "api://axonllm"
TENANT_CLAIM = "https://axonllm.example/tenant"
PROJECT_CLAIM = "https://axonllm.example/project"
NOW = 2_000_000_000
EXTERNAL_STACK = "AxonLLMAgentCoreStack-external"
WORKFLOW_REF = "AxonLLM/axonllm/.github/workflows/certify-agentcore-external-oidc.yml@refs/heads/main"
PARENT_WORKFLOW_REF = "AxonLLM/axonllm/.github/workflows/launch-agentcore-production.yml@refs/heads/main"


def _workflow() -> dict[str, Any]:
    value = yaml.load(
        WORKFLOW.read_text(encoding="utf-8"),
        Loader=WorkflowLoader,
    )
    assert isinstance(value, dict)
    return value


def _launch_providers(*, include_ai21: bool) -> set[str]:
    providers = set(launch_certification.PRODUCTION_LAUNCH_PROVIDERS)
    if include_ai21:
        providers.add("ai21")
    return providers


def _setup(
    *,
    include_ai21: bool = False,
) -> AgentCoreSetupConfig:
    return AgentCoreSetupConfig.from_mapping(
        {
            "schema_version": 2,
            "target": "agentcore",
            "identity_mode": "external-oidc",
            "aws_region": REGION,
            "tenant": {
                "tenant_id": "tenant-a",
                "project_id": "project-a",
                "project_name": "Production",
                "budget_limit": 1000,
            },
            "admin": {
                "user_name": "admin@example.com",
                "email": "admin@example.com",
                "display_name": "Tenant Admin",
                "subject": "reviewed-admin-subject",
            },
            "runtime": {
                "verified_image_uri": IMAGE,
                "bedrock_invoke_resource_arns": [
                    ("arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0")
                ],
                "approved_https_prefix_list_id": "pl-123abc",
                "enabled_providers": sorted(_launch_providers(include_ai21=include_ai21)),
            },
            "external_oidc": {
                "issuer": ISSUER,
                "discovery_url": (f"{ISSUER}/.well-known/openid-configuration"),
                "client_id": "axonllm-client",
                "audience": AUDIENCE,
                "tenant_claim": TENANT_CLAIM,
                "project_claim": PROJECT_CLAIM,
            },
        }
    )


def _certification(
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
    include_ai21: bool = False,
) -> launch_certification.CertificationConfig:
    providers = _launch_providers(include_ai21=include_ai21)
    value: dict[str, Any] = {
        "schemaVersion": 1,
        "profile": "production-launch",
        "region": REGION,
        "runtimeArn": RUNTIME_ARN,
        "qualifier": CANDIDATE,
        "identities": {
            "activeCredentialEnv": "ACTIVE_IDENTITY",
            "inactiveCredentialEnv": "INACTIVE_IDENTITY",
            "ungrantedCredentialEnv": "UNGRANTED_IDENTITY",
            "crossTenantCredentialEnv": "CROSS_TENANT_IDENTITY",
        },
        "providers": [
            {
                "provider": provider,
                "model": f"{provider}-certification",
                "features": sorted(launch_certification.PRODUCTION_PROVIDER_FEATURES_BY_PROVIDER[provider]),
            }
            for provider in sorted(providers)
        ],
    }
    if tenant_id is not None or project_id is not None:
        value["identities"]["adminCredentialEnv"] = "ADMIN_IDENTITY"
        value["identities"]["viewerCredentialEnv"] = "VIEWER_IDENTITY"
        value["tenantConfig"] = {
            "tenantId": tenant_id,
            "projectId": project_id,
        }
    return launch_certification.parse_config(value)


def _runtime_binding() -> external_certification.RuntimeBinding:
    return external_certification.RuntimeBinding(
        stack_name=EXTERNAL_STACK,
        stack_id=(
            f"arn:aws:cloudformation:us-east-1:123456789012:stack/{EXTERNAL_STACK}/11111111-2222-3333-4444-555555555555"
        ),
        stack_status="UPDATE_COMPLETE",
        runtime_arn=RUNTIME_ARN,
        runtime_version="7",
        endpoint_name=CANDIDATE,
        endpoint_arn=(f"{RUNTIME_ARN}/runtime-endpoint/{CANDIDATE}"),
        image=IMAGE,
        table_name="axonllm-agentcore-state-external",
        endpoint_status="READY",
    )


def _full_launch_report(
    *,
    include_ai21: bool = False,
) -> tuple[
    dict[str, Any],
    launch_certification.CertificationConfig,
]:
    certification = _certification(include_ai21=include_ai21)
    provider_features = {case.provider: sorted(case.features) for case in certification.providers}
    checks = [
        {
            "category": category,
            "passed": True,
            "responseSha256": "f" * 64,
            "transportError": None,
        }
        for category in (
            "missing_jwt_denied",
            "invalid_jwt_denied",
            "inactive_membership_denied",
            "missing_project_grant_denied",
            "cross_tenant_denied",
            "payload_identity_rejected",
            "liveness",
            "dependency_readiness",
            "model_listing",
        )
    ]
    tool_categories = (
        "provider_tool_call",
        "provider_tool_required",
        "provider_tool_continuation",
        "provider_tool_none",
        "provider_tool_stream",
    )
    for case in certification.providers:
        categories = [
            "provider_completion",
            "provider_stream",
        ]
        if "tool_calling" in case.features:
            categories.extend(tool_categories)
        checks.extend(
            {
                "category": category,
                "provider": case.provider,
                "model": case.model,
                "passed": True,
                "responseSha256": "f" * 64,
                "transportError": None,
            }
            for category in categories
        )
    return (
        {
            "schema": launch_certification.REPORT_SCHEMA,
            "generatedAt": datetime.fromtimestamp(
                NOW,
                tz=timezone.utc,
            ).isoformat(),
            "overallStatus": "PASS",
            "endpoint": external_certification._endpoint_metadata(
                _runtime_binding(),
                REGION,
            ),
            "summary": {
                "checkCount": len(checks),
                "passed": len(checks),
                "failed": 0,
                "providerCount": len(certification.providers),
                "profile": (launch_certification.PRODUCTION_LAUNCH_PROFILE),
                "providerFeatures": provider_features,
                "tenantConfigRbacExercised": False,
                "agentcoreHttpsInvoked": True,
            },
            "checks": checks,
        },
        certification,
    )


def _base64url(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _key_material(
    issuer: str,
    *,
    kid: str,
) -> tuple[Any, str, external_certification.IssuerMaterial]:
    private = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    numbers = private.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": kid,
        "alg": "RS256",
        "use": "sig",
        "n": _base64url(numbers.n),
        "e": _base64url(numbers.e),
    }
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    freshness = external_certification.Freshness(
        date=datetime.fromtimestamp(NOW, tz=timezone.utc),
        max_age_seconds=3600,
        current_age_seconds=0,
    )
    material = external_certification.IssuerMaterial(
        issuer=issuer,
        discovery_url=f"{issuer}/.well-known/openid-configuration",
        jwks_uri=f"{issuer}/keys",
        discovery_sha256="1" * 64,
        jwks_sha256="2" * 64,
        discovery_freshness=freshness,
        jwks_freshness=freshness,
        jwks={"keys": [jwk]},
    )
    return private, private_pem, material


def _identity(
    private_pem: str,
    *,
    kid: str,
    issuer: str,
    case: str,
    subject: str,
    audience: str = AUDIENCE,
    tenant: str | None = "tenant-a",
    project: str | None = "project-a",
    issued_at: int = NOW,
    expires_at: int = NOW + 600,
) -> str:
    claims: dict[str, Any] = {
        "iss": issuer,
        "sub": subject,
        "aud": audience,
        "iat": issued_at,
        "exp": expires_at,
        "jti": f"jti-{case}",
    }
    if tenant is not None:
        claims[TENANT_CLAIM] = tenant
    if project is not None:
        claims[PROJECT_CLAIM] = project
    return jwt.encode(
        claims,
        private_pem,
        algorithm="RS256",
        headers={"kid": kid, "typ": "JWT"},
    )


def _token_bundle() -> tuple[
    dict[str, str],
    external_certification.IssuerMaterial,
    external_certification.IssuerMaterial,
]:
    _, expected_pem, expected = _key_material(
        ISSUER,
        kid="expected-key",
    )
    _, mixup_pem, mixup = _key_material(
        MIXUP_ISSUER,
        kid="mixup-key",
    )
    tokens = {
        case: _identity(
            expected_pem,
            kid="expected-key",
            issuer=ISSUER,
            case=case,
            subject=f"subject-{case}",
            tenant=("tenant-cross" if case == "crossTenant" else "tenant-a"),
        )
        for case in (
            "admin",
            "viewer",
            "inactive",
            "ungranted",
            "crossTenant",
        )
    }
    tokens.update(
        {
            "wrongAudience": _identity(
                expected_pem,
                kid="expected-key",
                issuer=ISSUER,
                case="wrongAudience",
                subject="subject-viewer",
                audience="api://wrong",
            ),
            "missingTenant": _identity(
                expected_pem,
                kid="expected-key",
                issuer=ISSUER,
                case="missingTenant",
                subject="subject-viewer",
                tenant=None,
            ),
            "missingProject": _identity(
                expected_pem,
                kid="expected-key",
                issuer=ISSUER,
                case="missingProject",
                subject="subject-viewer",
                project=None,
            ),
            "expired": _identity(
                expected_pem,
                kid="expected-key",
                issuer=ISSUER,
                case="expired",
                subject="subject-viewer",
                issued_at=NOW - 500,
                expires_at=NOW - 10,
            ),
            "issuerMixup": _identity(
                mixup_pem,
                kid="mixup-key",
                issuer=MIXUP_ISSUER,
                case="issuerMixup",
                subject="subject-viewer",
            ),
        }
    )
    return tokens, expected, mixup


def _http_date(timestamp: int) -> str:
    return format_datetime(
        datetime.fromtimestamp(timestamp, tz=timezone.utc),
        usegmt=True,
    )


class _IssuerTransport:
    def __init__(
        self,
        issuer: str,
        *,
        now: int = NOW,
        max_age: int = 300,
        response_date: int = NOW,
        jwks_uri: str | None = None,
    ) -> None:
        self.issuer = issuer
        self.now = now
        self.jwks_uri = jwks_uri or f"{issuer}/keys"
        self.headers = {
            "content-type": "application/json",
            "cache-control": f"public, max-age={max_age}",
            "date": _http_date(response_date),
        }
        self.jwks = {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": "key-1",
                    "alg": "RS256",
                    "use": "sig",
                    "n": "AQAB",
                    "e": "AQAB",
                }
            ]
        }

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers,
        body,
        timeout_seconds,
        maximum_bytes,
    ):
        assert method == "GET"
        assert body is None
        assert timeout_seconds == external_certification.HTTP_TIMEOUT_SECONDS
        assert maximum_bytes == external_certification.MAX_HTTP_BYTES
        if url.endswith("/.well-known/openid-configuration"):
            value = {
                "issuer": self.issuer,
                "jwks_uri": self.jwks_uri,
            }
        else:
            assert url == self.jwks_uri
            value = self.jwks
        raw = json.dumps(value, separators=(",", ":")).encode()
        return external_certification.JsonResponse(
            status_code=200,
            headers=self.headers,
            body=raw,
            value=value,
        )


def _runtime_stack(
    *,
    issuer: str = ISSUER,
) -> dict[str, Any]:
    return {
        "StackId": (
            f"arn:aws:cloudformation:us-east-1:123456789012:stack/{EXTERNAL_STACK}/11111111-2222-3333-4444-555555555555"
        ),
        "StackStatus": "UPDATE_COMPLETE",
        "Parameters": [
            {"ParameterKey": "OidcIssuer", "ParameterValue": issuer},
            {
                "ParameterKey": "OidcDiscoveryUrl",
                "ParameterValue": (f"{ISSUER}/.well-known/openid-configuration"),
            },
            {
                "ParameterKey": "OidcClientIds",
                "ParameterValue": "axonllm-client",
            },
            {
                "ParameterKey": "OidcAudiences",
                "ParameterValue": AUDIENCE,
            },
            {
                "ParameterKey": "OidcTenantClaim",
                "ParameterValue": TENANT_CLAIM,
            },
            {
                "ParameterKey": "OidcProjectClaim",
                "ParameterValue": PROJECT_CLAIM,
            },
            {
                "ParameterKey": "VerifiedImageUri",
                "ParameterValue": IMAGE,
            },
        ],
        "Outputs": [
            {"OutputKey": "RuntimeArn", "OutputValue": RUNTIME_ARN},
            {"OutputKey": "RuntimeVersion", "OutputValue": "7"},
            {
                "OutputKey": "CandidateRuntimeVersion",
                "OutputValue": "7",
            },
            {
                "OutputKey": "CandidateRuntimeEndpointName",
                "OutputValue": CANDIDATE,
            },
            {
                "OutputKey": "CandidateRuntimeEndpointArn",
                "OutputValue": (f"{RUNTIME_ARN}/runtime-endpoint/{CANDIDATE}"),
            },
            {"OutputKey": "RuntimeImageUri", "OutputValue": IMAGE},
            {
                "OutputKey": "RecoveryCutoverMode",
                "OutputValue": "normal",
            },
            {
                "OutputKey": "StateTableName",
                "OutputValue": "axonllm-agentcore-state-external",
            },
            {
                "OutputKey": "SelectedRuntimeStateTableName",
                "OutputValue": "axonllm-agentcore-state-external",
            },
        ],
    }


class _CloudFormation:
    def __init__(self, stack: dict[str, Any]) -> None:
        self.stack = stack

    def describe_stacks(self, **kwargs):
        assert kwargs == {"StackName": EXTERNAL_STACK}
        return {"Stacks": [self.stack]}


class _Control:
    def get_agent_runtime_endpoint(self, **kwargs):
        assert kwargs == {
            "agentRuntimeId": "axonllm-AbCdEf1234",
            "endpointName": CANDIDATE,
        }
        return {
            "agentRuntimeArn": RUNTIME_ARN,
            "agentRuntimeEndpointArn": (f"{RUNTIME_ARN}/runtime-endpoint/{CANDIDATE}"),
            "name": CANDIDATE,
            "status": "READY",
            "liveVersion": "7",
            "targetVersion": "7",
        }


class _Session:
    def __init__(self, stack: dict[str, Any]) -> None:
        self.stack = stack

    def client(self, service_name: str, *, region_name: str):
        assert region_name == REGION
        if service_name == "cloudformation":
            return _CloudFormation(self.stack)
        if service_name == "bedrock-agentcore-control":
            return _Control()
        raise AssertionError(service_name)


class _AgentCoreTransport:
    def __init__(
        self,
        tokens: dict[str, str],
        certification: launch_certification.CertificationConfig,
        *,
        fail_config_reads: set[int] | None = None,
    ) -> None:
        self._cases = {token: case for case, token in tokens.items()}
        self._certification = certification
        self.revision = 3
        self.name = "Production"
        self.config_writes: list[tuple[str, int, str]] = []
        self.config_reads = 0
        self.fail_config_reads = fail_config_reads or set()

    def _config(self) -> dict[str, Any]:
        return {
            "tenant_id": "tenant-a",
            "project_id": "project-a",
            "revision": self.revision,
            "config": {
                "name": self.name,
                "budget_limit": 1000.0,
                "alert_threshold": None,
                "allowed_models": None,
                "guardrail_rules": [],
                "cache_enabled": False,
                "cache_ttl_seconds": 300,
                "semantic_cache_enabled": False,
                "semantic_cache_threshold": None,
                "log_level": "INFO",
                "log_destination": None,
                "prompt_caching_enabled": False,
                "ltm_enabled": False,
                "retention_period_hours": 24,
                "rate_limit_rpm": None,
            },
        }

    @staticmethod
    def _response(
        status_code: int,
        value: dict[str, Any],
    ) -> launch_certification.InvocationObservation:
        return launch_certification.InvocationObservation(
            status_code=status_code,
            latency_ms=1.0,
            content_type="application/json",
            body=json.dumps(value, separators=(",", ":")).encode("utf-8"),
        )

    def __call__(
        self,
        request: launch_certification.InvocationRequest,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> launch_certification.InvocationObservation:
        assert timeout_seconds == self._certification.timeout_seconds
        assert max_response_bytes == self._certification.max_response_bytes
        payload = json.loads(request.payload)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        case = self._cases.get(token)
        action = payload.get("action")

        if "roles" in payload:
            return self._response(
                400,
                {"error": {"code": "untrusted_identity_fields"}},
            )
        if case not in {"admin", "viewer"}:
            return self._response(
                401,
                {"error": {"code": "invalid_runtime_identity"}},
            )
        if action == "list_models":
            return self._response(
                200,
                {"models": [{"name": "claude-sonnet"}]},
            )
        if action == "get_tenant_config":
            self.config_reads += 1
            if self.config_reads in self.fail_config_reads:
                return self._response(
                    503,
                    {"error": {"code": "temporary_failure"}},
                )
            return self._response(200, self._config())
        if action == "update_tenant_config":
            if case == "viewer":
                self.config_writes.append(
                    (
                        case,
                        payload["expected_revision"],
                        payload["config"]["name"],
                    )
                )
                return self._response(
                    403,
                    {"error": {"code": "authorization_denied"}},
                )
            expected = payload["expected_revision"]
            name = payload["config"]["name"]
            self.config_writes.append((case, expected, name))
            if expected != self.revision:
                return self._response(
                    409,
                    {"error": {"code": "tenant_config_write_conflict"}},
                )
            self.revision += 1
            self.name = name
            return self._response(200, self._config())
        raise AssertionError(payload)


def _verified_identities() -> dict[
    str,
    external_certification.VerifiedIdentity,
]:
    identities: dict[
        str,
        external_certification.VerifiedIdentity,
    ] = {}
    for index, case in enumerate(external_certification.TOKEN_CASES):
        identities[case] = external_certification.VerifiedIdentity(
            case=case,
            token=f"header{index}.payload{index}.signature{index}",
            issuer=(MIXUP_ISSUER if case == "issuerMixup" else ISSUER),
            subject=f"subject-{case}",
            audience=(AUDIENCE,),
            expires_at=NOW + 600,
            issued_at=NOW,
            jwt_id=f"jti-{case}",
            tenant_id=("tenant-cross" if case == "crossTenant" else "tenant-a"),
            project_id="project-a",
        )
    return identities


def _principal(
    name: str,
    *,
    role: TenantRole,
    tenant_id: str = "tenant-a",
) -> Principal:
    return Principal(
        principal_id=f"principal-{name}",
        tenant_id=tenant_id,
        subject=f"subject-{name}",
        issuer=ISSUER,
        roles=frozenset({role}),
        auth_method=AuthMethod.OIDC_JWT,
        membership_status=MembershipStatus.ACTIVE,
        project_ids=frozenset({"project-a"}),
        scopes=frozenset({"inference.invoke", "model.list"}),
        authorization_version=1,
    )


def test_certification_credentials_support_legacy_and_tenant_config_contracts() -> None:
    verified = _verified_identities()

    legacy = external_certification._certification_credentials(
        _certification(),
        verified,
    )
    expanded = external_certification._certification_credentials(
        _certification(tenant_id="tenant-a", project_id="project-a"),
        verified,
    )

    assert set(legacy) == {
        "ACTIVE_IDENTITY",
        "INACTIVE_IDENTITY",
        "UNGRANTED_IDENTITY",
        "CROSS_TENANT_IDENTITY",
    }
    assert expanded["ADMIN_IDENTITY"] == verified["admin"].token
    assert expanded["VIEWER_IDENTITY"] == verified["viewer"].token


def test_external_tenant_config_must_match_reviewed_setup() -> None:
    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="tenantConfig does not match",
    ):
        external_certification._validate_configs(
            _setup(),
            _certification(
                tenant_id="tenant-other",
                project_id="project-a",
            ),
            expected_image=IMAGE,
        )


@pytest.mark.parametrize("include_ai21", [False, True])
def test_external_provider_contract_accepts_baseline_and_optional_ai21(
    include_ai21: bool,
) -> None:
    setup = _setup(include_ai21=include_ai21)
    certification = _certification(include_ai21=include_ai21)

    external_certification._validate_configs(
        setup,
        certification,
        expected_image=IMAGE,
    )

    expected = {
        provider: (launch_certification.PRODUCTION_PROVIDER_FEATURES_BY_PROVIDER[provider])
        for provider in _launch_providers(include_ai21=include_ai21)
    }
    assert {case.provider: case.features for case in certification.providers} == expected


@pytest.mark.parametrize(
    ("setup_ai21", "certification_ai21"),
    [(False, True), (True, False)],
)
def test_external_provider_contract_requires_exact_reviewed_match(
    setup_ai21: bool,
    certification_ai21: bool,
) -> None:
    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="must exactly match",
    ):
        external_certification._validate_configs(
            _setup(include_ai21=setup_ai21),
            _certification(include_ai21=certification_ai21),
            expected_image=IMAGE,
        )


@pytest.mark.parametrize("include_ai21", [False, True])
def test_full_launch_evidence_accepts_only_the_canonical_provider_matrix(
    include_ai21: bool,
) -> None:
    report, certification = _full_launch_report(include_ai21=include_ai21)
    expected = {case.provider: case.features for case in certification.providers}

    external_certification._verify_full_launch_certification(
        report,
        binding=_runtime_binding(),
        region=REGION,
    )
    external_certification._verify_full_launch_certification(
        report,
        binding=_runtime_binding(),
        region=REGION,
        expected_provider_features=expected,
    )


def test_full_launch_evidence_must_match_reviewed_provider_selection() -> None:
    report, _ = _full_launch_report(include_ai21=True)
    baseline = _certification()

    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="differ from the reviewed certification",
    ):
        external_certification._verify_full_launch_certification(
            report,
            binding=_runtime_binding(),
            region=REGION,
            expected_provider_features={case.provider: case.features for case in baseline.providers},
        )


@pytest.mark.parametrize(
    ("provider", "features"),
    [
        (
            "fireworks",
            ["completion", "stream", "tool_calling"],
        ),
        (
            "anthropic",
            ["completion", "stream"],
        ),
    ],
)
def test_portable_evidence_rejects_noncanonical_provider_features(
    provider: str,
    features: list[str],
) -> None:
    report, _ = _full_launch_report()
    report["summary"]["providerFeatures"][provider] = features

    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="provider features are incomplete",
    ):
        external_certification._verify_full_launch_certification(
            report,
            binding=_runtime_binding(),
            region=REGION,
        )


def test_portable_evidence_rejects_tool_checks_for_fireworks() -> None:
    report, _ = _full_launch_report()
    report["checks"].append(
        {
            "category": "provider_tool_call",
            "provider": "fireworks",
            "model": "fireworks-certification",
            "passed": True,
            "responseSha256": "f" * 64,
            "transportError": None,
        }
    )
    report["summary"]["checkCount"] += 1
    report["summary"]["passed"] += 1

    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="feature matrix is incomplete",
    ):
        external_certification._verify_full_launch_certification(
            report,
            binding=_runtime_binding(),
            region=REGION,
        )


def test_portable_evidence_rejects_missing_declared_tool_check() -> None:
    report, _ = _full_launch_report()
    report["checks"] = [
        check
        for check in report["checks"]
        if not (check.get("provider") == "anthropic" and check["category"] == "provider_tool_none")
    ]
    report["summary"]["checkCount"] -= 1
    report["summary"]["passed"] -= 1

    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="feature matrix is incomplete",
    ):
        external_certification._verify_full_launch_certification(
            report,
            binding=_runtime_binding(),
            region=REGION,
        )


def test_portable_evidence_rejects_unsupported_optional_provider() -> None:
    report, _ = _full_launch_report(include_ai21=True)
    features = report["summary"]["providerFeatures"].pop("ai21")
    report["summary"]["providerFeatures"]["unsupported"] = features
    for check in report["checks"]:
        if check.get("provider") == "ai21":
            check["provider"] = "unsupported"

    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="does not match the production provider contract",
    ):
        external_certification._verify_full_launch_certification(
            report,
            binding=_runtime_binding(),
            region=REGION,
        )


def test_portable_evidence_rejects_missing_mandatory_provider() -> None:
    report, _ = _full_launch_report()
    report["summary"]["providerFeatures"].pop("xai")
    report["summary"]["providerCount"] -= 1
    removed = [check for check in report["checks"] if check.get("provider") == "xai"]
    report["checks"] = [check for check in report["checks"] if check.get("provider") != "xai"]
    report["summary"]["checkCount"] -= len(removed)
    report["summary"]["passed"] -= len(removed)

    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="does not match the production provider contract",
    ):
        external_certification._verify_full_launch_certification(
            report,
            binding=_runtime_binding(),
            region=REGION,
        )


def test_external_checks_mutate_confirm_and_restore_tenant_config() -> None:
    certification = _certification()
    verified = _verified_identities()
    transport = _AgentCoreTransport(
        {case: identity.token for case, identity in verified.items()},
        certification,
    )
    principals = {
        "admin": _principal(
            "admin",
            role=TenantRole.TENANT_ADMIN,
        ),
        "viewer": _principal(
            "viewer",
            role=TenantRole.TENANT_MEMBER,
        ),
        "crossTenant": _principal(
            "cross",
            role=TenantRole.TENANT_MEMBER,
            tenant_id="tenant-cross",
        ),
    }

    checks = external_certification.run_external_checks(
        certification,
        setup=_setup(),
        verified=verified,
        principals=principals,
        challenge="c" * 64,
        transport=transport,
    )

    assert {check["id"] for check in checks} == (external_certification.REQUIRED_CHECKS)
    assert all(check["passed"] is True for check in checks)
    assert transport.name == "Production"
    assert transport.revision == 5
    assert transport.config_writes == [
        ("viewer", 3, "OIDC certification " + "c" * 24),
        ("admin", 3, "OIDC certification " + "c" * 24),
        ("admin", 4, "Production"),
    ]


def test_external_checks_restore_config_after_ambiguous_confirmation() -> None:
    certification = _certification()
    verified = _verified_identities()
    transport = _AgentCoreTransport(
        {case: identity.token for case, identity in verified.items()},
        certification,
        fail_config_reads={3},
    )
    principals = {
        "admin": _principal(
            "admin",
            role=TenantRole.TENANT_ADMIN,
        ),
        "viewer": _principal(
            "viewer",
            role=TenantRole.TENANT_MEMBER,
        ),
        "crossTenant": _principal(
            "cross",
            role=TenantRole.TENANT_MEMBER,
            tenant_id="tenant-cross",
        ),
    }

    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="mutation was not confirmed",
    ):
        external_certification.run_external_checks(
            certification,
            setup=_setup(),
            verified=verified,
            principals=principals,
            challenge="c" * 64,
            transport=transport,
        )

    assert transport.name == "Production"
    assert transport.revision == 5
    assert transport.config_writes[-1] == ("admin", 4, "Production")


def test_external_oidc_workflow_is_disjoint_protected_and_self_hosted() -> None:
    workflow = _workflow()
    triggers = workflow["on"]
    certify = workflow["jobs"]["certify"]

    assert workflow["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }
    assert set(triggers) == {"workflow_call"}
    assert certify["environment"] == ("agentcore-external-oidc-production-like")
    assert certify["runs-on"] == [
        "self-hosted",
        "linux",
        "x64",
        "axonllm-agentcore-allowlisted",
    ]
    assert certify["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }
    assert "ubuntu" not in json.dumps(certify["runs-on"]).casefold()
    assert WORKFLOW.name != MANAGED_WORKFLOW.name
    assert workflow["jobs"]["verify-release"]["uses"] == "./.github/workflows/deploy-verification.yml"
    assert workflow["jobs"]["verify-release"]["with"]["target"] == "agentcore"
    assert "secrets" not in workflow["jobs"]["verify-release"]
    assert "secrets" not in workflow["on"]["workflow_call"]


def test_external_oidc_workflow_has_no_hand_authored_report_input() -> None:
    workflow = _workflow()
    body = WORKFLOW.read_text(encoding="utf-8")
    call_inputs = workflow["on"]["workflow_call"]["inputs"]
    steps = workflow["jobs"]["certify"]["steps"]
    names = [step["name"] for step in steps]

    for inputs in (call_inputs,):
        assert "report" not in inputs
        assert "status" not in inputs
        assert "issuer" not in inputs
        assert inputs["setup_config_s3_version_id"]["required"] == "true"
        assert inputs["setup_config_sha256"]["required"] == "true"
        assert inputs["certification_config_s3_version_id"]["required"] == "true"
        assert inputs["certification_config_sha256"]["required"] == "true"

    assert names.index("Fetch exact reviewed external-OIDC contracts") < names.index(
        "Run live external-OIDC launch certification"
    )
    assert names.index("Run live external-OIDC launch certification") < names.index("Revalidate live-produced report")
    assert names.index("Revalidate live-produced report") < names.index("KMS-sign and verify external-OIDC report")
    assert names.index("KMS-sign and verify external-OIDC report") < names.index(
        "Persist and reverify locked external-OIDC evidence"
    )
    assert "certify_external_oidc_agentcore.py run" in body
    assert "certify_external_oidc_agentcore.py verify-report" in body
    assert "certify_external_oidc_agentcore.py cleanup" in body
    assert "trap cleanup EXIT" in body
    assert "trap 'exit 130' INT" in body
    assert "trap 'exit 143' TERM" in body
    assert "trap cleanup EXIT INT TERM" not in body
    assert "--version-id" in body
    assert "sha256sum --check --status" in body
    assert "identity_mode" not in body


def test_external_oidc_workflow_owns_exact_namespaced_lifecycle() -> None:
    workflow = _workflow()
    certify = workflow["jobs"]["certify"]
    teardown = workflow["jobs"]["teardown"]
    steps = certify["steps"]
    names = [step["name"] for step in steps]
    deploy = next(step for step in steps if step["name"] == "Deploy and bind isolated external-OIDC candidate")
    source = next(step for step in steps if step["name"] == "Read provider source secret into an owner-only file")
    teardown_run = teardown["steps"][-1]["run"]
    body = WORKFLOW.read_text(encoding="utf-8")

    assert workflow["env"]["EXTERNAL_NAMESPACE"] == "external"
    assert workflow["env"]["EXTERNAL_RUNTIME_STACK"] == ("AxonLLMAgentCoreStack-external")
    assert names.index("Read provider source secret into an owner-only file") < names.index(
        "Deploy and bind isolated external-OIDC candidate"
    )
    assert names.index("Deploy and bind isolated external-OIDC candidate") < names.index(
        "Run live external-OIDC launch certification"
    )
    assert source["env"]["PROVIDER_SOURCE_SECRET_ARN"] == (
        "${{ vars.AXON_AGENTCORE_EXTERNAL_PROVIDER_SOURCE_SECRET_ARN }}"
    )
    assert "secretsmanager get-secret-value" in source["run"]
    assert '--deployment-namespace "${EXTERNAL_NAMESPACE}"' in deploy["run"]
    assert '--provider-env-file "${provider_env}"' in deploy["run"]
    assert "certification-bound.json" in deploy["run"]
    assert body.count('--runtime-stack-name "${EXTERNAL_RUNTIME_STACK}"') == 3
    assert teardown["needs"] == ["verify-release", "certify"]
    assert teardown["if"] == "${{ always() }}"
    assert teardown_run.count('--stack-name "${EXTERNAL_RUNTIME_STACK}"') == 4
    assert "delete-stack" in teardown_run
    assert "wait stack-delete-complete" in teardown_run
    assert 'AxonLLMAgentCoreStack"' not in teardown_run


def test_external_oidc_workflow_locks_signs_and_refetches_exact_versions() -> None:
    body = WORKFLOW.read_text(encoding="utf-8")
    workflow = _workflow()
    persist = next(
        step
        for step in workflow["jobs"]["certify"]["steps"]
        if step["name"] == "Persist and reverify locked external-OIDC evidence"
    )["run"]

    assert "kms_evidence.py sign" in body
    assert body.count("kms_evidence.py verify") >= 2
    assert "get-object-lock-configuration" in body
    assert "get-bucket-versioning" in body
    assert "get-bucket-encryption" in body
    assert "AXON_DEPLOYMENT_EVIDENCE_KMS_KEY_ARN" in body
    assert "KMSMasterKeyID == $key" in body
    assert "BucketKeyEnabled == true" in body
    assert "s3:DeleteObjectVersion" in body
    assert "--object-lock-mode COMPLIANCE" in persist
    assert "--if-none-match '*'" in persist
    assert "--server-side-encryption aws:kms" in persist
    assert '--ssekms-key-id "${STORAGE_KEY_ARN}"' in persist
    assert "--bucket-key-enabled" in persist
    assert "--version-id" in persist
    assert "--checksum-mode ENABLED" in persist
    assert "ObjectLockMode" in persist
    assert "ObjectLockRetainUntilDate" in persist
    assert ".SSEKMSKeyId == $key" in persist
    assert 'sha256sum "${remote}/signature.json"' in persist
    assert (
        workflow["on"]["workflow_call"]["outputs"]["signature_sha256"]["value"]
        == "${{ jobs.certify.outputs.signature_sha256 }}"
    )
    assert persist.index('put_locked "${signature}"') < persist.index('put_locked "${report}"')
    assert "AWS_ACCESS_KEY_ID" not in body
    assert "AWS_SECRET_ACCESS_KEY" not in body
    assert "AXON_EXTERNAL_OIDC_FIXTURE_BROKER_TOKEN" in body
    assert "external-oidc-cleanup-state.json" not in persist


def test_external_oidc_workflow_validates_dynamic_aws_values_first() -> None:
    steps = _workflow()["jobs"]["certify"]["steps"]
    validation = next(step["run"] for step in steps if step["name"] == "Validate immutable workflow inputs")
    storage = next(step["run"] for step in steps if step["name"] == "Verify immutable evidence storage")

    assert validation.index('[[ "${AWS_ACCOUNT_ID}" =~ ^[0-9]{12}$ ]]') < validation.index(
        '[[ "${AGENTCORE_IMAGE}" =~ ^${AWS_ACCOUNT_ID}'
    )
    assert validation.index('[[ "${AWS_REGION}" =~ ^[a-z]{2}') < validation.index(
        '[[ "${AGENTCORE_IMAGE}" =~ ^${AWS_ACCOUNT_ID}'
    )
    assert '"${SETUP_CONFIG_VERSION_ID}" =~' in validation
    assert '"${CERTIFICATION_CONFIG_VERSION_ID}" =~' in validation
    assert '"${EVIDENCE_BUCKET}" != *..*' in storage
    assert '"${EVIDENCE_PREFIX}" != *//*' in storage
    assert '"/${EVIDENCE_PREFIX}/" != *"/./"*' in storage
    assert '"/${EVIDENCE_PREFIX}/" != *"/../"*' in storage
    assert '"${SIGNING_KEY_ARN}" != "${STORAGE_KEY_ARN}"' in storage


def test_external_oidc_workflow_passes_repository_policy() -> None:
    assert validate_workflow(WORKFLOW) == 6


def test_fetch_issuer_material_requires_fresh_same_origin_https_jwks() -> None:
    material = external_certification.fetch_issuer_material(
        ISSUER,
        transport=_IssuerTransport(ISSUER),
        clock=lambda: NOW,
    )

    assert material.issuer == ISSUER
    assert material.jwks_uri == f"{ISSUER}/keys"
    assert material.jwks_freshness.current_age_seconds == 0
    assert material.jwks["keys"][0]["kid"] == "key-1"

    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="stale",
    ):
        external_certification.fetch_issuer_material(
            ISSUER,
            transport=_IssuerTransport(
                ISSUER,
                max_age=60,
                response_date=NOW - 61,
            ),
            clock=lambda: NOW,
        )

    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="origin",
    ):
        external_certification.fetch_issuer_material(
            ISSUER,
            transport=_IssuerTransport(
                ISSUER,
                jwks_uri="https://keys.attacker.example/keys",
            ),
            clock=lambda: NOW,
        )

    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="HTTPS",
    ):
        external_certification.fetch_issuer_material(
            "http://idp.example.com",
            transport=_IssuerTransport(ISSUER),
            clock=lambda: NOW,
        )


def test_token_bundle_proves_short_lived_claim_and_mixup_cases() -> None:
    tokens, expected, mixup = _token_bundle()

    verified = external_certification.validate_token_bundle(
        tokens,
        expected_material=expected,
        mixup_material=mixup,
        setup=_setup(),
        cross_tenant_id="tenant-cross",
        clock=lambda: NOW,
    )

    assert set(verified) == set(external_certification.TOKEN_CASES)
    assert verified["admin"].issuer == ISSUER
    assert verified["viewer"].audience == (AUDIENCE,)
    assert verified["crossTenant"].tenant_id == "tenant-cross"
    assert verified["wrongAudience"].audience == ("api://wrong",)
    assert verified["missingTenant"].tenant_id is None
    assert verified["missingProject"].project_id is None
    assert verified["issuerMixup"].issuer == MIXUP_ISSUER


def test_token_verification_fails_closed_on_issuer_and_audience_mixup() -> None:
    _, expected_pem, expected = _key_material(
        ISSUER,
        kid="expected-key",
    )
    wrong_issuer = _identity(
        expected_pem,
        kid="expected-key",
        issuer=MIXUP_ISSUER,
        case="issuer-confusion",
        subject="subject-viewer",
    )
    wrong_audience = _identity(
        expected_pem,
        kid="expected-key",
        issuer=ISSUER,
        case="audience-confusion",
        subject="subject-viewer",
        audience="api://wrong",
    )

    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="registered claims",
    ):
        external_certification.verify_fixture_identity(
            "issuer-confusion",
            wrong_issuer,
            material=expected,
            audience=AUDIENCE,
            tenant_claim=TENANT_CLAIM,
            project_claim=PROJECT_CLAIM,
            expected_tenant="tenant-a",
            expected_project="project-a",
            clock=lambda: NOW,
        )

    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="registered claims",
    ):
        external_certification.verify_fixture_identity(
            "audience-confusion",
            wrong_audience,
            material=expected,
            audience=AUDIENCE,
            tenant_claim=TENANT_CLAIM,
            project_claim=PROJECT_CLAIM,
            expected_tenant="tenant-a",
            expected_project="project-a",
            clock=lambda: NOW,
        )


def test_signature_tamper_canary_changes_signed_bytes() -> None:
    tokens, _, _ = _token_bundle()
    original = tokens["viewer"]
    tampered = external_certification._tamper_signature(original)
    original_parts = original.split(".")
    tampered_parts = tampered.split(".")

    def decode(segment: str) -> bytes:
        return base64.urlsafe_b64decode(segment + ("=" * (-len(segment) % 4)))

    assert tampered_parts[:2] == original_parts[:2]
    assert tampered_parts[2] != original_parts[2]
    assert decode(tampered_parts[2]) != decode(original_parts[2])


def test_broker_cleanup_requires_affirmative_identity_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    challenge = "c" * 64
    fixture_id = "fixture-12345678"
    monkeypatch.setenv(
        external_certification.BROKER_CREDENTIAL_ENV,
        "ephemeral-broker-credential",
    )

    def transport(
        method: str,
        url: str,
        *,
        headers,
        body,
        timeout_seconds,
        maximum_bytes,
    ):
        assert method == "DELETE"
        assert url.endswith(f"/{fixture_id}")
        assert headers["Authorization"].startswith("Bearer ")
        assert body is not None
        assert timeout_seconds == external_certification.HTTP_TIMEOUT_SECONDS
        assert maximum_bytes == external_certification.MAX_HTTP_BYTES
        value = {
            "schema": external_certification.BROKER_CLEANUP_SCHEMA,
            "challenge": challenge,
            "fixtureId": fixture_id,
            "complete": True,
            "identitiesRevoked": False,
        }
        encoded = json.dumps(value).encode("utf-8")
        return external_certification.JsonResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=encoded,
            value=value,
        )

    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="did not prove fixture cleanup",
    ):
        external_certification.delete_broker_fixture(
            broker_url="https://broker.example.com/v1/fixtures",
            fixture_id=fixture_id,
            challenge=challenge,
            transport=transport,
        )


def test_runtime_binding_rejects_deployed_issuer_mixup() -> None:
    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="differ from reviewed setup",
    ):
        external_certification.resolve_runtime_binding(
            _Session(_runtime_stack(issuer=MIXUP_ISSUER)),
            setup=_setup(),
            certification=_certification(),
            expected_image=IMAGE,
            runtime_stack_name=EXTERNAL_STACK,
        )

    binding = external_certification.resolve_runtime_binding(
        _Session(_runtime_stack()),
        setup=_setup(),
        certification=_certification(),
        expected_image=IMAGE,
        runtime_stack_name=EXTERNAL_STACK,
    )
    assert binding.runtime_version == "7"
    assert binding.stack_name == EXTERNAL_STACK
    assert binding.endpoint_name == CANDIDATE
    assert binding.image == IMAGE


@pytest.mark.parametrize(
    "stack_name",
    [
        "AxonLLMAgentCoreStack-External",
        "AxonLLMAgentCoreStack-external*",
        "OtherStack-external",
    ],
)
def test_runtime_stack_name_is_strictly_validated(stack_name: str) -> None:
    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="runtime stack name",
    ):
        external_certification._source_binding(
            repository="AxonLLM/axonllm",
            workflow_ref=WORKFLOW_REF,
            parent_workflow_ref=PARENT_WORKFLOW_REF,
            run_id="123",
            run_attempt="1",
            workflow_commit="2" * 40,
            parent_workflow_commit="2" * 40,
            release_commit="2" * 40,
            agentcore_image=IMAGE,
            runtime_stack_name=stack_name,
            region=REGION,
        )


def test_cleanup_state_never_contains_identity_or_broker_credential() -> None:
    fixture = external_certification.BrokerFixture(
        fixture_id="fixture-12345678",
        challenge="c" * 64,
        expires_at=NOW + 600,
        tokens={"admin": "header.payload.signature"},
        response_sha256="d" * 64,
    )

    state = external_certification._new_cleanup_state(
        region=REGION,
        table_name="axonllm-agentcore-state",
        broker_url="https://broker.example.com/v1/fixtures",
        fixture=fixture,
    )
    serialized = json.dumps(state)

    assert "header.payload.signature" not in serialized
    assert "identities" not in serialized
    assert "authorization" not in serialized.casefold()
    assert external_certification.BROKER_CREDENTIAL_ENV not in serialized
    assert external_certification._validate_cleanup_state(state) == state


def test_hand_authored_pass_report_is_rejected_before_live_binding(
    tmp_path: Path,
) -> None:
    source = external_certification._source_binding(
        repository="AxonLLM/axonllm",
        workflow_ref=WORKFLOW_REF,
        parent_workflow_ref=PARENT_WORKFLOW_REF,
        run_id="123",
        run_attempt="1",
        workflow_commit="2" * 40,
        parent_workflow_commit="2" * 40,
        release_commit="2" * 40,
        agentcore_image=IMAGE,
        runtime_stack_name=EXTERNAL_STACK,
        region=REGION,
    )
    report = {
        "schema": external_certification.REPORT_SCHEMA,
        "generatedAt": datetime.fromtimestamp(
            NOW,
            tz=timezone.utc,
        ).isoformat(),
        "overallStatus": "PASS",
        "producer": {
            "path": external_certification.PRODUCER_PATH,
            "sha256": "0" * 64,
            "mode": "hand-authored",
        },
        "source": source.to_report(),
        "target": {},
        "oidc": {},
        "fixtures": {},
        "fullLaunchCertification": {},
        "checks": [],
        "summary": {},
    }
    path = tmp_path / "forged.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    class _NoAws:
        def client(self, service_name: str, *, region_name: str):
            raise AssertionError("forged data must fail before AWS calls")

    with pytest.raises(
        external_certification.ExternalOidcCertificationError,
        match="live producer",
    ):
        external_certification.verify_report(
            path,
            setup=_setup(),
            certification=_certification(),
            source=source,
            session=_NoAws(),
            clock=lambda: NOW,
        )
