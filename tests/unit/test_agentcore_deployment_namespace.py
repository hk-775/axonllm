"""Isolation contracts for namespaced AgentCore qualification stacks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from src.gateway.deployment.agentcore_deploy import (
    AgentCoreDeploymentError,
    IdentityValues,
    agentcore_deploy_command,
    control_plane_deploy_command,
    deployment_control_plane_domain,
    deployment_names,
    identity_deploy_command,
    validate_rehearsal_control_table_arn,
)
from src.gateway.agentcore_setup import AgentCoreSetupConfig


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "infra"
_INFRA_PYTHON = _INFRA / ".venv" / "bin" / "python"
_DIGEST = "a" * 64
_REHEARSAL_CONTROL_TABLE_ARN = "arn:aws:dynamodb:us-east-1:123456789012:table/axonllm-rehearsal-control-ledger"
_CANDIDATE_ENDPOINT_NAME = "candidate_" + "b" * 32


def _managed_config() -> AgentCoreSetupConfig:
    return AgentCoreSetupConfig.from_mapping(
        {
            "schema_version": 2,
            "target": "agentcore",
            "identity_mode": "managed-cognito",
            "aws_region": "us-east-1",
            "tenant": {
                "tenant_id": "tenant-a",
                "project_id": "project-a",
                "project_name": "Qualification",
            },
            "admin": {
                "user_name": "admin@example.com",
                "email": "admin@example.com",
            },
            "runtime": {
                "verified_image_uri": (
                    f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/agentcore@sha256:{_DIGEST}"
                ),
                "bedrock_invoke_resource_arns": [
                    "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0"
                ],
                "approved_https_prefix_list_id": "pl-123abc",
            },
            "managed_cognito": {
                "hosted_ui_domain_prefix": "axonllm-123456789012",
            },
            "control_plane": {
                "domain_name": "axon.example.com",
                "verified_image_uri": (
                    f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/control-plane@sha256:{_DIGEST}"
                ),
                "certificate_arn": (
                    "arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555"
                ),
                "public_hosted_zone_id": "Z123ABC",
                "approved_ingress_prefix_list_id": "pl-123abc",
                "approved_https_prefix_list_id": "pl-456def",
            },
        }
    )


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [resource for resource in template["Resources"].values() if resource["Type"] == resource_type]


def _values_for_key(value: object, wanted: str) -> list[object]:
    if isinstance(value, dict):
        found = [child for key, child in value.items() if key == wanted]
        for child in value.values():
            found.extend(_values_for_key(child, wanted))
        return found
    if isinstance(value, list):
        found: list[object] = []
        for child in value:
            found.extend(_values_for_key(child, wanted))
        return found
    return []


def _synth(
    tmp_path: Path,
    *,
    target: str,
    namespace: str,
) -> dict:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    out_dir = tmp_path / target
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(
                {
                    "account": "123456789012",
                    "deployment_namespace": namespace,
                    "deployment_target": target,
                    "region": "us-east-1",
                }
            ),
            "CDK_OUTDIR": str(out_dir),
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(tmp_path / "jsii-cache"),
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
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
    base = {
        "agentcore": "AxonLLMAgentCoreStack",
        "control-plane": "AxonLLMControlPlaneStack",
        "identity": "AxonLLMIdentityStack",
    }[target]
    suffix = f"-{namespace}" if namespace else ""
    return json.loads((out_dir / f"{base}{suffix}.template.json").read_text(encoding="utf-8"))


def _policy_statements(template: dict) -> list[dict]:
    statements: list[dict] = []
    for value in _values_for_key(template, "Statement"):
        if isinstance(value, dict):
            statements.append(value)
        elif isinstance(value, list):
            statements.extend(item for item in value if isinstance(item, dict))
    return statements


def _actions(statement: dict) -> set[str]:
    actions = statement["Action"]
    return {actions} if isinstance(actions, str) else set(actions)


def _ledger_statements(template: dict) -> list[dict]:
    expected = {"Ref": "RehearsalControlTableArn"}
    matches = []
    for statement in _policy_statements(template):
        resources = statement.get("Resource")
        if resources == expected or (isinstance(resources, list) and expected in resources):
            matches.append(statement)
    return matches


def _assert_fully_destroyable(template: dict) -> None:
    retained = {
        logical_id
        for logical_id, resource in template["Resources"].items()
        if resource.get("DeletionPolicy") == "Retain" or resource.get("UpdateReplacePolicy") == "Retain"
    }
    assert retained == set()


def _assert_role_trust_domain(
    template: dict,
    *,
    qualifier: str,
) -> None:
    roles = _resources(template, "AWS::IAM::Role")
    boundary_name = f"AxonLLMAgentCoreServiceBoundary-{qualifier}-us-east-1"
    for logical_id, role in (
        (logical_id, resource)
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::IAM::Role"
    ):
        properties = role["Properties"]
        assert boundary_name in json.dumps(
            properties["PermissionsBoundary"],
            sort_keys=True,
        )
        if "Tags" not in properties:
            assert logical_id.startswith(
                (
                    "CustomS3AutoDeleteObjectsCustomResourceProviderRole",
                    "CustomVpcRestrictDefaultSGCustomResourceProviderRole",
                )
            )
            continue
        tags = {tag["Key"]: tag["Value"] for tag in properties["Tags"]}
        assert tags["Application"] == "AxonLLM"
        assert tags["AxonLLMTrustDomain"] == qualifier


def test_default_and_qualification_names_do_not_collide() -> None:
    production = deployment_names()
    external = deployment_names("external-oidc")
    managed = deployment_names("managed")

    assert production.identity_stack == "AxonLLMIdentityStack"
    assert production.agentcore_stack == "AxonLLMAgentCoreStack"
    assert production.control_plane_stack == "AxonLLMControlPlaneStack"
    assert production.state_table == "axonllm-agentcore-state"
    assert production.user_pool == "axonllm-agentcore-users"
    assert (
        len(
            {
                production.agentcore_stack,
                external.agentcore_stack,
                managed.agentcore_stack,
            }
        )
        == 3
    )
    assert external.agentcore_stack.endswith("-external-oidc")
    assert managed.control_plane_stack.endswith("-managed")


@pytest.mark.parametrize(
    "namespace",
    [
        "A",
        "ends-",
        "-starts",
        "has_underscore",
        "a" * 17,
        "two..dots",
    ],
)
def test_namespace_validation_rejects_ambiguous_values(
    namespace: str,
) -> None:
    with pytest.raises(
        AgentCoreDeploymentError,
        match="deployment namespace must be",
    ):
        deployment_names(namespace)


def test_namespaced_deployment_requires_rehearsal_control_table() -> None:
    with pytest.raises(
        AgentCoreDeploymentError,
        match="required with --deployment-namespace",
    ):
        validate_rehearsal_control_table_arn(
            aws_region="us-east-1",
            deployment_namespace="managed",
            rehearsal_control_table_arn=None,
        )


def test_production_forbids_rehearsal_control_table() -> None:
    with pytest.raises(
        AgentCoreDeploymentError,
        match="forbidden for the production/default",
    ):
        validate_rehearsal_control_table_arn(
            aws_region="us-east-1",
            deployment_namespace=None,
            rehearsal_control_table_arn=(_REHEARSAL_CONTROL_TABLE_ARN),
        )


@pytest.mark.parametrize(
    "value",
    [
        ("arn:aws:dynamodb:us-west-2:123456789012:table/axonllm-rehearsal-control-ledger"),
        ("arn:aws:dynamodb:us-east-1:12345678901:table/axonllm-rehearsal-control-ledger"),
        ("arn:aws:dynamodb:us-east-1:123456789012:table/axonllm-rehearsal-control-ledger-*"),
        ("arn:aws:dynamodb:us-east-1:123456789012:table/other"),
        ("arn:aws-us-gov:dynamodb:us-east-1:123456789012:table/axonllm-rehearsal-control-ledger"),
    ],
)
def test_rehearsal_control_table_rejects_mismatched_arn(
    value: str,
) -> None:
    with pytest.raises(
        AgentCoreDeploymentError,
        match="must be the exact arn:aws:dynamodb ARN",
    ):
        validate_rehearsal_control_table_arn(
            aws_region="us-east-1",
            deployment_namespace="managed",
            rehearsal_control_table_arn=value,
        )


def test_namespaced_runtime_commands_receive_exact_rehearsal_parameter(
    tmp_path: Path,
) -> None:
    config = _managed_config()
    identity = IdentityValues(
        issuer="https://issuer.example.com",
        discovery_url=("https://issuer.example.com/.well-known/openid-configuration"),
        client_id="client-id",
        audience="api://axonllm",
        tenant_claim="tenant",
        project_claim="project",
    )
    commands = (
        (
            agentcore_deploy_command(
                config,
                identity,
                outputs_file=tmp_path / "agentcore.json",
                assume_yes=True,
                candidate_endpoint_name=(_CANDIDATE_ENDPOINT_NAME),
                deployment_namespace="managed",
                rehearsal_control_table_arn=(_REHEARSAL_CONTROL_TABLE_ARN),
            ),
            "AxonLLMAgentCoreStack-managed",
        ),
        (
            control_plane_deploy_command(
                config,
                primary_state_table_name=("axonllm-agentcore-state-managed"),
                outputs_file=tmp_path / "control-plane.json",
                assume_yes=True,
                deployment_namespace="managed",
                rehearsal_control_table_arn=(_REHEARSAL_CONTROL_TABLE_ARN),
            ),
            "AxonLLMControlPlaneStack-managed",
        ),
    )

    for command, stack_name in commands:
        assert (f"{stack_name}:RehearsalControlTableArn={_REHEARSAL_CONTROL_TABLE_ARN}") in command

    identity_command = identity_deploy_command(
        config,
        outputs_file=tmp_path / "identity.json",
        assume_yes=True,
        deployment_namespace="managed",
    )
    assert not any("RehearsalControlTableArn" in argument for argument in identity_command)
    assert ("AxonLLMIdentityStack-managed:ControlPlaneDomainName=managed.axon.example.com") in identity_command
    assert not any("OAuthCallbackUrls" in argument for argument in identity_command)
    control_command = commands[-1][0]
    assert ("AxonLLMControlPlaneStack-managed:IdentityStackName=AxonLLMIdentityStack-managed") in control_command


def test_production_cdk_commands_remain_without_rehearsal_parameter(
    tmp_path: Path,
) -> None:
    config = _managed_config()
    identity = IdentityValues(
        issuer="https://issuer.example.com",
        discovery_url=("https://issuer.example.com/.well-known/openid-configuration"),
        client_id="client-id",
        audience="api://axonllm",
        tenant_claim="tenant",
        project_claim="project",
    )
    commands = (
        identity_deploy_command(
            config,
            outputs_file=tmp_path / "identity.json",
            assume_yes=True,
        ),
        agentcore_deploy_command(
            config,
            identity,
            outputs_file=tmp_path / "agentcore.json",
            assume_yes=True,
            candidate_endpoint_name=_CANDIDATE_ENDPOINT_NAME,
        ),
        control_plane_deploy_command(
            config,
            primary_state_table_name="axonllm-agentcore-state",
            outputs_file=tmp_path / "control-plane.json",
            assume_yes=True,
        ),
    )

    assert all(not any("RehearsalControlTableArn" in argument for argument in command) for command in commands)
    assert ("AxonLLMIdentityStack:ControlPlaneDomainName=axon.example.com") in commands[0]
    assert not any("OAuthCallbackUrls" in argument for argument in commands[0])


def test_namespaced_control_plane_domain_enforces_dns_bounds() -> None:
    assert (
        deployment_control_plane_domain(
            "axon.example.com",
            "managed",
        )
        == "managed.axon.example.com"
    )
    assert (
        deployment_control_plane_domain(
            "axon.example.com",
            None,
        )
        == "axon.example.com"
    )

    maximum_domain = f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 57}.com"
    assert len(maximum_domain) == 253
    with pytest.raises(
        AgentCoreDeploymentError,
        match="at most 253 characters",
    ):
        deployment_control_plane_domain(
            maximum_domain,
            "managed",
        )


def test_namespaced_agentcore_physical_names_are_isolated(tmp_path) -> None:
    template = _synth(
        tmp_path,
        target="agentcore",
        namespace="external-oidc",
    )

    runtime = _resources(
        template,
        "AWS::BedrockAgentCore::Runtime",
    )[0]["Properties"]
    table = _resources(template, "AWS::DynamoDB::Table")[0]["Properties"]
    dashboard = _resources(
        template,
        "AWS::CloudWatch::Dashboard",
    )[0]["Properties"]
    guard = [
        resource["Properties"]
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::CloudFormation::CustomResource" and "RuntimeName" in resource["Properties"]
    ][0]
    role_names = [
        resource["Properties"]["RoleName"]
        for resource in _resources(template, "AWS::IAM::Role")
        if "RoleName" in resource["Properties"]
    ]

    assert runtime["AgentRuntimeName"] == "axonllm_external_oidc"
    assert table["TableName"] == "axonllm-agentcore-state-external-oidc"
    assert dashboard["DashboardName"] == "AxonLLM-AgentCore-Production-external-oidc"
    assert guard["ControlPlaneStackName"] == "AxonLLMControlPlaneStack-external-oidc"
    assert guard["RuntimeName"] == "axonllm_external_oidc"
    assert any("external-oidc" in json.dumps(role_name) for role_name in role_names)
    _assert_fully_destroyable(template)
    assert table["DeletionProtectionEnabled"] is False
    assert not any(resource["Type"].startswith("AWS::Backup::") for resource in template["Resources"].values())
    assert _resources(template, "AWS::KMS::Key") == []
    assert _resources(template, "AWS::KMS::Alias") == []
    assert _resources(template, "AWS::IAM::Role")
    _assert_role_trust_domain(template, qualifier="axext")


def test_namespaced_identity_and_control_plane_are_isolated(
    tmp_path,
) -> None:
    identity = _synth(
        tmp_path,
        target="identity",
        namespace="managed",
    )
    control = _synth(
        tmp_path,
        target="control-plane",
        namespace="managed",
    )

    pool = _resources(identity, "AWS::Cognito::UserPool")[0]["Properties"]
    client_names = {
        resource["Properties"]["ClientName"]
        for resource in _resources(
            identity,
            "AWS::Cognito::UserPoolClient",
        )
    }
    task = _resources(control, "AWS::ECS::TaskDefinition")[0]["Properties"]
    guard = [
        resource["Properties"]
        for resource in control["Resources"].values()
        if resource["Type"] == "AWS::CloudFormation::CustomResource"
        and "ControlPlaneStackName" in resource["Properties"]
    ][0]

    assert pool["UserPoolName"] == "axonllm-agentcore-users-managed"
    assert client_names == {
        "axonllm-agentcore-audience-managed",
        "axonllm-agentcore-certification-managed",
        "axonllm-control-plane-alb-managed",
    }
    assert task["Family"] == "axonllm-control-plane-managed"
    assert control["Parameters"]["AgentCoreStackName"]["Default"] == ("AxonLLMAgentCoreStack-managed")
    assert control["Parameters"]["IdentityStackName"]["Default"] == ("AxonLLMIdentityStack-managed")
    assert guard["ControlPlaneStackName"] == "AxonLLMControlPlaneStack-managed"
    assert "AxonLLMControlPlaneSession-managed" in _values_for_key(
        control,
        "SessionCookieName",
    )
    _assert_fully_destroyable(identity)
    _assert_fully_destroyable(control)
    _assert_role_trust_domain(identity, qualifier="axqual")
    _assert_role_trust_domain(control, qualifier="axqual")
    assert pool["DeletionProtection"] == "INACTIVE"
    load_balancer = _resources(
        control,
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
    )[0]["Properties"]
    attributes = {item["Key"]: item["Value"] for item in load_balancer["LoadBalancerAttributes"]}
    assert attributes["deletion_protection.enabled"] == "false"
    assert len(_resources(control, "Custom::S3AutoDeleteObjects")) == 1
    outputs = control["Outputs"]
    cluster_logical_id = next(
        logical_id for logical_id, resource in control["Resources"].items() if resource["Type"] == "AWS::ECS::Cluster"
    )
    task_security_group_logical_id = next(
        logical_id
        for logical_id, resource in control["Resources"].items()
        if resource["Type"] == "AWS::EC2::SecurityGroup"
        and resource["Properties"]["GroupDescription"] == "AxonLLM control-plane tasks"
    )
    assert outputs["ClusterArn"]["Value"] == {"Fn::GetAtt": [cluster_logical_id, "Arn"]}
    assert outputs["TaskSecurityGroupId"]["Value"] == {"Fn::GetAtt": [task_security_group_logical_id, "GroupId"]}
    subnet_join = outputs["SubnetIds"]["Value"]["Fn::Join"]
    assert subnet_join[0] == ","
    assert len(subnet_join[1]) == 2
    assert all(isinstance(item, dict) and set(item) == {"Ref"} for item in subnet_join[1])
    certification = next(
        resource["Properties"]
        for resource in _resources(
            identity,
            "AWS::Cognito::UserPoolClient",
        )
        if resource["Properties"]["ClientName"] == "axonllm-agentcore-certification-managed"
    )
    assert certification["AccessTokenValidity"] == 360
    assert certification["IdTokenValidity"] == 360
    assert certification["RefreshTokenValidity"] == 360
    assert certification["TokenValidityUnits"] == {
        "AccessToken": "minutes",
        "IdToken": "minutes",
        "RefreshToken": "minutes",
    }
    for client_name in (
        "axonllm-agentcore-audience-managed",
        "axonllm-control-plane-alb-managed",
    ):
        client = next(
            resource["Properties"]
            for resource in _resources(
                identity,
                "AWS::Cognito::UserPoolClient",
            )
            if resource["Properties"]["ClientName"] == client_name
        )
        assert client["AccessTokenValidity"] == 15
        assert client["IdTokenValidity"] == 15
        assert client["TokenValidityUnits"]["AccessToken"] == "minutes"
        assert client["TokenValidityUnits"]["IdToken"] == "minutes"


def test_namespaced_agentcore_wires_exact_rehearsal_ledger(
    tmp_path,
) -> None:
    template = _synth(
        tmp_path,
        target="agentcore",
        namespace="managed",
    )
    parameter = template["Parameters"]["RehearsalControlTableArn"]
    runtime = _resources(
        template,
        "AWS::BedrockAgentCore::Runtime",
    )[0]["Properties"]

    assert "Default" not in parameter
    assert parameter["AllowedPattern"] == (
        r"^arn:aws:dynamodb:us\-east\-1:123456789012:"
        r"table/axonllm-rehearsal-control-ledger$"
    )
    assert runtime["EnvironmentVariables"]["AXON_LAUNCH_REHEARSAL_TABLE"] == {"Ref": "RehearsalControlTableArn"}
    assert runtime["EnvironmentVariables"]["AXON_LAUNCH_REHEARSAL_ALLOW_PROCESS_EXIT"] == "true"
    statements = _ledger_statements(template)
    assert len(statements) == 2
    assert all(_actions(statement) == {"dynamodb:GetItem", "dynamodb:PutItem"} for statement in statements)


def test_namespaced_control_plane_wires_ledger_without_process_exit(
    tmp_path,
) -> None:
    template = _synth(
        tmp_path,
        target="control-plane",
        namespace="managed",
    )
    parameter = template["Parameters"]["RehearsalControlTableArn"]
    task = _resources(template, "AWS::ECS::TaskDefinition")[0]["Properties"]
    environment = {item["Name"]: item["Value"] for item in task["ContainerDefinitions"][0]["Environment"]}

    assert "Default" not in parameter
    assert parameter["AllowedPattern"] == (
        r"^arn:aws:dynamodb:us\-east\-1:123456789012:"
        r"table/axonllm-rehearsal-control-ledger$"
    )
    assert environment["AXON_LAUNCH_REHEARSAL_TABLE"] == {"Ref": "RehearsalControlTableArn"}
    assert "AXON_LAUNCH_REHEARSAL_ALLOW_PROCESS_EXIT" not in environment
    statements = _ledger_statements(template)
    assert len(statements) == 3
    runtime_actions = {"dynamodb:GetItem", "dynamodb:PutItem"}
    assert sum(_actions(statement) == runtime_actions for statement in statements) == 2
    task_role_statement = next(
        statement for statement in statements if statement.get("Sid") == "UseLaunchRehearsalControlLedger"
    )
    assert _actions(task_role_statement) == runtime_actions
    launch_worker_actions = {
        "dynamodb:BatchWriteItem",
        "dynamodb:DeleteItem",
        "dynamodb:DeleteTable",
        "dynamodb:DescribeContinuousBackups",
        "dynamodb:DescribeTable",
        "dynamodb:DescribeTimeToLive",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:Query",
        "dynamodb:RestoreTableToPointInTime",
        "dynamodb:Scan",
        "dynamodb:UpdateContinuousBackups",
        "dynamodb:UpdateItem",
        "dynamodb:UpdateTable",
        "dynamodb:UpdateTimeToLive",
    }
    assert any(_actions(statement) == launch_worker_actions for statement in statements)


@pytest.mark.parametrize("target", ["agentcore", "control-plane"])
def test_default_templates_have_no_rehearsal_wiring(
    tmp_path,
    target: str,
) -> None:
    template = _synth(
        tmp_path,
        target=target,
        namespace="",
    )
    serialized = json.dumps(template, sort_keys=True)

    assert "RehearsalControlTableArn" not in template["Parameters"]
    assert "AXON_LAUNCH_REHEARSAL_TABLE" not in serialized
    assert "AXON_LAUNCH_REHEARSAL_ALLOW_PROCESS_EXIT" not in serialized
    assert "axonllm-rehearsal-control-ledger" not in serialized
    assert _ledger_statements(template) == []
    _assert_role_trust_domain(template, qualifier="axprod")
    retained = {
        logical_id
        for logical_id, resource in template["Resources"].items()
        if resource.get("DeletionPolicy") == "Retain" and resource.get("UpdateReplacePolicy") == "Retain"
    }
    assert len(retained) == (10 if target == "agentcore" else 5)
    if target == "agentcore":
        table = _resources(
            template,
            "AWS::DynamoDB::Table",
        )[0]["Properties"]
        assert table["DeletionProtectionEnabled"] is True
        assert not any(resource["Type"].startswith("AWS::Backup::") for resource in template["Resources"].values())
        assert _resources(template, "AWS::KMS::Key") == []
        assert _resources(template, "AWS::KMS::Alias") == []
    else:
        load_balancer = _resources(
            template,
            "AWS::ElasticLoadBalancingV2::LoadBalancer",
        )[0]["Properties"]
        attributes = {item["Key"]: item["Value"] for item in load_balancer["LoadBalancerAttributes"]}
        assert attributes["deletion_protection.enabled"] == "true"
