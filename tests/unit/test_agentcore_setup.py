"""First-adopter AgentCore setup, deployment, and invitation contracts."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from src.gateway.deployment import agentcore_deploy as deployment
from src.gateway.deployment import bootstrap_policy
from src.gateway.deployment.agentcore_deploy import (
    AgentCoreDeploymentError,
    IdentityValues,
    agentcore_deploy_command,
    control_plane_deploy_command,
    ensure_managed_cognito_admin,
    identity_deploy_command,
)
from src.gateway.agentcore_setup import (
    DEFAULT_AGENTCORE_PROVIDERS,
    SUPPORTED_AGENTCORE_PROVIDERS,
    AgentCoreSetupConfig,
    AgentCoreSetupError,
    cmd_setup_agentcore,
    cmd_setup_local_demo,
    load_agentcore_setup,
    local_demo_environment,
    redact_sensitive,
    write_agentcore_setup,
)


_REPO = Path(__file__).resolve().parents[2]
_DIGEST = "e368b7b4522f4838f3ebb4dcc04967682c73cb73e7e40ce16421a6a1ffda6147"
_IMAGE = f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/agentcore@sha256:{_DIGEST}"
_CONTROL_IMAGE = f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/control-plane@sha256:{_DIGEST}"
_BEDROCK_ARN = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0"
_BEDROCK_PROFILE_ARN = (
    "arn:aws:bedrock:us-east-1:123456789012:"
    "inference-profile/us.anthropic.claude-sonnet-4-6"
)
_BEDROCK_PROFILE_DESTINATION_ARN = (
    "arn:aws:bedrock:us-west-2::"
    "foundation-model/anthropic.claude-sonnet-4-6"
)
_ATHENA_ROLE_ARN = "arn:aws:iam::123456789012:role/axon-athena-project-a"
_CERTIFICATE_ARN = "arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555"
_SCIM_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:axonllm/scim-AbCd12"
_CANDIDATE_ENDPOINT_NAME = "candidate_" + "a" * 32
_ALARM_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:axonllm-agentcore-alarms"
_REHEARSAL_CONTROL_TABLE_ARN = "arn:aws:dynamodb:us-east-1:123456789012:table/axonllm-rehearsal-control-ledger"


def _base() -> dict:
    return {
        "schema_version": 2,
        "target": "agentcore",
        "identity_mode": "managed-cognito",
        "aws_region": "us-east-1",
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
        },
        "runtime": {
            "verified_image_uri": _IMAGE,
            "bedrock_invoke_resource_arns": [_BEDROCK_ARN],
            "approved_https_prefix_list_id": "pl-123abc",
        },
        "managed_cognito": {
            "hosted_ui_domain_prefix": "axonllm-123456789012",
            "oauth_callback_urls": ["https://app.example.com/oauth/callback"],
        },
        "control_plane": {
            "domain_name": "axon.example.com",
            "verified_image_uri": _CONTROL_IMAGE,
            "certificate_arn": _CERTIFICATE_ARN,
            "public_hosted_zone_id": "Z123ABC",
            "approved_ingress_prefix_list_id": "pl-123abc",
            "approved_https_prefix_list_id": "pl-456def",
        },
    }


def _external() -> dict:
    value = _base()
    value["identity_mode"] = "external-oidc"
    value.pop("managed_cognito")
    value.pop("control_plane")
    value["admin"]["subject"] = "00u-admin-subject"
    value["external_oidc"] = {
        "issuer": "https://idp.example.com/oauth2/default",
        "discovery_url": ("https://idp.example.com/oauth2/default/.well-known/openid-configuration"),
        "client_id": "axonllm-client",
        "audience": "api://axonllm",
        "tenant_claim": "https://axonllm.example/tenant",
        "project_claim": "https://axonllm.example/project",
    }
    return value


def _cloudfront() -> dict:
    value = _base()
    value["control_plane"] = {
        "endpoint_mode": "cloudfront",
        "verified_image_uri": _CONTROL_IMAGE,
        "approved_https_prefix_list_id": "pl-456def",
        "allowed_viewer_cidrs": [
            "1.1.1.0/24",
            "8.8.8.8/32",
        ],
    }
    return value


def _athena_roles_for_bindings_length(target_length: int) -> list[str]:
    roles = [f"arn:aws:iam::{123456789012 + index}:role/r{index}" for index in range(4)]

    def serialize() -> str:
        return json.dumps(
            [
                {
                    "tenant_id": "tenant-a",
                    "project_id": "project-a",
                    "role_arn": role,
                }
                for role in roles
            ],
            separators=(",", ":"),
            sort_keys=True,
        )

    remaining = target_length - len(serialize())
    for index, role in enumerate(roles):
        role_path_length = len(role.split("role/", maxsplit=1)[1])
        added = min(remaining, 512 - role_path_length)
        roles[index] += "x" * added
        remaining -= added
    assert remaining == 0
    assert len(serialize()) == target_length
    return roles


def test_managed_setup_round_trips_without_a_secret_or_subject(tmp_path):
    config = AgentCoreSetupConfig.from_mapping(_base())
    output = write_agentcore_setup(config, tmp_path / "agentcore.json")

    loaded = load_agentcore_setup(output)
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert loaded == config
    assert payload["identity_mode"] == "managed-cognito"
    assert payload["control_plane"]["domain_name"] == "axon.example.com"
    assert payload["control_plane"]["saml_login_path"] == "/admin/dashboard"
    assert "subject" not in payload["admin"]
    assert "secret" not in output.read_text(encoding="utf-8").casefold()
    assert output.stat().st_mode & 0o777 == 0o600


def test_agentcore_defaults_to_the_required_google_ai_launch_profile():
    config = AgentCoreSetupConfig.from_mapping(_base())

    assert set(config.runtime.enabled_providers) == {
        "anthropic",
        "bedrock",
        "bedrock-mantle",
        "fireworks",
        "google_ai",
        "groq",
        "openai",
        "together",
        "xai",
    }
    assert config.runtime.enabled_providers == DEFAULT_AGENTCORE_PROVIDERS
    assert config.runtime.enabled_providers == tuple(
        sorted(config.runtime.enabled_providers)
    )
    assert SUPPORTED_AGENTCORE_PROVIDERS - set(
        DEFAULT_AGENTCORE_PROVIDERS
    ) == {
        "ai21",
        "azure_openai",
        "cohere",
        "vertex_ai",
    }


def test_cloudfront_setup_round_trips_without_dns_or_certificate(tmp_path):
    config = AgentCoreSetupConfig.from_mapping(_cloudfront())
    output = write_agentcore_setup(config, tmp_path / "agentcore.json")
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert load_agentcore_setup(output) == config
    assert config.control_plane is not None
    assert config.control_plane.endpoint_mode == "cloudfront"
    assert config.control_plane.allowed_viewer_cidrs == (
        "1.1.1.0/24",
        "8.8.8.8/32",
    )
    assert payload["control_plane"]["endpoint_mode"] == "cloudfront"
    assert "domain_name" not in payload["control_plane"]
    assert "certificate_arn" not in payload["control_plane"]
    assert "public_hosted_zone_id" not in payload["control_plane"]
    assert "approved_ingress_prefix_list_id" not in (
        payload["control_plane"]
    )


@pytest.mark.parametrize(
    ("cidrs", "message"),
    [
        ([], "must be a non-empty array"),
        (["10.0.0.0/24"], "must be a public IPv4 network"),
        (["224.0.0.0/24"], "must be a public IPv4 network"),
        (["1.1.0.0/16"], "no broader than /24"),
        (["1.1.1.1/24"], "must be a canonical IP network"),
        (["2606:4700:4700::1111/128"], "must be an IPv4 network"),
        (
            ["1.1.1.0/24", "1.1.1.1/32"],
            "must not contain overlapping CIDRs",
        ),
    ],
)
def test_cloudfront_setup_rejects_unsafe_viewer_cidrs(
    cidrs,
    message,
):
    value = _cloudfront()
    value["control_plane"]["allowed_viewer_cidrs"] = cidrs

    with pytest.raises(AgentCoreSetupError, match=message):
        AgentCoreSetupConfig.from_mapping(value)


def test_cloudfront_setup_rejects_custom_domain_inputs_and_other_regions():
    value = _cloudfront()
    value["control_plane"]["domain_name"] = "axon.example.com"
    with pytest.raises(
        AgentCoreSetupError,
        match="forbids custom-domain fields: domain_name",
    ):
        AgentCoreSetupConfig.from_mapping(value)

    value = _cloudfront()
    value["aws_region"] = "us-west-2"
    value["runtime"]["verified_image_uri"] = _IMAGE.replace(
        "us-east-1",
        "us-west-2",
    )
    value["control_plane"]["verified_image_uri"] = (
        _CONTROL_IMAGE.replace("us-east-1", "us-west-2")
    )
    value["runtime"]["bedrock_invoke_resource_arns"] = [
        _BEDROCK_ARN.replace("us-east-1", "us-west-2")
    ]
    with pytest.raises(
        AgentCoreSetupError,
        match="currently requires aws_region 'us-east-1'",
    ):
        AgentCoreSetupConfig.from_mapping(value)


def test_managed_setup_validates_optional_ses_sender_domain():
    value = _base()
    value["managed_cognito"].update(
        {
            "ses_from_email": "no-reply@example.com",
            "ses_verified_domain": "example.com",
        }
    )

    config = AgentCoreSetupConfig.from_mapping(value)

    assert config.managed_cognito is not None
    assert config.managed_cognito.ses_from_email == "no-reply@example.com"
    assert config.to_dict()["managed_cognito"]["ses_verified_domain"] == ("example.com")

    mismatched = deepcopy(value)
    mismatched["managed_cognito"]["ses_verified_domain"] = "other.example"
    with pytest.raises(
        AgentCoreSetupError,
        match="must be the lowercase domain",
    ):
        AgentCoreSetupConfig.from_mapping(mismatched)

    incomplete = deepcopy(value)
    incomplete["managed_cognito"].pop("ses_verified_domain")
    with pytest.raises(
        AgentCoreSetupError,
        match="must be supplied together",
    ):
        AgentCoreSetupConfig.from_mapping(incomplete)


def test_managed_setup_accepts_scim_secret_and_saml_login_path(tmp_path):
    value = _base()
    value["control_plane"].update(
        scim_tenants_secret_arn=_SCIM_SECRET_ARN,
        saml_login_path="/chat",
    )

    config = AgentCoreSetupConfig.from_mapping(value)
    output = write_agentcore_setup(config, tmp_path / "agentcore.json")

    assert load_agentcore_setup(output) == config
    assert config.control_plane is not None
    assert config.control_plane.scim_tenants_secret_arn == _SCIM_SECRET_ARN
    assert config.control_plane.saml_login_path == "/chat"
    assert config.to_dict()["control_plane"]["scim_tenants_secret_arn"] == _SCIM_SECRET_ARN

    wrong_region = deepcopy(value)
    wrong_region["control_plane"]["scim_tenants_secret_arn"] = _SCIM_SECRET_ARN.replace("us-east-1", "us-west-2")
    with pytest.raises(
        AgentCoreSetupError,
        match="complete Secrets Manager ARN in us-east-1",
    ):
        AgentCoreSetupConfig.from_mapping(wrong_region)

    legacy_direct_sp = deepcopy(value)
    legacy_direct_sp["control_plane"]["saml_config_secret_arn"] = _SCIM_SECRET_ARN
    with pytest.raises(
        AgentCoreSetupError,
        match="unsupported fields: saml_config_secret_arn",
    ):
        AgentCoreSetupConfig.from_mapping(legacy_direct_sp)


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/login",
        "//evil.example/login",
        "/saml/acs",
        "/scim/v2/Users",
        "/oauth2/authorize",
        "/admin/../ready",
        "/chat?next=/admin",
        "/admin dashboard",
        "/caf\u00e9",
    ],
)
def test_managed_setup_rejects_unsafe_saml_login_path(path):
    value = _base()
    value["control_plane"]["saml_login_path"] = path

    with pytest.raises(
        AgentCoreSetupError,
        match="must be a protected application-local path",
    ):
        AgentCoreSetupConfig.from_mapping(value)


def test_external_oidc_requires_complete_explicit_claim_mapping():
    config = AgentCoreSetupConfig.from_mapping(_external())

    assert config.external_oidc is not None
    assert config.external_oidc.tenant_claim.endswith("/tenant")
    assert config.admin.subject == "00u-admin-subject"

    incomplete = _external()
    incomplete["external_oidc"].pop("project_claim")
    with pytest.raises(
        AgentCoreSetupError,
        match="missing required fields: project_claim",
    ):
        AgentCoreSetupConfig.from_mapping(incomplete)


def test_external_discovery_is_bound_to_the_exact_issuer():
    value = _external()
    value["external_oidc"]["discovery_url"] = "https://other.example.com/.well-known/openid-configuration"

    with pytest.raises(
        AgentCoreSetupError,
        match="must be the configured issuer",
    ):
        AgentCoreSetupConfig.from_mapping(value)


@pytest.mark.parametrize("field", ["client_id", "audience"])
def test_external_oidc_rejects_comma_delimited_identifiers(field):
    value = _external()
    value["external_oidc"][field] = "first,second"

    with pytest.raises(
        AgentCoreSetupError,
        match="must not contain commas",
    ):
        AgentCoreSetupConfig.from_mapping(value)


def test_agentcore_has_no_unauthenticated_setup_mode():
    value = _base()
    value["identity_mode"] = "none"

    with pytest.raises(
        AgentCoreSetupError,
        match="no unauthenticated production mode",
    ):
        AgentCoreSetupConfig.from_mapping(value)


def test_control_plane_is_managed_cognito_only_and_region_bound():
    missing = _base()
    missing.pop("control_plane")
    with pytest.raises(
        AgentCoreSetupError,
        match="requires managed_cognito and control_plane",
    ):
        AgentCoreSetupConfig.from_mapping(missing)

    external = _external()
    external["control_plane"] = _base()["control_plane"]
    with pytest.raises(
        AgentCoreSetupError,
        match="forbids managed_cognito and control_plane",
    ):
        AgentCoreSetupConfig.from_mapping(external)

    wrong_region = _base()
    wrong_region["control_plane"]["certificate_arn"] = _CERTIFICATE_ARN.replace("us-east-1", "us-west-2")
    with pytest.raises(
        AgentCoreSetupError,
        match="regional ACM certificate ARN in us-east-1",
    ):
        AgentCoreSetupConfig.from_mapping(wrong_region)


def test_setup_rejects_boolean_schema_versions_and_local_identity_urls():
    boolean_schema = _base()
    boolean_schema["schema_version"] = True
    with pytest.raises(AgentCoreSetupError, match="schema_version must be 2"):
        AgentCoreSetupConfig.from_mapping(boolean_schema)

    previous_schema = _base()
    previous_schema["schema_version"] = 1
    with pytest.raises(AgentCoreSetupError, match="schema_version must be 2"):
        AgentCoreSetupConfig.from_mapping(previous_schema)

    local_issuer = _external()
    local_issuer["external_oidc"]["issuer"] = "https://127.0.0.1"
    local_issuer["external_oidc"]["discovery_url"] = "https://127.0.0.1/.well-known/openid-configuration"
    with pytest.raises(AgentCoreSetupError, match="must be an HTTPS URL"):
        AgentCoreSetupConfig.from_mapping(local_issuer)


def test_setup_rejects_mutable_images_wildcards_and_client_secrets():
    tagged = _base()
    tagged["runtime"]["verified_image_uri"] = "123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/agentcore:latest"
    with pytest.raises(AgentCoreSetupError, match="immutable private ECR"):
        AgentCoreSetupConfig.from_mapping(tagged)

    wildcard = _base()
    wildcard["runtime"]["bedrock_invoke_resource_arns"] = ["arn:aws:bedrock:us-east-1::foundation-model/*"]
    with pytest.raises(AgentCoreSetupError, match="concrete model"):
        AgentCoreSetupConfig.from_mapping(wildcard)

    secret = _external()
    secret["external_oidc"]["client_secret"] = "must-not-be-stored"
    with pytest.raises(
        AgentCoreSetupError,
        match="unsupported fields: client_secret",
    ):
        AgentCoreSetupConfig.from_mapping(secret)


def test_setup_accepts_cross_region_foundation_model_destinations():
    value = _base()
    value["runtime"]["bedrock_invoke_resource_arns"] = [
        _BEDROCK_PROFILE_ARN,
        _BEDROCK_ARN,
        _BEDROCK_PROFILE_DESTINATION_ARN,
    ]

    config = AgentCoreSetupConfig.from_mapping(value)

    assert config.runtime.bedrock_invoke_resource_arns == (
        _BEDROCK_PROFILE_ARN,
        _BEDROCK_ARN,
        _BEDROCK_PROFILE_DESTINATION_ARN,
    )


def test_setup_rejects_cross_region_inference_profiles():
    value = _base()
    value["runtime"]["bedrock_invoke_resource_arns"] = [
        (
            "arn:aws:bedrock:us-west-2:123456789012:"
            "inference-profile/us.anthropic.claude-sonnet-4-6"
        )
    ]

    with pytest.raises(
        AgentCoreSetupError,
        match="inference profiles and account-scoped resources must be in us-east-1",
    ):
        AgentCoreSetupConfig.from_mapping(value)


def test_optional_athena_query_setup_is_exact_and_bounded():
    value = _base()
    value["runtime"]["athena_query"] = {
        "role_arns": [_ATHENA_ROLE_ARN],
        "timeout_seconds": 12.5,
        "max_rows": 250,
        "max_result_bytes": 524288,
        "max_bytes_scanned": 104857600,
        "poll_interval_seconds": 0.5,
        "project_rpm": 30,
        "principal_rpm": 10,
        "project_concurrency": 5,
        "principal_concurrency": 2,
        "project_scan_bytes_per_minute": 5 * 1024 * 1024 * 1024,
        "principal_scan_bytes_per_minute": 2 * 1024 * 1024 * 1024,
        "max_datasources_per_tenant": 500,
    }

    config = AgentCoreSetupConfig.from_mapping(value)

    assert config.runtime.athena_query is not None
    assert config.runtime.athena_query.role_arns == (_ATHENA_ROLE_ARN,)
    assert config.to_dict()["runtime"]["athena_query"] == (value["runtime"]["athena_query"])


@pytest.mark.parametrize(
    "athena_query",
    [
        {"role_arns": []},
        {"role_arns": ["arn:aws:iam::123456789012:role/*"]},
        {"role_arns": [_ATHENA_ROLE_ARN, _ATHENA_ROLE_ARN]},
        {"role_arns": [_ATHENA_ROLE_ARN], "max_rows": 0},
        {
            "role_arns": [_ATHENA_ROLE_ARN],
            "max_result_bytes": 1023,
        },
        {
            "role_arns": [_ATHENA_ROLE_ARN],
            "timeout_seconds": float("inf"),
        },
    ],
)
def test_athena_query_setup_rejects_unsafe_values(
    athena_query: dict,
):
    value = _base()
    value["runtime"]["athena_query"] = athena_query

    with pytest.raises(AgentCoreSetupError):
        AgentCoreSetupConfig.from_mapping(value)


def test_redaction_is_recursive_and_does_not_mutate_input():
    source = {
        "client_secret": "secret-value",
        "nested": [
            {
                "refresh_token": "refresh-value",
                "client_id": "public-id",
            }
        ],
        "api_key": "key-value",
    }

    redacted = redact_sensitive(source)

    assert redacted["client_secret"] == "<redacted>"
    assert redacted["nested"][0]["refresh_token"] == "<redacted>"
    assert redacted["nested"][0]["client_id"] == "public-id"
    assert redacted["api_key"] == "<redacted>"
    assert source["client_secret"] == "secret-value"


def test_local_demo_environment_is_explicitly_non_production():
    environment = local_demo_environment(
        {
            "AXON_DEPLOYMENT_PROFILE": "production",
            "AXON_AUTH_MODE": "ENFORCE",
        }
    )

    assert environment["AXON_DEPLOYMENT_PROFILE"] == "development"
    assert environment["AXON_AUTH_MODE"] == "LOG_ONLY"
    assert environment["AXON_LOAD_DEMO_DATA"] == "true"
    assert environment["AXON_REQUIRE_CANONICAL_IDENTITY"] == "false"
    assert environment["LLM_ROUTER_DYNAMODB_ENABLED"] == "false"
    assert environment["AXON_ATHENA_QUERY_ENABLED"] == "false"
    assert environment["AXON_CONTROL_PLANE_ONLY"] == "false"


def test_setup_agentcore_deploys_through_installed_python_module(
    monkeypatch,
    tmp_path,
):
    config_path = write_agentcore_setup(
        AgentCoreSetupConfig.from_mapping(_base()),
        tmp_path / "agentcore.json",
    )
    calls: list[tuple[list[str], dict]] = []

    def run(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr(subprocess, "run", run)

    cmd_setup_agentcore(
        SimpleNamespace(
            config=str(config_path),
            output=None,
            show_config=False,
            deploy=True,
            yes=True,
            bootstrap_cdk=True,
            provider_env_file="providers.env",
            rollback_provider_secret_version=None,
        )
    )

    command, kwargs = calls[0]
    assert command[:3] == [
        sys.executable,
        "-m",
        "src.gateway.deployment.agentcore_deploy",
    ]
    assert command[3:] == [
        "--config",
        str(config_path),
        "--yes",
        "--bootstrap-cdk",
        "--provider-env-file",
        "providers.env",
    ]
    assert kwargs == {"check": True}


def test_setup_local_demo_starts_installed_python_module_without_chdir(
    monkeypatch,
    tmp_path,
):
    calls: list[tuple[str, list[str], dict[str, str]]] = []

    class ExecCalled(Exception):
        pass

    def execvpe(executable, argv, environment):
        calls.append((executable, argv, environment))
        raise ExecCalled

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "execvpe", execvpe)

    with pytest.raises(ExecCalled):
        cmd_setup_local_demo(
            SimpleNamespace(
                start=True,
                acknowledge_non_production=True,
            )
        )

    assert Path.cwd() == tmp_path
    executable, argv, environment = calls[0]
    assert executable == sys.executable
    assert argv == [
        sys.executable,
        "-m",
        "src.gateway.local_server",
    ]
    assert environment["AXON_DEPLOYMENT_PROFILE"] == "development"
    assert environment["AXON_AUTH_MODE"] == "LOG_ONLY"


def test_cli_parses_and_writes_managed_setup(monkeypatch, tmp_path, capsys):
    from src.gateway import cli

    output = tmp_path / "managed.json"
    monkeypatch.setattr(
        os.sys,
        "argv",
        [
            "axon",
            "setup",
            "agentcore",
            "--identity-mode",
            "managed-cognito",
            "--tenant",
            "tenant-a",
            "--project",
            "project-a",
            "--admin-user-name",
            "admin@example.com",
            "--admin-email",
            "admin@example.com",
            "--verified-image-uri",
            _IMAGE,
            "--bedrock-invoke-resource-arns",
            _BEDROCK_ARN,
            "--approved-https-prefix-list-id",
            "pl-123abc",
            "--hosted-ui-domain-prefix",
            "axonllm-123456789012",
            "--oauth-callback-url",
            "https://app.example.com/oauth/callback",
            "--control-plane-domain-name",
            "axon.example.com",
            "--control-plane-verified-image-uri",
            _CONTROL_IMAGE,
            "--control-plane-certificate-arn",
            _CERTIFICATE_ARN,
            "--control-plane-public-hosted-zone-id",
            "Z123ABC",
            "--control-plane-approved-ingress-prefix-list-id",
            "pl-123abc",
            "--control-plane-approved-https-prefix-list-id",
            "pl-456def",
            "--control-plane-scim-tenants-secret-arn",
            _SCIM_SECRET_ARN,
            "--control-plane-saml-login-path",
            "/chat",
            "--output",
            str(output),
        ],
    )

    cli.main()

    loaded = load_agentcore_setup(output)
    assert loaded.identity_mode == "managed-cognito"
    assert loaded.control_plane is not None
    assert loaded.control_plane.scim_tenants_secret_arn == _SCIM_SECRET_ARN
    assert loaded.control_plane.saml_login_path == "/chat"
    assert "Wrote authenticated AgentCore setup" in capsys.readouterr().out


def test_deploy_commands_pass_only_validated_standard_oidc_inputs(tmp_path):
    managed = AgentCoreSetupConfig.from_mapping(_base())
    identity_command = identity_deploy_command(
        managed,
        outputs_file=tmp_path / "identity.json",
        assume_yes=True,
    )
    assert "deployment_target=identity" in identity_command
    assert any(value.startswith("AxonLLMIdentityStack:OAuthCallbackUrls=https://") for value in identity_command)
    assert "AxonLLMIdentityStack:ControlPlaneDomainName=axon.example.com" in identity_command
    assert "AxonLLMIdentityStack:SesFromEmail=admin@example.com" in identity_command
    assert "AxonLLMIdentityStack:SesVerifiedDomain=example.com" in identity_command
    assert "never" in identity_command

    external = AgentCoreSetupConfig.from_mapping(_external())
    oidc = external.external_oidc
    assert oidc is not None
    runtime_command = agentcore_deploy_command(
        external,
        IdentityValues(
            issuer=oidc.issuer,
            discovery_url=oidc.discovery_url,
            client_id=oidc.client_id,
            audience=oidc.audience,
            tenant_claim=oidc.tenant_claim,
            project_claim=oidc.project_claim,
        ),
        outputs_file=tmp_path / "runtime.json",
        assume_yes=False,
        candidate_endpoint_name=_CANDIDATE_ENDPOINT_NAME,
    )
    joined = "\n".join(runtime_command)
    assert "AxonLLMAgentCoreStack:OidcIssuer=" in joined
    assert "AxonLLMAgentCoreStack:OidcTenantClaim=" in joined
    assert "AxonLLMAgentCoreStack:OidcProjectClaim=" in joined
    assert "AxonLLMAgentCoreStack:BedrockInvokeResourceArns=" in joined
    assert ("AxonLLMAgentCoreStack:AlarmNotificationEmail=admin@example.com") in runtime_command
    assert "broadening" in runtime_command
    assert "client_secret" not in joined.casefold()


def test_qualification_commands_share_one_bounded_namespace(tmp_path):
    managed = AgentCoreSetupConfig.from_mapping(_base())
    identity_command = identity_deploy_command(
        managed,
        outputs_file=tmp_path / "identity.json",
        assume_yes=True,
        deployment_namespace="managed",
    )
    control_command = control_plane_deploy_command(
        managed,
        primary_state_table_name="axonllm-agentcore-state-managed",
        outputs_file=tmp_path / "control.json",
        assume_yes=True,
        deployment_namespace="managed",
        rehearsal_control_table_arn=_REHEARSAL_CONTROL_TABLE_ARN,
    )
    external = AgentCoreSetupConfig.from_mapping(_external())
    oidc = external.external_oidc
    assert oidc is not None
    runtime_command = agentcore_deploy_command(
        external,
        IdentityValues(
            issuer=oidc.issuer,
            discovery_url=oidc.discovery_url,
            client_id=oidc.client_id,
            audience=oidc.audience,
            tenant_claim=oidc.tenant_claim,
            project_claim=oidc.project_claim,
        ),
        outputs_file=tmp_path / "runtime.json",
        assume_yes=True,
        candidate_endpoint_name=_CANDIDATE_ENDPOINT_NAME,
        deployment_namespace="external-oidc",
        rehearsal_control_table_arn=_REHEARSAL_CONTROL_TABLE_ARN,
    )

    assert identity_command[2] == "AxonLLMIdentityStack-managed"
    assert "deployment_namespace=managed" in identity_command
    assert ("AxonLLMIdentityStack-managed:HostedUiDomainPrefix=axonllm-123456789012-managed") in identity_command
    assert control_command[2] == "AxonLLMControlPlaneStack-managed"
    assert ("AxonLLMControlPlaneStack-managed:AgentCoreStackName=AxonLLMAgentCoreStack-managed") in control_command
    assert ("AxonLLMControlPlaneStack-managed:IdentityStackName=AxonLLMIdentityStack-managed") in control_command
    assert runtime_command[2] == ("AxonLLMAgentCoreStack-external-oidc")
    assert "deployment_namespace=external-oidc" in runtime_command
    assert any(argument.startswith("AxonLLMAgentCoreStack-external-oidc:OidcIssuer=") for argument in runtime_command)


def test_control_plane_deploy_command_is_bound_to_managed_identity(
    tmp_path,
):
    config = AgentCoreSetupConfig.from_mapping(_base())
    command = control_plane_deploy_command(
        config,
        primary_state_table_name="axonllm-agentcore-state",
        outputs_file=tmp_path / "control.json",
        assume_yes=True,
    )
    joined = "\n".join(command)

    assert "deployment_target=control-plane" in command
    assert ("AxonLLMControlPlaneStack:AgentCoreStackName=AxonLLMAgentCoreStack") in command
    assert ("AxonLLMControlPlaneStack:IdentityStackName=AxonLLMIdentityStack") in command
    assert ("AxonLLMControlPlaneStack:PrimaryStateTableName=axonllm-agentcore-state") in command
    assert (f"AxonLLMControlPlaneStack:ControlPlaneVerifiedImageUri={_CONTROL_IMAGE}") in command
    assert (f"AxonLLMControlPlaneStack:CertificateArn={_CERTIFICATE_ARN}") in command
    assert (
        "AxonLLMControlPlaneStack:ControlPlaneDomainName="
        "axon.example.com"
    ) in command
    assert "client_secret" not in joined.casefold()

    with pytest.raises(
        AgentCoreDeploymentError,
        match="requires managed-cognito",
    ):
        control_plane_deploy_command(
            AgentCoreSetupConfig.from_mapping(_external()),
            primary_state_table_name="axonllm-agentcore-state",
            outputs_file=tmp_path / "external-control.json",
            assume_yes=True,
        )


def test_cloudfront_deploy_commands_select_generated_endpoint(tmp_path):
    config = AgentCoreSetupConfig.from_mapping(_cloudfront())
    identity_command = identity_deploy_command(
        config,
        outputs_file=tmp_path / "identity.json",
        assume_yes=True,
    )
    control_command = control_plane_deploy_command(
        config,
        primary_state_table_name="axonllm-agentcore-state",
        outputs_file=tmp_path / "control.json",
        assume_yes=True,
    )
    identity_arguments = "\n".join(identity_command)
    control_arguments = "\n".join(control_command)

    assert (
        "AxonLLMIdentityStack:EndpointMode=cloudfront"
    ) in identity_command
    assert "ControlPlaneDomainName=" not in identity_arguments
    assert (
        "AxonLLMControlPlaneStack:EndpointMode=cloudfront"
    ) in control_command
    assert (
        "AxonLLMControlPlaneStack:AllowedViewerCidrs="
        "1.1.1.0/24,8.8.8.8/32"
    ) in control_command
    assert "CertificateArn=" not in control_arguments
    assert "PublicHostedZoneId=" not in control_arguments
    assert "ApprovedIngressPrefixListId=" not in control_arguments
    assert "ControlPlaneDomainName=" not in control_arguments


def test_control_plane_deploy_command_passes_recovery_and_identity_inputs(
    tmp_path,
):
    value = _base()
    value["control_plane"].update(
        scim_tenants_secret_arn=_SCIM_SECRET_ARN,
        saml_login_path="/chat",
    )
    config = AgentCoreSetupConfig.from_mapping(value)

    command = control_plane_deploy_command(
        config,
        primary_state_table_name="axonllm-agentcore-state",
        outputs_file=tmp_path / "control.json",
        assume_yes=True,
        runtime_state_table_name=("axonllm-agentcore-state-restore-validation-reviewed"),
        recovery_approval_id="CHG-2026-015",
    )

    assert ("scim_tenants_secret_arn=" + _SCIM_SECRET_ARN) in command
    assert ("AxonLLMControlPlaneStack:SamlLoginPath=/chat") in command
    assert not any("saml_config_secret_arn" in argument for argument in command)
    assert (
        "AxonLLMControlPlaneStack:RuntimeStateTableName=axonllm-agentcore-state-restore-validation-reviewed"
    ) in command
    assert ("AxonLLMControlPlaneStack:RecoveryCutoverMode=normal") in command
    assert ("AxonLLMControlPlaneStack:RecoveryApprovalId=CHG-2026-015") in command


def test_agentcore_deploy_command_binds_exact_athena_roles(tmp_path):
    value = _external()
    value["runtime"]["athena_query"] = {
        "role_arns": [_ATHENA_ROLE_ARN],
        "max_rows": 250,
    }
    config = AgentCoreSetupConfig.from_mapping(value)
    oidc = config.external_oidc
    assert oidc is not None

    command = agentcore_deploy_command(
        config,
        IdentityValues(
            issuer=oidc.issuer,
            discovery_url=oidc.discovery_url,
            client_id=oidc.client_id,
            audience=oidc.audience,
            tenant_claim=oidc.tenant_claim,
            project_claim=oidc.project_claim,
        ),
        outputs_file=tmp_path / "runtime.json",
        assume_yes=True,
        candidate_endpoint_name=_CANDIDATE_ENDPOINT_NAME,
    )
    joined = "\n".join(command)

    assert "athena_query_bindings=" in joined
    assert '"tenant_id":"tenant-a"' in joined
    assert '"project_id":"project-a"' in joined
    assert f'"role_arn":"{_ATHENA_ROLE_ARN}"' in joined
    assert "athena_query_max_rows=250" in joined

    control_command = control_plane_deploy_command(
        AgentCoreSetupConfig.from_mapping(
            {
                **_base(),
                "runtime": value["runtime"],
            }
        ),
        primary_state_table_name="axonllm-agentcore-state",
        outputs_file=tmp_path / "control.json",
        assume_yes=True,
    )
    control_joined = "\n".join(control_command)
    assert "athena_query_bindings=" in control_joined
    assert "athena_query_max_rows=250" in control_joined


def test_deployer_enforces_agentcore_binding_character_boundary():
    value = _external()
    value["runtime"]["athena_query"] = {
        "role_arns": _athena_roles_for_bindings_length(2_048),
    }
    contexts = deployment._athena_contexts(AgentCoreSetupConfig.from_mapping(value))
    assert len(contexts["athena_query_bindings"]) == 2_048

    value["runtime"]["athena_query"]["role_arns"] = _athena_roles_for_bindings_length(2_049)
    with pytest.raises(
        AgentCoreDeploymentError,
        match="2,048-character",
    ):
        deployment._athena_contexts(AgentCoreSetupConfig.from_mapping(value))


def test_production_shared_runtime_configuration_is_locked():
    config = AgentCoreSetupConfig.from_mapping(_external())
    expected = deployment._shared_runtime_configuration(config)

    deployment._validate_shared_runtime_configuration(config, expected)
    for name in expected:
        changed = {**expected, name: "changed"}
        with pytest.raises(
            AgentCoreDeploymentError,
            match=name,
        ):
            deployment._validate_shared_runtime_configuration(
                config,
                changed,
            )

    missing = dict(expected)
    missing.pop("AthenaConfigurationFingerprint")
    with pytest.raises(
        AgentCoreDeploymentError,
        match="maintenance change",
    ):
        deployment._validate_shared_runtime_configuration(config, missing)


def test_alarm_subscription_must_be_confirmed():
    config = AgentCoreSetupConfig.from_mapping(_external())

    class _PendingSns:
        def list_subscriptions_by_topic(self, **kwargs):
            assert kwargs == {"TopicArn": _ALARM_TOPIC_ARN}
            return {
                "Subscriptions": [
                    {
                        "TopicArn": _ALARM_TOPIC_ARN,
                        "Protocol": "email",
                        "Endpoint": config.admin.email,
                        "SubscriptionArn": "PendingConfirmation",
                    }
                ]
            }

    class _Session:
        def client(self, service_name, *, region_name):
            assert service_name == "sns"
            assert region_name == config.aws_region
            return _PendingSns()

    with pytest.raises(
        AgentCoreDeploymentError,
        match="not confirmed",
    ):
        deployment._verify_confirmed_alarm_subscription(
            _Session(),
            config=config,
            outputs={"AlarmTopicArn": _ALARM_TOPIC_ARN},
        )


def test_cdk_tool_cache_marker_detects_tampering(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "tools"
    python = root / ".venv" / "bin" / "python"
    cdk = root / "node_modules" / ".bin" / "cdk"
    python.parent.mkdir(parents=True)
    cdk.parent.mkdir(parents=True)
    python.write_text("python", encoding="ascii")
    cdk.write_text("cdk", encoding="ascii")
    marker = root / ".complete"
    infra_digest = "a" * 64
    monkeypatch.setattr(deployment, "INFRA_TOOLS_ROOT", root)
    marker.write_text(
        json.dumps(
            {
                "infraDigest": infra_digest,
                "toolsDigest": deployment._tool_tree_digest(root),
            }
        ),
        encoding="ascii",
    )

    assert deployment._valid_tool_cache_marker(
        marker,
        python=python,
        cdk=cdk,
        infra_digest=infra_digest,
    )
    cdk.write_text("tampered", encoding="ascii")
    assert not deployment._valid_tool_cache_marker(
        marker,
        python=python,
        cdk=cdk,
        infra_digest=infra_digest,
    )


class _AwsError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


@pytest.fixture
def deployment_preflights(monkeypatch):
    identity = deployment.AwsIdentity(
        account_id="123456789012",
        partition="aws",
    )
    policy_arns = tuple(
        "arn:aws:iam::123456789012:policy/"
        f"AxonLLMAgentCoreCloudFormationExecution-axprod-us-east-1-part{part}"
        for part in range(1, 4)
    )
    monkeypatch.setattr(
        deployment,
        "_validate_prefix_list_inputs",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        deployment,
        "_assert_no_retained_runtime_without_stack",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        deployment,
        "_assert_no_retained_identity_without_stack",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        deployment,
        "_require_bootstrap_execution_policy",
        lambda *_args, **_kwargs: (identity, policy_arns),
    )
    monkeypatch.setattr(
        deployment,
        "_assert_cdk_execution_role_policy",
        lambda *_args, **_kwargs: None,
    )


class _Sts:
    def get_caller_identity(self):
        return {
            "Account": "123456789012",
            "Arn": "arn:aws:iam::123456789012:user/deployer",
        }


class _BootstrapIam:
    def __init__(self, *, existing_document=None) -> None:
        self.existing_document = existing_document
        self.documents: dict[str, dict] = {}
        self.create_calls: list[dict] = []

    def get_policy(self, *, PolicyArn):
        if PolicyArn not in self.documents and self.existing_document is None:
            raise _AwsError("NoSuchEntity")
        return {
            "Policy": {
                "Arn": PolicyArn,
                "DefaultVersionId": "v1",
            }
        }

    def create_policy(self, **kwargs):
        self.create_calls.append(deepcopy(kwargs))
        arn = f"arn:aws:iam::123456789012:policy/{kwargs['PolicyName']}"
        self.documents[arn] = json.loads(kwargs["PolicyDocument"])
        return {
            "Policy": {
                "Arn": arn,
                "DefaultVersionId": "v1",
            }
        }

    def get_policy_version(self, *, PolicyArn, VersionId):
        assert VersionId == "v1"
        return {
            "PolicyVersion": {
                "Document": self.documents.get(
                    PolicyArn,
                    self.existing_document,
                )
            }
        }


class _PreflightSession:
    def __init__(self, **clients) -> None:
        self.clients = {"sts": _Sts(), **clients}

    def client(self, service_name, *, region_name):
        assert region_name == "us-east-1"
        return self.clients[service_name]


def test_bootstrap_requires_repository_execution_policy_without_admin():
    config = AgentCoreSetupConfig.from_mapping(_external())
    iam = _BootstrapIam()
    identity, policy_arns = deployment._require_bootstrap_execution_policy(
        _PreflightSession(iam=iam),
        config=config,
        create_if_missing=True,
    )

    assert identity.account_id == "123456789012"
    assert len(iam.create_calls) == 5
    calls = {call["PolicyName"]: call for call in iam.create_calls}
    assert set(calls) == {
        "AxonLLMAgentCoreBootstrapBoundary-axprod-us-east-1",
        "AxonLLMAgentCoreCloudFormationExecution-axprod-us-east-1-part1",
        "AxonLLMAgentCoreCloudFormationExecution-axprod-us-east-1-part2",
        "AxonLLMAgentCoreCloudFormationExecution-axprod-us-east-1-part3",
        "AxonLLMAgentCoreServiceBoundary-axprod-us-east-1",
    }
    for part in range(1, 4):
        execution_call = calls[
            "AxonLLMAgentCoreCloudFormationExecution-"
            f"axprod-us-east-1-part{part}"
        ]
        document = json.loads(execution_call["PolicyDocument"])
        assert len(execution_call["PolicyDocument"]) <= (
            bootstrap_policy.IAM_MANAGED_POLICY_SIZE_LIMIT
        )
        assert all(
            statement["Action"] != "*"
            for statement in document["Statement"]
        )
    assert all(
        policy_arn.endswith(
            "AxonLLMAgentCoreCloudFormationExecution-"
            f"axprod-us-east-1-part{part}"
        )
        for part, policy_arn in enumerate(policy_arns, start=1)
    )
    command = deployment.cdk_bootstrap_command(
        config,
        identity=identity,
        execution_policy_arns=policy_arns,
    )
    assert command[2] == "aws://123456789012/us-east-1"
    assert [
        command[index + 1]
        for index, argument in enumerate(command)
        if argument == "--cloudformation-execution-policies"
    ] == list(policy_arns)
    assert command[command.index("--custom-permissions-boundary") + 1] == (
        "AxonLLMAgentCoreBootstrapBoundary-axprod-us-east-1"
    )
    assert command[command.index("--qualifier") + 1] == "axprod"
    assert command[command.index("--toolkit-stack-name") + 1] == ("AxonLLMToolkit-axprod")
    assert "--termination-protection" in command
    assert "AdministratorAccess" not in " ".join(command)


def test_bootstrap_policy_uses_valid_bounded_lifecycle_and_pass_role_actions():
    document = bootstrap_policy.policy_document(
        partition="aws",
        account_id="123456789012",
        region="us-east-1",
    )
    statements = {statement["Sid"]: statement for statement in document["Statement"]}
    global_actions = set(statements["GlobalAxonLLMInfrastructure"]["Action"])

    assert "s3:PutLifecycleConfiguration" in global_actions
    assert "s3:PutBucketLifecycleConfiguration" not in global_actions
    assert "s3:PutEncryptionConfiguration" in global_actions
    assert "s3:PutBucketEncryption" not in global_actions
    assert {
        "cloudfront:CreateDistribution",
        "cloudfront:CreateFunction",
        "cloudfront:CreateVpcOrigin",
        "cloudfront:DeleteDistribution",
        "cloudfront:DeleteFunction",
        "cloudfront:DeleteVpcOrigin",
        "cloudfront:GetDistribution",
        "cloudfront:GetDistributionConfig",
        "cloudfront:GetVpcOrigin",
        "cloudfront:PublishFunction",
        "cloudfront:UpdateDistribution",
        "cloudfront:UpdateFunction",
        "cloudfront:UpdateVpcOrigin",
        "s3:GetBucketOwnershipControls",
        "s3:GetBucketPolicyStatus",
        "s3:GetBucketPublicAccessBlock",
        "s3:GetBucketTagging",
        "s3:GetLifecycleConfiguration",
    } <= global_actions
    assert not any(action in global_actions for action in {"s3:GetObject", "s3:GetObjectVersion"})
    regional_actions = set(
        statements["RegionalAxonLLMInfrastructure"]["Action"]
    )
    assert {
        "wafv2:CreateIPSet",
        "wafv2:CreateWebACL",
        "wafv2:DeleteIPSet",
        "wafv2:DeleteWebACL",
        "wafv2:GetIPSet",
        "wafv2:GetWebACL",
        "wafv2:UpdateIPSet",
        "wafv2:UpdateWebACL",
    } <= regional_actions
    assert statements["CreateBoundedAxonLLMServiceRoles"]["Action"] == ("iam:CreateRole")
    create_conditions = statements["CreateBoundedAxonLLMServiceRoles"]["Condition"]["StringEquals"]
    assert create_conditions == {
        "aws:RequestTag/Application": "AxonLLM",
        "aws:RequestTag/AxonLLMTrustDomain": "axprod",
        "iam:PermissionsBoundary": (
            "arn:aws:iam::123456789012:policy/AxonLLMAgentCoreServiceBoundary-axprod-us-east-1"
        ),
    }
    managed_actions = set(statements["ManageBoundedAxonLLMServiceRoles"]["Action"])
    assert {
        "iam:AttachRolePolicy",
        "iam:CreateRole",
        "iam:DeleteRolePermissionsBoundary",
        "iam:PassRole",
        "iam:PutRolePermissionsBoundary",
        "iam:UpdateAssumeRolePolicy",
    }.isdisjoint(managed_actions)
    assert statements["ManageBoundedAxonLLMServiceRoles"]["Condition"] == {
        "StringEquals": {
            "aws:ResourceTag/Application": "AxonLLM",
            "aws:ResourceTag/AxonLLMTrustDomain": "axprod",
        }
    }
    provider_roles = statements["CreateBoundedCdkProviderRoles"]["Resource"]
    assert provider_roles
    assert all(
        resource.startswith("arn:aws:iam::123456789012:role/AxonLLM") and "CustomResourceProviderRole-*" in resource
        for resource in provider_roles
    )
    assert statements["CreateBoundedCdkProviderRoles"]["Condition"] == {
        "StringEquals": {
            "iam:PermissionsBoundary": (
                "arn:aws:iam::123456789012:policy/AxonLLMAgentCoreServiceBoundary-axprod-us-east-1"
            )
        }
    }
    assert statements["PassAxonLLMServiceRoles"]["Action"] == ("iam:PassRole")
    assert statements["PassAxonLLMServiceRoles"]["Condition"] == {
        "StringEquals": {
            "iam:PassedToService": [
                "backup.amazonaws.com",
                "bedrock-agentcore.amazonaws.com",
                "ecs-tasks.amazonaws.com",
                "lambda.amazonaws.com",
            ]
        }
    }
    assert statements["CreateRequiredServiceLinkedRoles"]["Condition"]["StringEquals"]["iam:AWSServiceName"] == [
        "bedrock-agentcore.amazonaws.com",
        "cloudfront.amazonaws.com",
        "ecs.amazonaws.com",
        "ecs.application-autoscaling.amazonaws.com",
        "email.cognito-idp.amazonaws.com",
        "elasticloadbalancing.amazonaws.com",
    ]


@pytest.mark.parametrize("qualifier", ("axprod", "axqual", "axext"))
def test_bootstrap_execution_policy_parts_fit_iam_limit_and_preserve_actions(
    qualifier,
):
    complete = bootstrap_policy.policy_document(
        partition="aws",
        account_id="123456789012",
        region="us-east-1",
        qualifier=qualifier,
    )
    parts = bootstrap_policy.policy_documents(
        partition="aws",
        account_id="123456789012",
        region="us-east-1",
        qualifier=qualifier,
    )

    assert len(parts) == bootstrap_policy.EXECUTION_POLICY_PART_COUNT
    assert all(
        len(
            json.dumps(
                part,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        <= bootstrap_policy.IAM_MANAGED_POLICY_SIZE_LIMIT
        for part in parts
    )
    assert all(
        len(
            json.dumps(
                part,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        <= bootstrap_policy._EXECUTION_POLICY_TARGET_SIZE
        for part in parts
    )
    original_actions = {
        action
        for statement in complete["Statement"]
        for action in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    }
    partitioned_actions = {
        action
        for part in parts
        for statement in part["Statement"]
        for action in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    }
    assert partitioned_actions == original_actions
    assert sum(len(part["Statement"]) for part in parts) == (
        len(complete["Statement"]) + 1
    )


def test_bootstrap_trust_domains_have_distinct_qualifiers_and_boundaries():
    assert bootstrap_policy.qualifier_for_namespace(None) == "axprod"
    assert bootstrap_policy.qualifier_for_namespace("") == "axprod"
    assert bootstrap_policy.qualifier_for_namespace("managed") == "axqual"
    assert bootstrap_policy.qualifier_for_namespace("external-oidc") == "axext"
    names = {
        bootstrap_policy.boundary_name(
            "us-east-1",
            qualifier=qualifier,
        )
        for qualifier in ("axprod", "axqual", "axext")
    }
    assert len(names) == 3

    boundary = bootstrap_policy.bootstrap_boundary_document(
        partition="aws",
        account_id="123456789012",
        region="us-east-1",
        qualifier="axprod",
    )
    denied = {
        action
        for statement in boundary["Statement"]
        if statement["Effect"] == "Deny"
        for action in ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])
    }
    assert {
        "iam:AttachRolePolicy",
        "iam:UpdateAssumeRolePolicy",
    } <= denied


class _CdkExecutionRoleIam:
    def __init__(
        self,
        *,
        policy_arns: tuple[str, ...],
        boundary_arn: str,
    ) -> None:
        self.policy_arns = policy_arns
        self.boundary_arn = boundary_arn
        self.role_names: list[str] = []

    def list_attached_role_policies(self, *, RoleName, **_kwargs):
        self.role_names.append(RoleName)
        return {
            "AttachedPolicies": [
                {"PolicyArn": policy_arn}
                for policy_arn in reversed(self.policy_arns)
            ],
            "IsTruncated": False,
        }

    def list_role_policies(self, *, RoleName):
        self.role_names.append(RoleName)
        return {"PolicyNames": []}

    def get_role(self, *, RoleName):
        self.role_names.append(RoleName)
        return {
            "Role": {
                "PermissionsBoundary": {
                    "PermissionsBoundaryArn": self.boundary_arn,
                    "PermissionsBoundaryType": "Policy",
                }
            }
        }


def test_cdk_execution_role_requires_exact_policy_boundary_and_qualifier():
    config = AgentCoreSetupConfig.from_mapping(_external())
    identity = deployment.AwsIdentity(
        account_id="123456789012",
        partition="aws",
    )
    policy_arns = tuple(
        "arn:aws:iam::123456789012:policy/"
        f"AxonLLMAgentCoreCloudFormationExecution-axprod-us-east-1-part{part}"
        for part in range(1, 4)
    )
    boundary_arn = "arn:aws:iam::123456789012:policy/AxonLLMAgentCoreBootstrapBoundary-axprod-us-east-1"
    iam = _CdkExecutionRoleIam(
        policy_arns=policy_arns,
        boundary_arn=boundary_arn,
    )

    deployment._assert_cdk_execution_role_policy(
        _PreflightSession(iam=iam),
        config=config,
        identity=identity,
        expected_policy_arns=policy_arns,
    )

    assert set(iam.role_names) == {"cdk-axprod-cfn-exec-role-123456789012-us-east-1"}

    iam.boundary_arn = boundary_arn.replace("axprod", "axqual")
    with pytest.raises(
        AgentCoreDeploymentError,
        match="exact permissions boundary",
    ):
        deployment._assert_cdk_execution_role_policy(
            _PreflightSession(iam=iam),
            config=config,
            identity=identity,
            expected_policy_arns=policy_arns,
        )


def test_bootstrap_rejects_repository_policy_drift():
    config = AgentCoreSetupConfig.from_mapping(_external())
    iam = _BootstrapIam(
        existing_document={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "*",
                    "Resource": "*",
                }
            ],
        }
    )

    with pytest.raises(
        AgentCoreDeploymentError,
        match="differs from this repository",
    ):
        deployment._require_bootstrap_execution_policy(
            _PreflightSession(iam=iam),
            config=config,
            create_if_missing=False,
        )


class _PrefixLists:
    def __init__(
        self,
        entries: dict[str, list[str]],
        *,
        owner: str = "123456789012",
    ) -> None:
        self.entries = entries
        self.owner = owner

    def describe_managed_prefix_lists(self, *, PrefixListIds):
        prefix_list_id = PrefixListIds[0]
        return {
            "PrefixLists": [
                {
                    "AddressFamily": "IPv4",
                    "OwnerId": self.owner,
                    "PrefixListId": prefix_list_id,
                    "State": "create-complete",
                    "Version": 7,
                }
            ]
        }

    def get_managed_prefix_list_entries(self, **kwargs):
        assert kwargs["TargetVersion"] == 7
        return {"Entries": [{"Cidr": cidr} for cidr in self.entries[kwargs["PrefixListId"]]]}


def test_prefix_lists_are_aws_validated_for_owner_and_safe_entries():
    config = AgentCoreSetupConfig.from_mapping(_base())
    deployment._validate_prefix_list_inputs(
        _PreflightSession(
            ec2=_PrefixLists(
                {
                    "pl-123abc": ["8.8.8.0/24"],
                    "pl-456def": ["1.1.1.0/24"],
                }
            )
        ),
        config=config,
    )

    with pytest.raises(
        AgentCoreDeploymentError,
        match="customer-owned",
    ):
        deployment._validate_prefix_list_inputs(
            _PreflightSession(
                ec2=_PrefixLists(
                    {
                        "pl-123abc": ["8.8.8.0/24"],
                        "pl-456def": ["1.1.1.0/24"],
                    },
                    owner="210987654321",
                )
            ),
            config=config,
        )

    with pytest.raises(AgentCoreDeploymentError, match="unsafe CIDR"):
        deployment._validate_prefix_list_inputs(
            _PreflightSession(
                ec2=_PrefixLists(
                    {
                        "pl-123abc": ["0.0.0.0/0"],
                        "pl-456def": ["1.1.1.0/24"],
                    }
                )
            ),
            config=config,
        )


def test_retained_resources_fail_before_deployment_mutation():
    config = AgentCoreSetupConfig.from_mapping(_base())

    class _DynamoDb:
        def describe_table(self, **kwargs):
            return {"Table": {"TableName": kwargs["TableName"]}}

    with pytest.raises(
        AgentCoreDeploymentError,
        match="import the retained table.*no AWS resources were changed",
    ):
        deployment._assert_no_retained_runtime_without_stack(
            _PreflightSession(dynamodb=_DynamoDb()),
            config=config,
        )

    class _CognitoDomain:
        def describe_user_pool_domain(self, **_kwargs):
            return {"DomainDescription": {"UserPoolId": "us-east-1_POOL"}}

    with pytest.raises(
        AgentCoreDeploymentError,
        match="recover or import.*no AWS resources were changed",
    ):
        deployment._assert_no_retained_identity_without_stack(
            _PreflightSession(**{"cognito-idp": _CognitoDomain()}),
            config=config,
        )


class _Cognito:
    def __init__(self) -> None:
        self.user: dict | None = None
        self.create_calls: list[dict] = []

    def admin_get_user(self, **kwargs):
        if self.user is None:
            raise _AwsError("UserNotFoundException")
        return deepcopy(self.user)

    def admin_create_user(self, **kwargs):
        self.create_calls.append(deepcopy(kwargs))
        attributes = deepcopy(kwargs["UserAttributes"])
        attributes.append({"Name": "sub", "Value": "cognito-subject"})
        self.user = {
            "Enabled": True,
            "UserStatus": "FORCE_CHANGE_PASSWORD",
            "UserAttributes": attributes,
        }
        return {"User": deepcopy(self.user)}


class _ConfirmedSns:
    def list_subscriptions_by_topic(self, **kwargs):
        assert kwargs == {"TopicArn": _ALARM_TOPIC_ARN}
        return {
            "Subscriptions": [
                {
                    "TopicArn": _ALARM_TOPIC_ARN,
                    "Protocol": "email",
                    "Endpoint": "admin@example.com",
                    "SubscriptionArn": (f"{_ALARM_TOPIC_ARN}:11111111-2222-3333-4444-555555555555"),
                }
            ]
        }


def test_managed_admin_invitation_is_idempotent_and_strongly_checked():
    client = _Cognito()
    arguments = {
        "user_pool_id": "us-east-1_POOL",
        "user_name": "admin@example.com",
        "email": "admin@example.com",
        "tenant_id": "tenant-a",
        "project_id": "project-a",
    }

    first = ensure_managed_cognito_admin(client, **arguments)
    second = ensure_managed_cognito_admin(client, **arguments)

    assert first.created is True
    assert second.created is False
    assert first.subject == second.subject == "cognito-subject"
    assert len(client.create_calls) == 1
    assert client.create_calls[0]["DesiredDeliveryMediums"] == ["EMAIL"]


def test_managed_admin_rerun_refuses_tenant_reassignment():
    client = _Cognito()
    client.admin_create_user(
        UserPoolId="us-east-1_POOL",
        Username="admin@example.com",
        DesiredDeliveryMediums=["EMAIL"],
        UserAttributes=[
            {"Name": "email", "Value": "admin@example.com"},
            {"Name": "email_verified", "Value": "true"},
            {"Name": "custom:tenant_id", "Value": "other-tenant"},
            {"Name": "custom:project_id", "Value": "project-a"},
        ],
    )

    with pytest.raises(
        AgentCoreDeploymentError,
        match="conflicting attributes: custom:tenant_id",
    ):
        ensure_managed_cognito_admin(
            client,
            user_pool_id="us-east-1_POOL",
            user_name="admin@example.com",
            email="admin@example.com",
            tenant_id="tenant-a",
            project_id="project-a",
        )


def test_existing_managed_admin_verification_never_creates_authority():
    client = _Cognito()

    with pytest.raises(
        AgentCoreDeploymentError,
        match="routine candidate staging will not create",
    ):
        ensure_managed_cognito_admin(
            client,
            user_pool_id="us-east-1_POOL",
            user_name="admin@example.com",
            email="admin@example.com",
            tenant_id="tenant-a",
            project_id="project-a",
            allow_create=False,
        )

    assert client.create_calls == []


def test_existing_identity_is_verified_without_a_stack_update(tmp_path):
    config = AgentCoreSetupConfig.from_mapping(_base())
    cognito = _Cognito()
    cognito.admin_create_user(
        UserPoolId="us-east-1_POOL",
        Username="admin@example.com",
        DesiredDeliveryMediums=["EMAIL"],
        UserAttributes=[
            {"Name": "email", "Value": "admin@example.com"},
            {"Name": "email_verified", "Value": "true"},
            {"Name": "custom:tenant_id", "Value": "tenant-a"},
            {"Name": "custom:project_id", "Value": "project-a"},
        ],
    )
    outputs = {
        "TenantClaimName": "custom:tenant_id",
        "ProjectClaimName": "custom:project_id",
        "OidcClientId": "public-client",
        "OidcAudience": "public-client",
        "CertificationClientId": "certification-client",
        "OidcIssuer": ("https://cognito-idp.us-east-1.amazonaws.com/us-east-1_POOL"),
        "OidcDiscoveryUrl": (
            "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_POOL/.well-known/openid-configuration"
        ),
        "HostedUiDomain": ("https://axonllm-123456789012.auth.us-east-1.amazoncognito.com"),
        "UserPoolId": "us-east-1_POOL",
    }

    class _CloudFormation:
        def describe_stacks(self, **kwargs):
            assert kwargs == {"StackName": deployment.IDENTITY_STACK}
            return {
                "Stacks": [
                    {
                        "StackStatus": "UPDATE_COMPLETE",
                        "Parameters": [
                            {
                                "ParameterKey": name,
                                "ParameterValue": value,
                            }
                            for name, value in deployment._identity_parameters(config).items()
                        ],
                        "Outputs": [
                            {
                                "OutputKey": name,
                                "OutputValue": value,
                            }
                            for name, value in outputs.items()
                        ],
                    }
                ]
            }

    class _Session:
        def client(self, service_name, *, region_name):
            assert service_name == "cognito-idp"
            assert region_name == "us-east-1"
            return cognito

    identity, subject = deployment._managed_identity_for_candidate(
        config,
        cloudformation_client=_CloudFormation(),
        boto3_session=_Session(),
        outputs_dir=tmp_path,
        assume_yes=True,
        runner=lambda *_args: pytest.fail("existing identity must not be deployed"),
        existing_runtime=True,
    )

    assert identity.user_pool_id == "us-east-1_POOL"
    assert subject == "cognito-subject"
    assert len(cognito.create_calls) == 1
    written = json.loads((tmp_path / "identity-outputs.json").read_text(encoding="utf-8"))
    assert written[deployment.IDENTITY_STACK] == outputs


def test_existing_stack_accepts_completed_update_rollback_only():
    class _CloudFormation:
        def __init__(self, status: str) -> None:
            self.status = status

        def describe_stacks(self, **kwargs):
            assert kwargs == {"StackName": "Stack"}
            return {
                "Stacks": [
                    {
                        "StackStatus": self.status,
                    }
                ]
            }

    assert deployment._existing_stack(
        _CloudFormation("UPDATE_ROLLBACK_COMPLETE"),
        "Stack",
    ) == {"StackStatus": "UPDATE_ROLLBACK_COMPLETE"}
    with pytest.raises(
        AgentCoreDeploymentError,
        match="not in a stable successful state",
    ):
        deployment._existing_stack(
            _CloudFormation("ROLLBACK_COMPLETE"),
            "Stack",
        )
    assert deployment._existing_stack(
        _CloudFormation("ROLLBACK_COMPLETE"),
        "Stack",
        allow_failed_creation=True,
    ) == {"StackStatus": "ROLLBACK_COMPLETE"}


def test_canonical_bootstrap_retry_is_allowed_until_production_exists(
    monkeypatch,
):
    config = AgentCoreSetupConfig.from_mapping(_external())
    calls: list[str] = []

    def bootstrap(*_args, **_kwargs):
        calls.append("bootstrap")
        return {"principal_id": "principal", "project_id": "project-a"}

    def verify(*_args, **_kwargs):
        calls.append("verify")
        return {"principal_id": "principal", "project_id": "project-a"}

    monkeypatch.setattr(
        deployment,
        "bootstrap_canonical_admin",
        bootstrap,
    )
    monkeypatch.setattr(
        deployment,
        "verify_canonical_admin",
        verify,
    )
    arguments = {
        "table_name": "axonllm-agentcore-state",
        "issuer": "https://idp.example.com/oauth2/default",
        "subject": "subject",
    }

    deployment._ensure_canonical_admin(
        config,
        production_runtime_version="",
        **arguments,
    )
    deployment._ensure_canonical_admin(
        config,
        production_runtime_version="7",
        **arguments,
    )

    assert calls == ["bootstrap", "verify"]


def test_first_managed_deploy_stages_identity_and_runtime_only(
    monkeypatch,
    tmp_path,
    capsys,
    deployment_preflights,
):
    setup = _base()
    setup["runtime"]["enabled_providers"] = ["bedrock"]
    config = AgentCoreSetupConfig.from_mapping(setup)
    cognito = _Cognito()
    commands: list[list[str]] = []
    bootstrap_calls: list[dict] = []

    class _Session:
        def client(self, service_name, *, region_name):
            assert region_name == "us-east-1"
            if service_name == "cognito-idp":
                return cognito
            if service_name == "sns":
                return _ConfirmedSns()
            assert service_name == "cloudformation"
            return object()

    outputs = {
        deployment.IDENTITY_STACK: {
            "TenantClaimName": "custom:tenant_id",
            "ProjectClaimName": "custom:project_id",
            "OidcClientId": "public-client",
            "OidcAudience": "public-client",
            "CertificationClientId": "certification-client",
            "OidcIssuer": ("https://cognito-idp.us-east-1.amazonaws.com/us-east-1_POOL"),
            "OidcDiscoveryUrl": (
                "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_POOL/.well-known/openid-configuration"
            ),
            "HostedUiDomain": ("https://axonllm-123456789012.auth.us-east-1.amazoncognito.com"),
            "UserPoolId": "us-east-1_POOL",
        },
        deployment.AGENTCORE_STACK: {
            "StateTableName": "axonllm-agentcore-state",
            "SelectedRuntimeStateTableName": ("axonllm-agentcore-state"),
            "RecoveryCutoverMode": "normal",
            "RecoveryApprovalId": "",
            "RuntimeExecutionRoleArn": ("arn:aws:iam::123456789012:role/axonllm-agentcore-runtime-us-east-1"),
            "RuntimeArn": ("arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/axonllm"),
            "CandidateRuntimeEndpointArn": (
                "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
                "runtime/axonllm/runtime-endpoint/"
                f"{_CANDIDATE_ENDPOINT_NAME}"
            ),
            "CandidateRuntimeEndpointName": _CANDIDATE_ENDPOINT_NAME,
            "CandidateRuntimeVersion": "2",
            "EnabledProviders": "bedrock",
            "RuntimeVersion": "2",
            "ProviderSecretArn": ("arn:aws:secretsmanager:us-east-1:123456789012:secret:provider-AbCd12"),
            "ProviderSecretVersion": "version-2",
            "AlarmTopicArn": _ALARM_TOPIC_ARN,
            **deployment._shared_runtime_configuration(config),
        },
        deployment.CONTROL_PLANE_STACK: {
            "AgentCoreStackName": deployment.AGENTCORE_STACK,
            "LoadBalancerDnsName": "internal.example.elb.amazonaws.com",
            "PrimaryStateTableName": "axonllm-agentcore-state",
            "SelectedRuntimeStateTableName": ("axonllm-agentcore-state"),
            "RecoveryCutoverMode": "normal",
            "RecoveryApprovalId": "",
        },
    }

    def runner(command: list[str], cwd: Path) -> None:
        assert cwd == deployment.INFRA_ROOT
        commands.append(command)
        stack_name = command[command.index("deploy") + 1]
        output_path = Path(command[command.index("--outputs-file") + 1])
        output_path.write_text(
            json.dumps({stack_name: outputs[stack_name]}),
            encoding="utf-8",
        )

    def bootstrap(config, **kwargs):
        bootstrap_calls.append(kwargs)
        return {
            "principal_id": "principal-1",
            "project_id": config.tenant.project_id,
        }

    monkeypatch.setattr(
        deployment,
        "_assert_deployment_prerequisites",
        lambda: None,
    )
    monkeypatch.setattr(
        deployment,
        "bootstrap_canonical_admin",
        bootstrap,
    )
    monkeypatch.setattr(
        deployment,
        "_existing_agentcore_outputs",
        lambda _client: None,
    )
    monkeypatch.setattr(
        deployment,
        "_existing_stack",
        lambda _client, _stack_name: None,
    )
    monkeypatch.setattr(
        deployment,
        "_new_candidate_endpoint_name",
        lambda: _CANDIDATE_ENDPOINT_NAME,
    )
    monkeypatch.setattr(
        deployment,
        "_sync_provider_credentials",
        lambda *args, **kwargs: deployment.ProviderSecretVersion(
            secret_arn=outputs[deployment.AGENTCORE_STACK]["ProviderSecretArn"],
            version_id="version-2",
            previous_version_id="version-1",
            changed=True,
            configured_fields=(),
            fingerprint="a" * 64,
        ),
    )

    deployment.deploy(
        config,
        outputs_dir=tmp_path / "outputs",
        assume_yes=True,
        bootstrap_cdk=False,
        runner=runner,
        boto3_session=_Session(),
        provider_environment={},
    )

    assert [command[command.index("deploy") + 1] for command in commands] == [
        deployment.IDENTITY_STACK,
        deployment.AGENTCORE_STACK,
        deployment.AGENTCORE_STACK,
    ]
    first_runtime = commands[1]
    second_runtime = commands[2]
    assert "AxonLLMAgentCoreStack:PublishCandidateEndpoint=false" in first_runtime
    assert "AxonLLMAgentCoreStack:PublishProductionEndpoint=false" in first_runtime
    assert "AxonLLMAgentCoreStack:ProviderSecretVersion=bootstrap" in first_runtime
    assert "AxonLLMAgentCoreStack:PublishCandidateEndpoint=true" in second_runtime
    assert "AxonLLMAgentCoreStack:PublishProductionEndpoint=false" in second_runtime
    assert "AxonLLMAgentCoreStack:ProviderSecretVersion=version-2" in second_runtime
    candidate_parameter = f"AxonLLMAgentCoreStack:CandidateEndpointName={_CANDIDATE_ENDPOINT_NAME}"
    assert candidate_parameter in first_runtime
    assert candidate_parameter in second_runtime
    assert bootstrap_calls == [
        {
            "table_name": "axonllm-agentcore-state",
            "issuer": outputs[deployment.IDENTITY_STACK]["OidcIssuer"],
            "subject": "cognito-subject",
        }
    ]
    output = capsys.readouterr().out
    assert "deferred until candidate certification" in output
    assert "Runtime execution role:" in output
    assert "Runtime ARN:" in output


def _candidate_outputs(
    config: AgentCoreSetupConfig,
    *,
    candidate_version: str = "7",
    production_version: str = "6",
) -> dict[str, str]:
    runtime_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/axonllm-AbCdEf1234"
    return {
        "CandidateRuntimeEndpointArn": (f"{runtime_arn}/runtime-endpoint/{_CANDIDATE_ENDPOINT_NAME}"),
        "CandidateRuntimeEndpointName": _CANDIDATE_ENDPOINT_NAME,
        "CandidateRuntimeVersion": candidate_version,
        "EnabledProviders": ",".join(DEFAULT_AGENTCORE_PROVIDERS),
        "ProviderSecretVersion": "provider-version-7",
        "RecoveryCutoverMode": "normal",
        "RuntimeArn": runtime_arn,
        "RuntimeEndpointArn": (f"{runtime_arn}/runtime-endpoint/production"),
        "RuntimeEndpointName": "production",
        "RuntimeVersion": candidate_version,
        "ProductionRuntimeVersion": production_version,
        "AlarmTopicArn": _ALARM_TOPIC_ARN,
        **deployment._shared_runtime_configuration(config),
    }


_CONTROL_PLANE_STACK_ID = (
    "arn:aws:cloudformation:us-east-1:123456789012:stack/AxonLLMControlPlaneStack/11111111-2222-3333-4444-555555555555"
)
_CONTROL_PLANE_ROLE_ARN = "arn:aws:iam::123456789012:role/axonllm-cfn-execution"
_CONTROL_PLANE_ALB_ARN = (
    "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/axonllm-control/0123456789abcdef"
)
_PREVIOUS_CONTROL_IMAGE = f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/control-plane@sha256:{'b' * 64}"
_PRIMARY_STATE_TABLE = "axonllm-agentcore-state"
_SELECTED_STATE_TABLE = "axonllm-agentcore-state-restore-validation-reviewed"
_RECOVERY_APPROVAL_ID = "CHG-2026-015"
_DEPLOYMENT_TRANSITION_ID = "b" * 64


def _managed_transition_config() -> AgentCoreSetupConfig:
    value = _base()
    value["runtime"]["athena_query"] = {
        "role_arns": [_ATHENA_ROLE_ARN],
        "max_rows": 250,
    }
    return AgentCoreSetupConfig.from_mapping(value)


def _promoted_runtime_outputs(
    config: AgentCoreSetupConfig,
) -> dict[str, str]:
    return {
        **_candidate_outputs(
            config,
            candidate_version="7",
            production_version="7",
        ),
        "StateTableName": _PRIMARY_STATE_TABLE,
        "SelectedRuntimeStateTableName": _SELECTED_STATE_TABLE,
        "RecoveryApprovalId": _RECOVERY_APPROVAL_ID,
    }


def _control_transition_metadata(
    config: AgentCoreSetupConfig,
    runtime_outputs: dict[str, str],
    *,
    previous_parameters: dict[str, str] | None,
) -> dict:
    stack_existed = previous_parameters is not None
    return {
        "candidateEndpointName": _CANDIDATE_ENDPOINT_NAME,
        "candidateRuntimeVersion": "7",
        "controlPlane": {
            "previousParameters": previous_parameters,
            "previousStackId": (_CONTROL_PLANE_STACK_ID if stack_existed else None),
            "stackExisted": stack_existed,
            "targetImage": _CONTROL_IMAGE,
        },
        "enabledProviders": runtime_outputs["EnabledProviders"],
        "previousProductionRuntimeVersion": "6",
        "productionEndpointArn": runtime_outputs["RuntimeEndpointArn"],
        "productionRuntimeVersion": "7",
        "providerSecretVersion": runtime_outputs["ProviderSecretVersion"],
        "region": config.aws_region,
        "runtimeArn": runtime_outputs["RuntimeArn"],
        "schemaVersion": 3,
        "sharedRuntimeConfiguration": (deployment._shared_runtime_configuration(config)),
        "transition": {
            "changeId": "CHG-2026-001",
            "deploymentCommit": "a" * 40,
            "repository": "owner/repo",
            "rollbackNotBefore": "2026-08-11T16:00:00+00:00",
            "runAttempt": "1",
            "runId": "42",
            "transitionId": _DEPLOYMENT_TRANSITION_ID,
        },
    }


def _write_control_transition(
    outputs_dir: Path,
    metadata: dict,
) -> None:
    (outputs_dir / "promotion.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )


def _target_control_parameters(
    config: AgentCoreSetupConfig,
) -> dict[str, str]:
    return deployment._control_plane_parameters(
        config,
        primary_state_table_name=_PRIMARY_STATE_TABLE,
        runtime_state_table_name=_SELECTED_STATE_TABLE,
        recovery_approval_id=_RECOVERY_APPROVAL_ID,
        deployment_transition_id=_DEPLOYMENT_TRANSITION_ID,
    )


def _previous_control_parameters(
    target_parameters: dict[str, str],
) -> dict[str, str]:
    return {
        **target_parameters,
        "ApprovedIngressPrefixListId": "pl-previous",
        "ControlPlaneVerifiedImageUri": _PREVIOUS_CONTROL_IMAGE,
        "DeploymentTransitionId": "a" * 64,
        "RecoveryApprovalId": "",
        "RuntimeStateTableName": "",
    }


def _control_outputs(
    runtime_outputs: dict[str, str],
    *,
    image: str = _CONTROL_IMAGE,
) -> dict[str, str]:
    return {
        "AgentCoreStackName": deployment.AGENTCORE_STACK,
        "ClusterName": "axonllm-control",
        "ControlPlaneAuthMode": "alb-cognito",
        "ControlPlaneDomainName": "axon.example.com",
        "ControlPlaneImageUri": image,
        "ControlPlaneUrl": "https://axon.example.com",
        "DeploymentTransitionId": _DEPLOYMENT_TRANSITION_ID,
        "EndpointMode": "custom-domain",
        "LoadBalancerScheme": "internet-facing",
        "PrimaryStateTableName": runtime_outputs["StateTableName"],
        "QueryPlaneEnabled": "true",
        "RecoveryApprovalId": runtime_outputs.get(
            "RecoveryApprovalId",
            "",
        ),
        "RecoveryCutoverMode": "normal",
        "SelectedRuntimeStateTableName": runtime_outputs["SelectedRuntimeStateTableName"],
        "ServiceName": "axonllm-control-web",
        "TaskDefinitionArn": ("arn:aws:ecs:us-east-1:123456789012:task-definition/axonllm-control:7"),
        "TargetGroupArn": (
            "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/axonllm-control/0123456789abcdef"
        ),
    }


def _cloudfront_control_outputs(
    runtime_outputs: dict[str, str],
) -> dict[str, str]:
    domain_name = "d111111abcdef8.cloudfront.net"
    return {
        **_control_outputs(runtime_outputs),
        "BrowserClientId": "browser-client-id",
        "ControlPlaneAuthMode": "application-oidc",
        "ControlPlaneDomainName": domain_name,
        "ControlPlaneUrl": f"https://{domain_name}",
        "DistributionDomainName": domain_name,
        "DistributionId": "E1234567890ABC",
        "EndpointMode": "cloudfront",
        "LoadBalancerScheme": "internal",
        "VpcOriginId": "vo_1234567890",
        "WebAclArn": (
            "arn:aws:wafv2:us-east-1:123456789012:"
            "global/webacl/axonllm/11111111-2222-3333-4444-555555555555"
        ),
    }


def test_control_plane_output_validation_locks_endpoint_architecture():
    config = _managed_transition_config()
    runtime_outputs = _promoted_runtime_outputs(config)
    custom_outputs = _control_outputs(runtime_outputs)
    legacy_outputs = {
        name: value
        for name, value in custom_outputs.items()
        if name
        not in {
            "ControlPlaneAuthMode",
            "ControlPlaneDomainName",
            "ControlPlaneUrl",
            "EndpointMode",
            "LoadBalancerScheme",
        }
    }

    deployment._validate_control_plane_outputs(
        legacy_outputs,
        runtime_outputs=runtime_outputs,
        expected_image=_CONTROL_IMAGE,
        expected_endpoint_mode="custom-domain",
    )
    with pytest.raises(
        AgentCoreDeploymentError,
        match="predate the requested endpoint architecture",
    ):
        deployment._validate_control_plane_outputs(
            legacy_outputs,
            runtime_outputs=runtime_outputs,
            expected_image=_CONTROL_IMAGE,
            expected_endpoint_mode="cloudfront",
        )

    cloudfront_outputs = _cloudfront_control_outputs(runtime_outputs)
    deployment._validate_control_plane_outputs(
        cloudfront_outputs,
        runtime_outputs=runtime_outputs,
        expected_image=_CONTROL_IMAGE,
        expected_endpoint_mode="cloudfront",
    )
    cloudfront_outputs.pop("WebAclArn")
    with pytest.raises(
        AgentCoreDeploymentError,
        match="WebAclArn is missing or invalid",
    ):
        deployment._validate_control_plane_outputs(
            cloudfront_outputs,
            runtime_outputs=runtime_outputs,
            expected_image=_CONTROL_IMAGE,
            expected_endpoint_mode="cloudfront",
        )


def test_retained_stack_endpoint_mode_defaults_legacy_to_custom_domain():
    legacy_stack = {"Parameters": []}
    deployment._validate_stack_endpoint_mode(
        legacy_stack,
        expected_endpoint_mode="custom-domain",
        stack_name=deployment.CONTROL_PLANE_STACK,
    )
    with pytest.raises(
        AgentCoreDeploymentError,
        match="cannot be changed in place",
    ):
        deployment._validate_stack_endpoint_mode(
            legacy_stack,
            expected_endpoint_mode="cloudfront",
            stack_name=deployment.CONTROL_PLANE_STACK,
        )


def _control_stack(
    parameters: dict[str, str],
    outputs: dict[str, str],
) -> dict:
    return {
        "Outputs": [{"OutputKey": name, "OutputValue": value} for name, value in outputs.items()],
        "Parameters": [{"ParameterKey": name, "ParameterValue": value} for name, value in parameters.items()],
        "RoleARN": _CONTROL_PLANE_ROLE_ARN,
        "StackId": _CONTROL_PLANE_STACK_ID,
        "StackStatus": "UPDATE_COMPLETE",
    }


class _ControlPlaneCloudFormation:
    def __init__(
        self,
        runtime_outputs: dict[str, str],
        control_stack: dict | None,
        *,
        restored_outputs: dict[str, str] | None = None,
    ) -> None:
        self.runtime_outputs = runtime_outputs
        self.control_stack = control_stack
        self.restored_outputs = restored_outputs
        self.update_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self.wait_calls: list[tuple[str, dict]] = []
        self.events: list[str] = []

    def describe_stacks(self, *, StackName):
        if StackName == deployment.AGENTCORE_STACK:
            return {
                "Stacks": [
                    {
                        "Outputs": [
                            {
                                "OutputKey": name,
                                "OutputValue": value,
                            }
                            for name, value in self.runtime_outputs.items()
                        ],
                        "StackStatus": "UPDATE_COMPLETE",
                    }
                ]
            }
        assert StackName == deployment.CONTROL_PLANE_STACK
        if self.control_stack is None:
            raise _AwsError("ValidationError")
        return {"Stacks": [deepcopy(self.control_stack)]}

    def list_stack_resources(self, **kwargs):
        assert kwargs == {"StackName": deployment.CONTROL_PLANE_STACK}
        return {
            "StackResourceSummaries": [
                {
                    "PhysicalResourceId": _CONTROL_PLANE_ALB_ARN,
                    "ResourceStatus": "CREATE_COMPLETE",
                    "ResourceType": ("AWS::ElasticLoadBalancingV2::LoadBalancer"),
                }
            ]
        }

    def update_stack(self, **kwargs):
        assert self.control_stack is not None
        self.update_calls.append(deepcopy(kwargs))
        current = {item["ParameterKey"]: item["ParameterValue"] for item in self.control_stack["Parameters"]}
        for item in kwargs["Parameters"]:
            name = item["ParameterKey"]
            if "ParameterValue" in item:
                current[name] = item["ParameterValue"]
            else:
                assert item == {
                    "ParameterKey": name,
                    "UsePreviousValue": True,
                }
        self.control_stack["Parameters"] = [
            {"ParameterKey": name, "ParameterValue": value} for name, value in current.items()
        ]
        if self.restored_outputs is not None:
            self.control_stack["Outputs"] = [
                {"OutputKey": name, "OutputValue": value} for name, value in self.restored_outputs.items()
            ]

    def delete_stack(self, **kwargs):
        assert self.control_stack is not None
        self.events.append("delete-stack")
        self.delete_calls.append(deepcopy(kwargs))
        self.control_stack = None

    def get_waiter(self, name):
        return SimpleNamespace(wait=lambda **kwargs: self.wait_calls.append((name, deepcopy(kwargs))))


class _ControlPlaneElb:
    def __init__(self, events: list[str]) -> None:
        self.deletion_protection = True
        self.events = events
        self.modify_calls: list[dict] = []

    def describe_load_balancer_attributes(self, **kwargs):
        assert kwargs == {"LoadBalancerArn": _CONTROL_PLANE_ALB_ARN}
        return {
            "Attributes": [
                {
                    "Key": "deletion_protection.enabled",
                    "Value": ("true" if self.deletion_protection else "false"),
                }
            ]
        }

    def modify_load_balancer_attributes(self, **kwargs):
        self.events.append("disable-deletion-protection")
        self.modify_calls.append(deepcopy(kwargs))
        self.deletion_protection = False


class _ControlPlaneSession:
    def __init__(self, cloudformation: _ControlPlaneCloudFormation):
        self.cloudformation = cloudformation
        self.elbv2 = _ControlPlaneElb(cloudformation.events)

    def client(self, service_name, *, region_name):
        assert region_name == "us-east-1"
        if service_name == "cloudformation":
            return self.cloudformation
        assert service_name == "elbv2"
        return self.elbv2


def test_deploy_control_plane_binds_schema_2_runtime_and_image(
    monkeypatch,
    tmp_path,
    deployment_preflights,
):
    config = _managed_transition_config()
    runtime_outputs = _promoted_runtime_outputs(config)
    target_parameters = _target_control_parameters(config)
    previous_parameters = _previous_control_parameters(target_parameters)
    target_outputs = _control_outputs(runtime_outputs)
    metadata = _control_transition_metadata(
        config,
        runtime_outputs,
        previous_parameters=previous_parameters,
    )
    _write_control_transition(tmp_path, metadata)
    client = _ControlPlaneCloudFormation(
        runtime_outputs,
        _control_stack(previous_parameters, target_outputs),
    )
    commands: list[list[str]] = []

    def runner(command: list[str], cwd: Path) -> None:
        assert cwd == deployment.INFRA_ROOT
        commands.append(command)
        output_path = Path(command[command.index("--outputs-file") + 1])
        output_path.write_text(
            json.dumps({deployment.CONTROL_PLANE_STACK: target_outputs}),
            encoding="utf-8",
        )
        client.control_stack = _control_stack(
            target_parameters,
            target_outputs,
        )

    monkeypatch.setattr(
        deployment,
        "_assert_deployment_prerequisites",
        lambda: None,
    )

    deployment.deploy_control_plane(
        config,
        outputs_dir=tmp_path,
        assume_yes=True,
        runner=runner,
        boto3_session=_ControlPlaneSession(client),
    )

    assert len(commands) == 1
    command = commands[0]
    assert (f"AxonLLMControlPlaneStack:ControlPlaneVerifiedImageUri={_CONTROL_IMAGE}") in command
    assert (f"AxonLLMControlPlaneStack:PrimaryStateTableName={_PRIMARY_STATE_TABLE}") in command
    assert (f"AxonLLMControlPlaneStack:RuntimeStateTableName={_SELECTED_STATE_TABLE}") in command
    assert (f"AxonLLMControlPlaneStack:RecoveryApprovalId={_RECOVERY_APPROVAL_ID}") in command
    assert client.control_stack is not None
    assert client.control_stack["StackId"] == metadata["controlPlane"]["previousStackId"]
    assert (
        deployment._stack_parameters(
            client.control_stack,
            stack_name=deployment.CONTROL_PLANE_STACK,
        )
        == target_parameters
    )
    written = json.loads((tmp_path / "control-plane-outputs.json").read_text(encoding="utf-8"))
    assert written[deployment.CONTROL_PLANE_STACK] == target_outputs


def test_deploy_control_plane_rejects_schema_2_for_another_image(
    monkeypatch,
    tmp_path,
):
    config = _managed_transition_config()
    runtime_outputs = _promoted_runtime_outputs(config)
    metadata = _control_transition_metadata(
        config,
        runtime_outputs,
        previous_parameters=None,
    )
    metadata["controlPlane"]["targetImage"] = _PREVIOUS_CONTROL_IMAGE
    _write_control_transition(tmp_path, metadata)
    client = _ControlPlaneCloudFormation(runtime_outputs, None)
    monkeypatch.setattr(
        deployment,
        "_assert_deployment_prerequisites",
        lambda: None,
    )

    with pytest.raises(
        AgentCoreDeploymentError,
        match="metadata is not bound",
    ):
        deployment.deploy_control_plane(
            config,
            outputs_dir=tmp_path,
            assume_yes=True,
            runner=lambda *_args: pytest.fail("mismatched image must not deploy"),
            boto3_session=_ControlPlaneSession(client),
        )


def test_deploy_control_plane_rejects_runtime_drift_from_schema_2(
    monkeypatch,
    tmp_path,
    deployment_preflights,
):
    config = _managed_transition_config()
    prepared_runtime = _promoted_runtime_outputs(config)
    metadata = _control_transition_metadata(
        config,
        prepared_runtime,
        previous_parameters=None,
    )
    _write_control_transition(tmp_path, metadata)
    drifted_runtime = {
        **prepared_runtime,
        "RuntimeVersion": "8",
    }
    client = _ControlPlaneCloudFormation(drifted_runtime, None)
    monkeypatch.setattr(
        deployment,
        "_assert_deployment_prerequisites",
        lambda: None,
    )

    with pytest.raises(
        AgentCoreDeploymentError,
        match="runtime does not match",
    ):
        deployment.deploy_control_plane(
            config,
            outputs_dir=tmp_path,
            assume_yes=True,
            runner=lambda *_args: pytest.fail("drifted runtime must not deploy"),
            boto3_session=_ControlPlaneSession(client),
        )


def test_deploy_control_plane_reconciles_completed_creation(
    monkeypatch,
    tmp_path,
    deployment_preflights,
):
    config = _managed_transition_config()
    runtime_outputs = _promoted_runtime_outputs(config)
    target_parameters = _target_control_parameters(config)
    target_outputs = _control_outputs(runtime_outputs)
    metadata = _control_transition_metadata(
        config,
        runtime_outputs,
        previous_parameters=None,
    )
    _write_control_transition(tmp_path, metadata)
    client = _ControlPlaneCloudFormation(
        runtime_outputs,
        _control_stack(target_parameters, target_outputs),
    )
    monkeypatch.setattr(
        deployment,
        "_assert_deployment_prerequisites",
        lambda: None,
    )

    deployment.deploy_control_plane(
        config,
        outputs_dir=tmp_path,
        assume_yes=True,
        runner=lambda *_args: pytest.fail("completed deployment must reconcile without a rerun"),
        boto3_session=_ControlPlaneSession(client),
    )

    assert client.update_calls == []
    written = json.loads((tmp_path / "control-plane-outputs.json").read_text(encoding="utf-8"))
    assert written[deployment.CONTROL_PLANE_STACK] == target_outputs


def test_rollback_control_plane_restores_previous_state_idempotently(
    tmp_path,
):
    config = _managed_transition_config()
    runtime_outputs = _promoted_runtime_outputs(config)
    target_parameters = _target_control_parameters(config)
    previous_parameters = _previous_control_parameters(target_parameters)
    metadata = _control_transition_metadata(
        config,
        runtime_outputs,
        previous_parameters=previous_parameters,
    )
    _write_control_transition(tmp_path, metadata)
    previous_runtime_outputs = {
        **runtime_outputs,
        "RecoveryApprovalId": "",
        "SelectedRuntimeStateTableName": _PRIMARY_STATE_TABLE,
    }
    previous_outputs = _control_outputs(
        previous_runtime_outputs,
        image=_PREVIOUS_CONTROL_IMAGE,
    )
    client = _ControlPlaneCloudFormation(
        runtime_outputs,
        _control_stack(
            target_parameters,
            _control_outputs(runtime_outputs),
        ),
        restored_outputs=previous_outputs,
    )
    session = _ControlPlaneSession(client)

    deployment.rollback_control_plane(
        config,
        outputs_dir=tmp_path,
        boto3_session=session,
    )

    assert len(client.update_calls) == 1
    update = client.update_calls[0]
    assert update["UsePreviousTemplate"] is True
    assert update["RoleARN"] == _CONTROL_PLANE_ROLE_ARN
    assert {item["ParameterKey"]: item["ParameterValue"] for item in update["Parameters"]} == previous_parameters
    assert client.control_stack is not None
    assert (
        deployment._stack_parameters(
            client.control_stack,
            stack_name=deployment.CONTROL_PLANE_STACK,
        )
        == previous_parameters
    )
    report_path = tmp_path / "control-plane-rollback.json"
    assert json.loads(report_path.read_text(encoding="utf-8"))["outcome"] == "restored"
    written = json.loads((tmp_path / "control-plane-outputs.json").read_text(encoding="utf-8"))
    assert written[deployment.CONTROL_PLANE_STACK] == previous_outputs

    deployment.rollback_control_plane(
        config,
        outputs_dir=tmp_path,
        boto3_session=session,
    )

    assert len(client.update_calls) == 1
    assert json.loads(report_path.read_text(encoding="utf-8"))["outcome"] == "already-restored"


def test_rollback_control_plane_removes_new_stack_idempotently(
    tmp_path,
):
    config = _managed_transition_config()
    runtime_outputs = _promoted_runtime_outputs(config)
    target_parameters = _target_control_parameters(config)
    metadata = _control_transition_metadata(
        config,
        runtime_outputs,
        previous_parameters=None,
    )
    _write_control_transition(tmp_path, metadata)
    output_path = tmp_path / "control-plane-outputs.json"
    output_path.write_text("{}", encoding="utf-8")
    failed_stack = _control_stack(
        target_parameters,
        _control_outputs(runtime_outputs),
    )
    failed_stack["StackStatus"] = "ROLLBACK_COMPLETE"
    failed_stack["Outputs"] = []
    client = _ControlPlaneCloudFormation(
        runtime_outputs,
        failed_stack,
    )
    session = _ControlPlaneSession(client)

    deployment.rollback_control_plane(
        config,
        outputs_dir=tmp_path,
        boto3_session=session,
    )

    assert client.control_stack is None
    assert len(client.delete_calls) == 1
    assert client.delete_calls[0]["StackName"] == (deployment.CONTROL_PLANE_STACK)
    assert client.delete_calls[0]["RoleARN"] == (_CONTROL_PLANE_ROLE_ARN)
    assert client.events == [
        "disable-deletion-protection",
        "delete-stack",
    ]
    assert session.elbv2.modify_calls == [
        {
            "Attributes": [
                {
                    "Key": "deletion_protection.enabled",
                    "Value": "false",
                }
            ],
            "LoadBalancerArn": _CONTROL_PLANE_ALB_ARN,
        }
    ]
    assert not output_path.exists()
    report_path = tmp_path / "control-plane-rollback.json"
    assert json.loads(report_path.read_text(encoding="utf-8"))["outcome"] == "removed"

    deployment.rollback_control_plane(
        config,
        outputs_dir=tmp_path,
        boto3_session=session,
    )

    assert len(client.delete_calls) == 1
    assert json.loads(report_path.read_text(encoding="utf-8"))["outcome"] == "already-absent"


def test_rollback_refuses_new_stack_owned_by_another_transition(
    tmp_path,
) -> None:
    config = _managed_transition_config()
    runtime_outputs = _promoted_runtime_outputs(config)
    metadata = _control_transition_metadata(
        config,
        runtime_outputs,
        previous_parameters=None,
    )
    _write_control_transition(tmp_path, metadata)
    target_parameters = {
        **_target_control_parameters(config),
        "DeploymentTransitionId": "c" * 64,
    }
    stack = _control_stack(
        target_parameters,
        _control_outputs(runtime_outputs),
    )
    stack["StackStatus"] = "ROLLBACK_COMPLETE"
    stack["Outputs"] = []
    client = _ControlPlaneCloudFormation(runtime_outputs, stack)

    with pytest.raises(
        AgentCoreDeploymentError,
        match="not owned by this transition",
    ):
        deployment.rollback_control_plane(
            config,
            outputs_dir=tmp_path,
            boto3_session=_ControlPlaneSession(client),
        )

    assert client.delete_calls == []
    assert client.events == []


def test_rollback_control_plane_refuses_unrecognized_state(
    tmp_path,
):
    config = _managed_transition_config()
    runtime_outputs = _promoted_runtime_outputs(config)
    target_parameters = _target_control_parameters(config)
    previous_parameters = _previous_control_parameters(target_parameters)
    metadata = _control_transition_metadata(
        config,
        runtime_outputs,
        previous_parameters=previous_parameters,
    )
    _write_control_transition(tmp_path, metadata)
    drifted_parameters = {
        **target_parameters,
        "ControlPlaneVerifiedImageUri": _PREVIOUS_CONTROL_IMAGE,
        "RuntimeStateTableName": "unreviewed-state-table",
    }
    client = _ControlPlaneCloudFormation(
        runtime_outputs,
        _control_stack(
            drifted_parameters,
            _control_outputs(runtime_outputs),
        ),
    )

    with pytest.raises(
        AgentCoreDeploymentError,
        match="no longer matches either side",
    ):
        deployment.rollback_control_plane(
            config,
            outputs_dir=tmp_path,
            boto3_session=_ControlPlaneSession(client),
        )

    assert client.update_calls == []
    assert client.delete_calls == []


def test_candidate_promotion_pins_exact_certified_version(
    monkeypatch,
    tmp_path,
):
    config = AgentCoreSetupConfig.from_mapping(_external())
    before = _candidate_outputs(config)
    updates: list[dict[str, object]] = []

    class _Session:
        def client(self, service_name, *, region_name):
            assert region_name == "us-east-1"
            if service_name == "sns":
                return _ConfirmedSns()
            assert service_name == "cloudformation"
            return object()

    def update(_client, **kwargs):
        assert (tmp_path / "promotion.json").is_file()
        updates.append(kwargs)
        return {
            **before,
            "ProductionRuntimeVersion": "7",
        }

    monkeypatch.setattr(
        deployment,
        "_update_endpoint_publication",
        update,
    )
    monkeypatch.setattr(
        deployment,
        "_existing_agentcore_outputs",
        lambda _client: before,
    )
    transition = {
        "changeId": "CHG-2026-001",
        "deploymentCommit": "a" * 40,
        "repository": "owner/repo",
        "rollbackNotBefore": "2026-08-11T16:00:00+00:00",
        "runAttempt": "1",
        "runId": "42",
        "transitionId": "b" * 64,
    }

    deployment.promote_candidate(
        config,
        candidate_version="7",
        candidate_endpoint_name=_CANDIDATE_ENDPOINT_NAME,
        outputs_dir=tmp_path,
        assume_yes=True,
        runner=lambda *_args: None,
        boto3_session=_Session(),
        transition=transition,
    )

    assert updates == [
        {
            "candidate_endpoint_name": (_CANDIDATE_ENDPOINT_NAME),
            "publish_candidate": True,
            "production_version": "7",
        }
    ]
    promotion = json.loads((tmp_path / "promotion.json").read_text(encoding="utf-8"))
    assert promotion["previousProductionRuntimeVersion"] == "6"
    assert promotion["productionRuntimeVersion"] == "7"
    assert promotion["candidateEndpointName"] == _CANDIDATE_ENDPOINT_NAME
    assert promotion["schemaVersion"] == 3
    assert promotion["transition"] == transition


def test_promotion_finalization_removes_only_candidate_endpoint(
    monkeypatch,
    tmp_path,
):
    config = _managed_transition_config()
    before = _promoted_runtime_outputs(config)
    metadata = _control_transition_metadata(
        config,
        before,
        previous_parameters=None,
    )
    _write_control_transition(tmp_path, metadata)
    updates: list[dict[str, object]] = []

    class _Session:
        def client(self, service_name, *, region_name):
            assert service_name == "cloudformation"
            assert region_name == config.aws_region
            return object()

    def update(_client, **kwargs):
        updates.append(kwargs)
        return {name: value for name, value in before.items() if not name.startswith("Candidate")}

    monkeypatch.setattr(
        deployment,
        "_existing_agentcore_outputs",
        lambda _client: before,
    )
    monkeypatch.setattr(
        deployment,
        "_update_endpoint_publication",
        update,
    )

    deployment.finalize_promotion(
        config,
        outputs_dir=tmp_path,
        boto3_session=_Session(),
    )

    assert updates == [
        {
            "candidate_endpoint_name": (_CANDIDATE_ENDPOINT_NAME),
            "publish_candidate": False,
            "production_version": "7",
        }
    ]
    written = json.loads((tmp_path / "agentcore-outputs.json").read_text(encoding="utf-8"))[deployment.AGENTCORE_STACK]
    assert written["ProductionRuntimeVersion"] == "7"
    assert written["ProviderSecretVersion"] == (before["ProviderSecretVersion"])
    assert not any(name.startswith("Candidate") for name in written)
    report = json.loads((tmp_path / "promotion-finalization.json").read_text(encoding="utf-8"))
    assert report["outcome"] == "finalized"
    assert report["productionRuntimeVersion"] == "7"


def test_promotion_finalization_is_idempotent_when_candidate_is_absent(
    monkeypatch,
    tmp_path,
):
    config = _managed_transition_config()
    promoted = _promoted_runtime_outputs(config)
    metadata = _control_transition_metadata(
        config,
        promoted,
        previous_parameters=None,
    )
    _write_control_transition(tmp_path, metadata)
    finalized = {name: value for name, value in promoted.items() if not name.startswith("Candidate")}

    class _Session:
        def client(self, service_name, *, region_name):
            assert service_name == "cloudformation"
            assert region_name == config.aws_region
            return object()

    monkeypatch.setattr(
        deployment,
        "_existing_agentcore_outputs",
        lambda _client: finalized,
    )
    monkeypatch.setattr(
        deployment,
        "_update_endpoint_publication",
        lambda *_args, **_kwargs: pytest.fail("an already-finalized promotion must not update the stack"),
    )

    deployment.finalize_promotion(
        config,
        outputs_dir=tmp_path,
        boto3_session=_Session(),
    )

    report = json.loads((tmp_path / "promotion-finalization.json").read_text(encoding="utf-8"))
    assert report["outcome"] == "already-finalized"


def test_promotion_finalization_rejects_drift_and_legacy_metadata(
    monkeypatch,
    tmp_path,
):
    config = _managed_transition_config()
    promoted = _promoted_runtime_outputs(config)
    metadata = _control_transition_metadata(
        config,
        promoted,
        previous_parameters=None,
    )
    _write_control_transition(tmp_path, metadata)
    drifted = {
        **promoted,
        "ProductionRuntimeVersion": "6",
    }

    class _Session:
        def client(self, service_name, *, region_name):
            assert service_name == "cloudformation"
            assert region_name == config.aws_region
            return object()

    monkeypatch.setattr(
        deployment,
        "_existing_agentcore_outputs",
        lambda _client: drifted,
    )
    with pytest.raises(
        AgentCoreDeploymentError,
        match="no longer points",
    ):
        deployment.finalize_promotion(
            config,
            outputs_dir=tmp_path,
            boto3_session=_Session(),
        )

    legacy = {
        name: value
        for name, value in metadata.items()
        if name
        not in {
            "controlPlane",
            "enabledProviders",
            "region",
            "sharedRuntimeConfiguration",
            "transition",
        }
    }
    legacy["schemaVersion"] = 1
    _write_control_transition(tmp_path, legacy)
    with pytest.raises(
        AgentCoreDeploymentError,
        match="requires versioned transition metadata",
    ):
        deployment.finalize_promotion(
            config,
            outputs_dir=tmp_path,
            boto3_session=_Session(),
        )


def test_endpoint_publication_reuses_template_and_preserves_parameters():
    config = AgentCoreSetupConfig.from_mapping(_external())
    outputs = _candidate_outputs(config)
    parameters = [
        {
            "ParameterKey": "CandidateEndpointName",
            "ParameterValue": _CANDIDATE_ENDPOINT_NAME,
        },
        {
            "ParameterKey": "ProductionRuntimeVersion",
            "ParameterValue": "6",
        },
        {
            "ParameterKey": "PublishCandidateEndpoint",
            "ParameterValue": "true",
        },
        {
            "ParameterKey": "PublishProductionEndpoint",
            "ParameterValue": "true",
        },
        {
            "ParameterKey": "VerifiedImageUri",
            "ParameterValue": _IMAGE,
        },
    ]

    class _Waiter:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def wait(self, **kwargs):
            self.calls.append(kwargs)

    class _CloudFormation:
        def __init__(self) -> None:
            self.update_calls: list[dict] = []
            self.waiter = _Waiter()

        def describe_stacks(self, **kwargs):
            assert kwargs == {"StackName": deployment.AGENTCORE_STACK}
            current_outputs = {
                **outputs,
                "ProductionRuntimeVersion": ("7" if self.update_calls else "6"),
            }
            return {
                "Stacks": [
                    {
                        "StackStatus": "UPDATE_COMPLETE",
                        "RoleARN": ("arn:aws:iam::123456789012:role/axonllm-cfn-execution"),
                        "Parameters": parameters,
                        "Outputs": [
                            {
                                "OutputKey": name,
                                "OutputValue": value,
                            }
                            for name, value in current_outputs.items()
                        ],
                    }
                ]
            }

        def update_stack(self, **kwargs):
            self.update_calls.append(kwargs)

        def get_waiter(self, name):
            assert name == "stack_update_complete"
            return self.waiter

    client = _CloudFormation()

    after = deployment._update_endpoint_publication(
        client,
        candidate_endpoint_name=_CANDIDATE_ENDPOINT_NAME,
        publish_candidate=True,
        production_version="7",
    )

    assert after["ProductionRuntimeVersion"] == "7"
    assert len(client.update_calls) == 1
    update = client.update_calls[0]
    assert update["UsePreviousTemplate"] is True
    assert update["RoleARN"].endswith("role/axonllm-cfn-execution")
    by_name = {item["ParameterKey"]: item for item in update["Parameters"]}
    assert by_name["ProductionRuntimeVersion"]["ParameterValue"] == "7"
    assert by_name["PublishProductionEndpoint"]["ParameterValue"] == "true"
    assert by_name["VerifiedImageUri"] == {
        "ParameterKey": "VerifiedImageUri",
        "UsePreviousValue": True,
    }
    assert client.waiter.calls == [
        {
            "StackName": deployment.AGENTCORE_STACK,
            "WaiterConfig": {"Delay": 15, "MaxAttempts": 240},
        }
    ]


def test_failed_candidate_discard_preserves_existing_production(
    monkeypatch,
    tmp_path,
):
    config = AgentCoreSetupConfig.from_mapping(_external())
    before = _candidate_outputs(config)
    updates: list[dict[str, object]] = []

    class _Session:
        def client(self, service_name, *, region_name):
            assert service_name == "cloudformation"
            assert region_name == "us-east-1"
            return object()

    def update(_client, **kwargs):
        updates.append(kwargs)
        return {key: value for key, value in before.items() if not key.startswith("Candidate")}

    monkeypatch.setattr(
        deployment,
        "_update_endpoint_publication",
        update,
    )
    monkeypatch.setattr(
        deployment,
        "_existing_agentcore_outputs",
        lambda _client: before,
    )

    deployment.discard_candidate(
        config,
        candidate_version="7",
        candidate_endpoint_name=_CANDIDATE_ENDPOINT_NAME,
        outputs_dir=tmp_path,
        assume_yes=True,
        runner=lambda *_args: None,
        boto3_session=_Session(),
    )

    assert updates == [
        {
            "candidate_endpoint_name": (_CANDIDATE_ENDPOINT_NAME),
            "publish_candidate": False,
            "production_version": "6",
        }
    ]


def test_post_promotion_rollback_restores_previous_version(
    monkeypatch,
    tmp_path,
):
    config = AgentCoreSetupConfig.from_mapping(_external())
    before = _candidate_outputs(
        config,
        candidate_version="7",
        production_version="7",
    )
    updates: list[dict[str, object]] = []
    promotion = {
        "candidateEndpointName": _CANDIDATE_ENDPOINT_NAME,
        "candidateRuntimeVersion": "7",
        "previousProductionRuntimeVersion": "6",
        "productionEndpointArn": before["RuntimeEndpointArn"],
        "productionRuntimeVersion": "7",
        "providerSecretVersion": before["ProviderSecretVersion"],
        "runtimeArn": before["RuntimeArn"],
        "schemaVersion": 1,
    }
    (tmp_path / "promotion.json").write_text(
        json.dumps(promotion),
        encoding="utf-8",
    )

    class _Session:
        def client(self, service_name, *, region_name):
            assert service_name == "cloudformation"
            assert region_name == "us-east-1"
            return object()

    def update(_client, **kwargs):
        updates.append(kwargs)
        after = {key: value for key, value in before.items() if not key.startswith("Candidate")}
        after["ProductionRuntimeVersion"] = "6"
        return after

    monkeypatch.setattr(
        deployment,
        "_update_endpoint_publication",
        update,
    )
    monkeypatch.setattr(
        deployment,
        "_existing_agentcore_outputs",
        lambda _client: before,
    )

    deployment.rollback_promotion(
        config,
        outputs_dir=tmp_path,
        assume_yes=True,
        runner=lambda *_args: None,
        boto3_session=_Session(),
    )

    assert updates == [
        {
            "candidate_endpoint_name": (_CANDIDATE_ENDPOINT_NAME),
            "publish_candidate": False,
            "production_version": "6",
        }
    ]
    report = json.loads((tmp_path / "promotion-rollback.json").read_text(encoding="utf-8"))
    assert report["removedCandidateRuntimeVersion"] == "7"
    assert report["restoredProductionRuntimeVersion"] == "6"


def test_deployment_wrapper_validates_without_aws(tmp_path):
    config_path = write_agentcore_setup(
        AgentCoreSetupConfig.from_mapping(_base()),
        tmp_path / "agentcore.json",
    )
    environment = os.environ.copy()
    environment["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")

    completed = subprocess.run(
        [
            str(_REPO / "deploy-agentcore.sh"),
            "--config",
            str(config_path),
            "--validate-only",
        ],
        cwd=_REPO,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stdout
    assert "Validated authenticated AgentCore configuration" in completed.stdout


def test_deployment_wrapper_never_sources_setup_data():
    script = (_REPO / "deploy-agentcore.sh").read_text(encoding="utf-8")

    assert "source " not in script
    assert "eval " not in script
    assert "with_no_auth" not in script
    assert "src.gateway.deployment.agentcore_deploy" in script
