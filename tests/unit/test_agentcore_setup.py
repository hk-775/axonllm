"""First-adopter AgentCore setup, deployment, and invitation contracts."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts.operations.deploy_agentcore import (
    AgentCoreDeploymentError,
    IdentityValues,
    agentcore_deploy_command,
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
_BEDROCK_ARN = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0"


def _base() -> dict:
    return {
        "schema_version": 1,
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
    }


def _external() -> dict:
    value = _base()
    value["identity_mode"] = "external-oidc"
    value.pop("managed_cognito")
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


def test_managed_setup_round_trips_without_a_secret_or_subject(tmp_path):
    config = AgentCoreSetupConfig.from_mapping(_base())
    output = write_agentcore_setup(config, tmp_path / "agentcore.json")

    loaded = load_agentcore_setup(output)
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert loaded == config
    assert payload["identity_mode"] == "managed-cognito"
    assert "subject" not in payload["admin"]
    assert "secret" not in output.read_text(encoding="utf-8").casefold()
    assert output.stat().st_mode & 0o777 == 0o600


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


def test_setup_rejects_boolean_schema_versions_and_local_identity_urls():
    boolean_schema = _base()
    boolean_schema["schema_version"] = True
    with pytest.raises(AgentCoreSetupError, match="schema_version must be 1"):
        AgentCoreSetupConfig.from_mapping(boolean_schema)

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
            "--output",
            str(output),
        ],
    )

    cli.main()

    assert load_agentcore_setup(output).identity_mode == "managed-cognito"
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
