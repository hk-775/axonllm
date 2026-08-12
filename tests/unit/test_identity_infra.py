"""Synthesized security contract for the retained Cognito identity stack."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "infra"
_INFRA_PYTHON = _INFRA / ".venv" / "bin" / "python"


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [resource for resource in template["Resources"].values() if resource["Type"] == resource_type]


def _one_resource(template: dict, resource_type: str) -> dict:
    resources = _resources(template, resource_type)
    assert len(resources) == 1
    return resources[0]


def _client(template: dict, client_name: str) -> dict:
    clients = _resources(template, "AWS::Cognito::UserPoolClient")
    matches = [
        client
        for client in clients
        if client["Properties"]["ClientName"] == client_name
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.fixture(scope="module")
def identity_template(tmp_path_factory: pytest.TempPathFactory) -> dict:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")

    work_dir = tmp_path_factory.mktemp("identity-infra")
    out_dir = work_dir / "cdk.out"
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(
                {
                    "deployment_target": "identity",
                    "region": "us-east-1",
                }
            ),
            "CDK_OUTDIR": str(out_dir),
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(work_dir / "jsii-cache"),
            "PYTHONPYCACHEPREFIX": str(work_dir / "pycache"),
        }
    )
    completed = subprocess.run(
        [str(_INFRA_PYTHON), "app.py"],
        cwd=_INFRA,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout
    return json.loads((out_dir / "AxonLLMIdentityStack.template.json").read_text(encoding="utf-8"))


def test_identity_inputs_are_explicit_https_values(identity_template):
    parameters = identity_template["Parameters"]
    assert "Default" not in parameters["HostedUiDomainPrefix"]
    assert "Default" not in parameters["OAuthCallbackUrls"]
    assert "Default" not in parameters["ControlPlaneDomainName"]
    assert "Default" not in parameters["SesFromEmail"]
    assert "Default" not in parameters["SesVerifiedDomain"]
    assert parameters["HostedUiDomainPrefix"]["MinLength"] == 3
    assert parameters["HostedUiDomainPrefix"]["MaxLength"] == 63
    assert parameters["OAuthCallbackUrls"]["Type"] == "CommaDelimitedList"
    assert parameters["OAuthCallbackUrls"]["AllowedPattern"].startswith("^https://")
    assert parameters["ControlPlaneDomainName"]["AllowedPattern"].endswith(
        r"[a-z]{2,63}$"
    )
    assert parameters["SesFromEmail"]["AllowedPattern"].startswith(
        r"^[^@\s]+@"
    )
    assert parameters["SesVerifiedDomain"]["AllowedPattern"].endswith(
        r"[a-z]{2,63}$"
    )


def test_user_pool_is_retained_and_operator_enrolled(identity_template):
    pool = _one_resource(
        identity_template,
        "AWS::Cognito::UserPool",
    )
    properties = pool["Properties"]
    assert pool["DeletionPolicy"] == "Retain"
    assert pool["UpdateReplacePolicy"] == "Retain"
    assert properties["DeletionProtection"] == "ACTIVE"
    assert properties["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    assert properties["UsernameAttributes"] == ["email"]
    assert properties["UsernameConfiguration"]["CaseSensitive"] is False
    assert properties["AccountRecoverySetting"]["RecoveryMechanisms"] == [{"Name": "verified_email", "Priority": 1}]
    email = properties["EmailConfiguration"]
    assert email["EmailSendingAccount"] == "DEVELOPER"
    assert email["From"]["Fn::Join"][1] == [
        "AxonLLM <",
        {"Ref": "SesFromEmail"},
        ">",
    ]
    source_parts = email["SourceArn"]["Fn::Join"][1]
    assert source_parts[-2:] == [
        ":identity/",
        {"Ref": "SesVerifiedDomain"},
    ]
    assert {"Ref": "AWS::AccountId"} in source_parts


def test_password_and_totp_policy_fail_closed(identity_template):
    properties = _one_resource(
        identity_template,
        "AWS::Cognito::UserPool",
    )["Properties"]
    password = properties["Policies"]["PasswordPolicy"]
    assert password == {
        "MinimumLength": 14,
        "RequireLowercase": True,
        "RequireNumbers": True,
        "RequireSymbols": True,
        "RequireUppercase": True,
        "TemporaryPasswordValidityDays": 7,
    }
    assert properties["MfaConfiguration"] == "ON"
    assert properties["EnabledMfas"] == ["SOFTWARE_TOKEN_MFA"]


def test_tenant_and_project_are_operator_controlled_claims(identity_template):
    properties = _one_resource(
        identity_template,
        "AWS::Cognito::UserPool",
    )["Properties"]
    custom_schema = {item["Name"]: item for item in properties["Schema"] if item["Name"] in {"tenant_id", "project_id"}}
    assert set(custom_schema) == {"tenant_id", "project_id"}
    assert all(item["Mutable"] is True for item in custom_schema.values())
    assert all(
        item["StringAttributeConstraints"]
        == {
            "MaxLength": "128",
            "MinLength": "1",
        }
        for item in custom_schema.values()
    )

    client = _client(
        identity_template,
        "axonllm-agentcore-pkce",
    )["Properties"]
    assert {"custom:tenant_id", "custom:project_id"} <= set(client["ReadAttributes"])
    assert "custom:tenant_id" not in client["WriteAttributes"]
    assert "custom:project_id" not in client["WriteAttributes"]


def test_public_client_is_code_pkce_shaped_without_implicit_flow(
    identity_template,
):
    client_resource = _client(
        identity_template,
        "axonllm-agentcore-pkce",
    )
    client = client_resource["Properties"]
    assert client_resource["DeletionPolicy"] == "Retain"
    assert client_resource["UpdateReplacePolicy"] == "Retain"
    assert client["GenerateSecret"] is False
    assert client["AllowedOAuthFlows"] == ["code"]
    assert "ExplicitAuthFlows" not in client
    assert client["AllowedOAuthScopes"] == ["openid", "email", "profile"]
    assert client["CallbackURLs"] == {"Ref": "OAuthCallbackUrls"}
    assert client["PreventUserExistenceErrors"] == "ENABLED"
    assert client["EnableTokenRevocation"] is True
    assert client["RefreshTokenRotation"] == {
        "Feature": "ENABLED",
        "RetryGracePeriodSeconds": 0,
    }
    assert client["IdTokenValidity"] == 15
    assert client["AccessTokenValidity"] == 15


def test_confidential_alb_client_has_its_own_exact_callback(
    identity_template,
):
    client_resource = _client(
        identity_template,
        "axonllm-control-plane-alb",
    )
    client = client_resource["Properties"]
    assert client_resource["DeletionPolicy"] == "Retain"
    assert client_resource["UpdateReplacePolicy"] == "Retain"
    assert client["GenerateSecret"] is True
    assert client["AllowedOAuthFlows"] == ["code"]
    assert client["AllowedOAuthScopes"] == ["openid", "email", "profile"]
    assert client["CallbackURLs"] == [
        {
            "Fn::Join": [
                "",
                [
                    "https://",
                    {"Ref": "ControlPlaneDomainName"},
                    "/oauth2/idpresponse",
                ],
            ]
        }
    ]
    assert client["PreventUserExistenceErrors"] == "ENABLED"
    assert "ExplicitAuthFlows" not in client


def test_hosted_ui_and_standard_oidc_outputs_are_retained(identity_template):
    domain = _one_resource(
        identity_template,
        "AWS::Cognito::UserPoolDomain",
    )
    assert domain["DeletionPolicy"] == "Retain"
    assert domain["Properties"]["Domain"] == {"Ref": "HostedUiDomainPrefix"}
    outputs = identity_template["Outputs"]
    assert {
        "UserPoolId",
        "OidcIssuer",
        "OidcDiscoveryUrl",
        "OidcClientId",
        "OidcAudience",
        "AlbClientId",
        "ControlPlaneDomainName",
        "HostedUiDomain",
        "HostedUiDomainName",
        "TenantClaimName",
        "ProjectClaimName",
    } <= set(outputs)
    assert outputs["TenantClaimName"]["Value"] == "custom:tenant_id"
    assert outputs["ProjectClaimName"]["Value"] == "custom:project_id"


def test_identity_stack_creates_no_iam_or_lambda_resources(identity_template):
    resource_types = {resource["Type"] for resource in identity_template["Resources"].values()}
    assert not {
        resource_type
        for resource_type in resource_types
        if resource_type.startswith("AWS::IAM::") or resource_type.startswith("AWS::Lambda::")
    }
