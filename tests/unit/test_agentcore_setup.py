"""First-adopter AgentCore setup, deployment, and invitation contracts."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts.operations import deploy_agentcore as deployment
from scripts.operations.deploy_agentcore import (
    AgentCoreDeploymentError,
    IdentityValues,
    agentcore_deploy_command,
    control_plane_deploy_command,
    ensure_managed_cognito_admin,
    identity_deploy_command,
)
from src.gateway.agentcore_setup import (
    AgentCoreSetupConfig,
    AgentCoreSetupError,
    load_agentcore_setup,
    local_demo_environment,
    redact_sensitive,
    write_agentcore_setup,
)


_REPO = Path(__file__).resolve().parents[2]
_DIGEST = "e368b7b4522f4838f3ebb4dcc04967682c73cb73e7e40ce16421a6a1ffda6147"
_IMAGE = f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/agentcore@sha256:{_DIGEST}"
_CONTROL_IMAGE = (
    "123456789012.dkr.ecr.us-east-1.amazonaws.com/"
    f"axonllm/control-plane@sha256:{_DIGEST}"
)
_BEDROCK_ARN = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0"
_ATHENA_ROLE_ARN = (
    "arn:aws:iam::123456789012:role/axon-athena-project-a"
)
_CERTIFICATE_ARN = (
    "arn:aws:acm:us-east-1:123456789012:"
    "certificate/11111111-2222-3333-4444-555555555555"
)
_SCIM_SECRET_ARN = (
    "arn:aws:secretsmanager:us-east-1:123456789012:"
    "secret:axonllm/scim-AbCd12"
)


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


def _athena_roles_for_bindings_length(target_length: int) -> list[str]:
    roles = [
        f"arn:aws:iam::{123456789012 + index}:role/r{index}"
        for index in range(4)
    ]

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
    assert (
        payload["control_plane"]["saml_login_path"]
        == "/admin/dashboard"
    )
    assert "subject" not in payload["admin"]
    assert "secret" not in output.read_text(encoding="utf-8").casefold()
    assert output.stat().st_mode & 0o777 == 0o600


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
    assert config.to_dict()["control_plane"][
        "scim_tenants_secret_arn"
    ] == _SCIM_SECRET_ARN

    wrong_region = deepcopy(value)
    wrong_region["control_plane"]["scim_tenants_secret_arn"] = (
        _SCIM_SECRET_ARN.replace("us-east-1", "us-west-2")
    )
    with pytest.raises(
        AgentCoreSetupError,
        match="complete Secrets Manager ARN in us-east-1",
    ):
        AgentCoreSetupConfig.from_mapping(wrong_region)

    legacy_direct_sp = deepcopy(value)
    legacy_direct_sp["control_plane"]["saml_config_secret_arn"] = (
        _SCIM_SECRET_ARN
    )
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
    wrong_region["control_plane"]["certificate_arn"] = (
        _CERTIFICATE_ARN.replace("us-east-1", "us-west-2")
    )
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
    assert config.runtime.athena_query.role_arns == (
        _ATHENA_ROLE_ARN,
    )
    assert config.to_dict()["runtime"]["athena_query"] == (
        value["runtime"]["athena_query"]
    )


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
    assert (
        "AxonLLMIdentityStack:ControlPlaneDomainName=axon.example.com"
        in identity_command
    )
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
    )
    joined = "\n".join(runtime_command)
    assert "AxonLLMAgentCoreStack:OidcIssuer=" in joined
    assert "AxonLLMAgentCoreStack:OidcTenantClaim=" in joined
    assert "AxonLLMAgentCoreStack:OidcProjectClaim=" in joined
    assert "AxonLLMAgentCoreStack:BedrockInvokeResourceArns=" in joined
    assert "broadening" in runtime_command
    assert "client_secret" not in joined.casefold()


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
    assert (
        "AxonLLMControlPlaneStack:AgentCoreStackName="
        "AxonLLMAgentCoreStack"
    ) in command
    assert (
        "AxonLLMControlPlaneStack:IdentityStackName="
        "AxonLLMIdentityStack"
    ) in command
    assert (
        "AxonLLMControlPlaneStack:PrimaryStateTableName="
        "axonllm-agentcore-state"
    ) in command
    assert (
        "AxonLLMControlPlaneStack:ControlPlaneVerifiedImageUri="
        f"{_CONTROL_IMAGE}"
    ) in command
    assert (
        "AxonLLMControlPlaneStack:CertificateArn="
        f"{_CERTIFICATE_ARN}"
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
        runtime_state_table_name=(
            "axonllm-agentcore-state-restore-validation-reviewed"
        ),
        recovery_approval_id="CHG-2026-015",
    )

    assert (
        "scim_tenants_secret_arn=" + _SCIM_SECRET_ARN
    ) in command
    assert (
        "AxonLLMControlPlaneStack:SamlLoginPath=/chat"
    ) in command
    assert not any(
        "saml_config_secret_arn" in argument
        for argument in command
    )
    assert (
        "AxonLLMControlPlaneStack:RuntimeStateTableName="
        "axonllm-agentcore-state-restore-validation-reviewed"
    ) in command
    assert (
        "AxonLLMControlPlaneStack:RecoveryCutoverMode=normal"
    ) in command
    assert (
        "AxonLLMControlPlaneStack:RecoveryApprovalId=CHG-2026-015"
    ) in command


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
    contexts = deployment._athena_contexts(
        AgentCoreSetupConfig.from_mapping(value)
    )
    assert len(contexts["athena_query_bindings"]) == 2_048

    value["runtime"]["athena_query"]["role_arns"] = (
        _athena_roles_for_bindings_length(2_049)
    )
    with pytest.raises(
        AgentCoreDeploymentError,
        match="2,048-character",
    ):
        deployment._athena_contexts(
            AgentCoreSetupConfig.from_mapping(value)
        )


class _AwsError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


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


def test_managed_deploy_provisions_identity_runtime_and_control_plane(
    monkeypatch,
    tmp_path,
    capsys,
):
    config = AgentCoreSetupConfig.from_mapping(_base())
    cognito = _Cognito()
    commands: list[list[str]] = []
    bootstrap_calls: list[dict] = []

    class _Session:
        def client(self, service_name, *, region_name):
            assert service_name == "cognito-idp"
            assert region_name == "us-east-1"
            return cognito

    outputs = {
        deployment.IDENTITY_STACK: {
            "TenantClaimName": "custom:tenant_id",
            "ProjectClaimName": "custom:project_id",
            "OidcClientId": "public-client",
            "OidcAudience": "public-client",
            "OidcIssuer": (
                "https://cognito-idp.us-east-1.amazonaws.com/"
                "us-east-1_POOL"
            ),
            "OidcDiscoveryUrl": (
                "https://cognito-idp.us-east-1.amazonaws.com/"
                "us-east-1_POOL/.well-known/openid-configuration"
            ),
            "HostedUiDomain": (
                "https://axonllm-123456789012.auth."
                "us-east-1.amazoncognito.com"
            ),
            "UserPoolId": "us-east-1_POOL",
        },
        deployment.AGENTCORE_STACK: {
            "StateTableName": "axonllm-agentcore-state",
            "SelectedRuntimeStateTableName": (
                "axonllm-agentcore-state"
            ),
            "RecoveryCutoverMode": "normal",
            "RecoveryApprovalId": "",
            "RuntimeExecutionRoleArn": (
                "arn:aws:iam::123456789012:"
                "role/axonllm-agentcore-runtime-us-east-1"
            ),
            "RuntimeArn": (
                "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
                "runtime/axonllm"
            ),
        },
        deployment.CONTROL_PLANE_STACK: {
            "AgentCoreStackName": deployment.AGENTCORE_STACK,
            "LoadBalancerDnsName": "internal.example.elb.amazonaws.com",
            "PrimaryStateTableName": "axonllm-agentcore-state",
            "SelectedRuntimeStateTableName": (
                "axonllm-agentcore-state"
            ),
            "RecoveryCutoverMode": "normal",
            "RecoveryApprovalId": "",
        },
    }

    def runner(command: list[str], cwd: Path) -> None:
        assert cwd == deployment.INFRA_ROOT
        commands.append(command)
        stack_name = command[3]
        output_path = Path(
            command[command.index("--outputs-file") + 1]
        )
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

    deployment.deploy(
        config,
        outputs_dir=tmp_path / "outputs",
        assume_yes=True,
        bootstrap_cdk=False,
        runner=runner,
        boto3_session=_Session(),
    )

    assert [command[3] for command in commands] == [
        deployment.IDENTITY_STACK,
        deployment.AGENTCORE_STACK,
        deployment.CONTROL_PLANE_STACK,
    ]
    assert bootstrap_calls == [
        {
            "table_name": "axonllm-agentcore-state",
            "issuer": outputs[deployment.IDENTITY_STACK][
                "OidcIssuer"
            ],
            "subject": "cognito-subject",
        }
    ]
    output = capsys.readouterr().out
    assert "Control plane: https://axon.example.com" in output
    assert "Runtime execution role:" in output
    assert "Runtime ARN:" in output


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
    assert "deploy_agentcore.py" in script
