"""CDK security contracts for the AgentCore query and ECS control planes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "infra"
_INFRA_PYTHON = _INFRA / ".venv" / "bin" / "python"
_ROLE_ARNS = {
    "arn:aws:iam::123456789012:role/AxonAthenaReader",
    "arn:aws:iam::210987654321:role/data/AxonAthenaReader",
}
_BINDINGS = [
    {
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "role_arn": "arn:aws:iam::123456789012:role/AxonAthenaReader",
    },
    {
        "tenant_id": "tenant-b",
        "project_id": "project-b",
        "role_arn": (
            "arn:aws:iam::210987654321:role/data/AxonAthenaReader"
        ),
    },
]
_ATHENA_ACTIONS = {
    "athena:GetQueryExecution",
    "athena:GetQueryResults",
    "athena:GetWorkGroup",
    "athena:StartQueryExecution",
    "athena:StopQueryExecution",
}
_ASSUME_ROLE_ACTIONS = {
    "sts:AssumeRole",
    "sts:SetSourceIdentity",
    "sts:TagSession",
}
_QUERY_ENVIRONMENT = {
    "AXON_ATHENA_QUERY_ENABLED": "true",
    "AXON_ATHENA_QUERY_BINDINGS": json.dumps(
        _BINDINGS,
        separators=(",", ":"),
        sort_keys=True,
    ),
    "AXON_ATHENA_QUERY_TIMEOUT_SECONDS": "30",
    "AXON_ATHENA_QUERY_MAX_ROWS": "1000",
    "AXON_ATHENA_QUERY_MAX_RESULT_BYTES": "1048576",
    "AXON_ATHENA_QUERY_MAX_BYTES_SCANNED": "1073741824",
    "AXON_ATHENA_QUERY_POLL_INTERVAL_SECONDS": "0.25",
    "AXON_ATHENA_QUERY_PROJECT_RPM": "30",
    "AXON_ATHENA_QUERY_PRINCIPAL_RPM": "10",
    "AXON_ATHENA_QUERY_PROJECT_CONCURRENCY": "5",
    "AXON_ATHENA_QUERY_PRINCIPAL_CONCURRENCY": "2",
    "AXON_ATHENA_QUERY_PROJECT_SCAN_BYTES_PER_MINUTE": "5368709120",
    "AXON_ATHENA_QUERY_PRINCIPAL_SCAN_BYTES_PER_MINUTE": "2147483648",
    "AXON_ATHENA_QUERY_MAX_DATASOURCES_PER_TENANT": "500",
}


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == resource_type
    ]


def _one_resource(template: dict, resource_type: str) -> dict:
    resources = _resources(template, resource_type)
    assert len(resources) == 1
    return resources[0]


def _actions(statement: dict) -> set[str]:
    actions = statement["Action"]
    return {actions} if isinstance(actions, str) else set(actions)


def _as_set(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    assert isinstance(value, list)
    assert all(isinstance(item, str) for item in value)
    return set(value)


def _interface_endpoint(template: dict, service_suffix: str) -> dict:
    matches = [
        endpoint["Properties"]
        for endpoint in _resources(template, "AWS::EC2::VPCEndpoint")
        if str(endpoint["Properties"]["ServiceName"]).endswith(
            service_suffix
        )
    ]
    assert len(matches) == 1
    return matches[0]


def _bindings_with_json_length(target_length: int) -> list[dict[str, str]]:
    bindings = [
        {
            "tenant_id": f"tenant-{index}",
            "project_id": f"project-{index}",
            "role_arn": (
                f"arn:aws:iam::{123456789012 + index}:role/r{index}"
            ),
        }
        for index in range(4)
    ]

    def serialize() -> str:
        return json.dumps(
            bindings,
            separators=(",", ":"),
            sort_keys=True,
        )

    remaining = target_length - len(serialize())
    for binding in bindings:
        role_path_length = len(
            binding["role_arn"].split("role/", maxsplit=1)[1]
        )
        added = min(remaining, 512 - role_path_length)
        binding["role_arn"] += "x" * added
        remaining -= added
    assert remaining == 0
    assert len(serialize()) == target_length
    return bindings


def _run_app(
    *,
    target: str,
    work_dir: Path,
    extra_context: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    out_dir = work_dir / "cdk.out"
    context = {
        "deployment_target": target,
        "region": "us-east-1",
        "athena_query_bindings": _BINDINGS,
    }
    context.update(extra_context or {})
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(context),
            "CDK_OUTDIR": str(out_dir),
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(
                work_dir / "jsii-cache"
            ),
            "PYTHONPYCACHEPREFIX": str(work_dir / "pycache"),
        }
    )
    return subprocess.run(
        [str(_INFRA_PYTHON), "app.py"],
        cwd=_INFRA,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )


def _synth(
    *,
    target: str,
    work_dir: Path,
    extra_context: dict | None = None,
) -> dict:
    out_dir = work_dir / "cdk.out"
    completed = _run_app(
        target=target,
        work_dir=work_dir,
        extra_context=extra_context,
    )
    assert completed.returncode == 0, completed.stdout
    stack_name = {
        "agentcore": "AxonLLMAgentCoreStack",
        "control-plane": "AxonLLMControlPlaneStack",
    }[target]
    return json.loads(
        (out_dir / f"{stack_name}.template.json").read_text(
            encoding="utf-8"
        )
    )


def test_infra_enforces_agentcore_binding_character_boundary(
    tmp_path,
):
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")

    at_limit = _bindings_with_json_length(2_048)
    template = _synth(
        target="agentcore",
        work_dir=tmp_path / "at-limit",
        extra_context={"athena_query_bindings": at_limit},
    )
    runtime = _one_resource(
        template,
        "AWS::BedrockAgentCore::Runtime",
    )["Properties"]
    assert len(
        runtime["EnvironmentVariables"]["AXON_ATHENA_QUERY_BINDINGS"]
    ) == 2_048

    completed = _run_app(
        target="agentcore",
        work_dir=tmp_path / "over-limit",
        extra_context={
            "athena_query_bindings": _bindings_with_json_length(2_049)
        },
    )
    assert completed.returncode != 0
    assert "2,048-character environment value limit" in completed.stdout


@pytest.fixture(scope="module")
def query_templates(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, dict]:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    return {
        target: _synth(
            target=target,
            work_dir=tmp_path_factory.mktemp(
                f"{target}-query-infra"
            ),
        )
        for target in ("agentcore", "control-plane")
    }


def test_agentcore_query_env_iam_and_endpoints_share_one_allowlist(
    query_templates,
):
    template = query_templates["agentcore"]
    runtime = _one_resource(
        template,
        "AWS::BedrockAgentCore::Runtime",
    )["Properties"]
    environment = runtime["EnvironmentVariables"]
    for name, value in _QUERY_ENVIRONMENT.items():
        assert environment[name] == value
    assert "AXON_CONTROL_PLANE_ONLY" not in environment
    assert environment["AWS_STS_REGIONAL_ENDPOINTS"] == "regional"

    endpoints = _resources(template, "AWS::EC2::VPCEndpoint")
    assert len(endpoints) == 7
    athena = _interface_endpoint(template, ".athena")
    sts = _interface_endpoint(template, ".sts")
    for endpoint in (athena, sts):
        assert endpoint["PrivateDnsEnabled"] is True
        assert len(endpoint["SubnetIds"]) == 2
        assert all(
            subnet["Ref"].startswith("VpcRuntimeSubnet")
            for subnet in endpoint["SubnetIds"]
        )

    athena_policy = athena["PolicyDocument"]["Statement"][0]
    assert _actions(athena_policy) == _ATHENA_ACTIONS
    assert _as_set(athena_policy["Principal"]["AWS"]) == _ROLE_ARNS
    sts_policy = sts["PolicyDocument"]["Statement"][0]
    assert _actions(sts_policy) == _ASSUME_ROLE_ACTIONS
    assert _as_set(sts_policy["Resource"]) == _ROLE_ARNS
    assert sts_policy["Principal"]["AWS"]["Fn::GetAtt"][0].startswith(
        "RuntimeExecutionRole"
    )

    runtime_policy = next(
        resource
        for logical_id, resource in template["Resources"].items()
        if logical_id.startswith("RuntimeExecutionRoleDefaultPolicy")
    )
    statements = runtime_policy["Properties"]["PolicyDocument"]["Statement"]
    assume = next(
        statement
        for statement in statements
        if "sts:AssumeRole" in _actions(statement)
    )
    assert _actions(assume) == _ASSUME_ROLE_ACTIONS
    assert _as_set(assume["Resource"]) == _ROLE_ARNS
    assert not any(
        action.startswith("athena:")
        for statement in statements
        for action in _actions(statement)
    )

    runtime_role = next(
        resource
        for resource in _resources(template, "AWS::IAM::Role")
        if resource["Properties"].get("RoleName")
        == "axonllm-agentcore-runtime-us-east-1"
    )
    runtime_role_logical_id = next(
        logical_id
        for logical_id, resource in template["Resources"].items()
        if resource is runtime_role
    )
    assert runtime["RoleArn"] == {
        "Fn::GetAtt": [runtime_role_logical_id, "Arn"]
    }
    assert template["Outputs"]["RuntimeExecutionRoleArn"]["Value"] == {
        "Fn::GetAtt": [runtime_role_logical_id, "Arn"]
    }


def test_agentcore_exports_only_the_existing_canonical_authority(
    query_templates,
):
    template = query_templates["agentcore"]
    outputs = template["Outputs"]
    exported = {
        "StateTableName",
        "DataKeyArn",
        "SecurityEventOutboxQueueArn",
        "SecurityEventOutboxQueueUrl",
        "SecurityEventTopicArn",
        "SecurityEventLogGroupArn",
    }
    assert exported <= set(outputs)
    assert all("Export" in outputs[name] for name in exported)
    assert len(_resources(template, "AWS::DynamoDB::Table")) == 1


def test_control_plane_imports_state_and_never_creates_another_table(
    query_templates,
):
    template = query_templates["control-plane"]
    assert _resources(template, "AWS::DynamoDB::Table") == []
    assert _resources(template, "AWS::KMS::Key") == []

    task = _one_resource(
        template,
        "AWS::ECS::TaskDefinition",
    )["Properties"]
    container = task["ContainerDefinitions"][0]
    environment = {
        item["Name"]: item["Value"]
        for item in container["Environment"]
    }
    table_import = environment["AXON_DYNAMODB_TABLE"]["Fn::ImportValue"]
    assert "StateTableName" in json.dumps(table_import)
    assert environment["AXON_CONTROL_PLANE_ONLY"] == "true"
    assert environment["AXON_ENABLED_PROVIDERS"] == "bedrock"
    for name, value in _QUERY_ENVIRONMENT.items():
        assert environment[name] == value
    assert environment["AXON_ALB_CLIENT_ID"] == {
        "Fn::ImportValue": {
            "Fn::Join": [
                ":",
                [
                    {"Ref": "IdentityStackName"},
                    "AlbClientId",
                ],
            ]
        }
    }
    assert container["ReadonlyRootFilesystem"] is True
    assert container["LinuxParameters"]["Capabilities"]["Drop"] == ["ALL"]
    assert container["LinuxParameters"]["InitProcessEnabled"] is True
    assert container["MountPoints"] == [
        {
            "ContainerPath": "/tmp",
            "ReadOnly": False,
            "SourceVolume": "tmp",
        }
    ]
    assert task["RuntimePlatform"] == {
        "CpuArchitecture": "X86_64",
        "OperatingSystemFamily": "LINUX",
    }
    parameters = template["Parameters"]
    assert "VerifiedImageUri" not in parameters
    image = parameters["ControlPlaneVerifiedImageUri"]
    assert "Default" not in image
    assert image["AllowedPattern"].endswith(
        r"@sha256:[0-9a-f]{64}$"
    )

def test_control_tasks_are_private_and_alb_requires_https_cognito(
    query_templates,
):
    template = query_templates["control-plane"]
    service = _one_resource(template, "AWS::ECS::Service")["Properties"]
    network = service["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert network["AssignPublicIp"] == "DISABLED"
    assert service["DesiredCount"] == 2
    assert service["EnableExecuteCommand"] is False
    assert service["DeploymentConfiguration"][
        "DeploymentCircuitBreaker"
    ] == {"Enable": True, "Rollback": True}
    assert all(
        subnet["Ref"].startswith("VpcControlSubnet")
        for subnet in network["Subnets"]
    )

    listeners = _resources(
        template,
        "AWS::ElasticLoadBalancingV2::Listener",
    )
    assert len(listeners) == 1
    listener = listeners[0]["Properties"]
    assert listener["Port"] == 443
    assert listener["Protocol"] == "HTTPS"
    assert listener["SslPolicy"] == (
        "ELBSecurityPolicy-TLS13-1-2-2021-06"
    )
    assert [action["Type"] for action in listener["DefaultActions"]] == [
        "authenticate-cognito",
        "forward",
    ]
    authentication = listener["DefaultActions"][0][
        "AuthenticateCognitoConfig"
    ]
    assert authentication["OnUnauthenticatedRequest"] == "authenticate"
    assert "AlbClientId" in json.dumps(
        authentication["UserPoolClientId"]
    )
    assert "HostedUiDomainName" in json.dumps(
        authentication["UserPoolDomain"]
    )

    load_balancer = _one_resource(
        template,
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
    )["Properties"]
    assert load_balancer["Scheme"] == "internet-facing"
    attributes = {
        item["Key"]: item["Value"]
        for item in load_balancer["LoadBalancerAttributes"]
    }
    assert attributes["deletion_protection.enabled"] == "true"
    assert attributes[
        "routing.http.drop_invalid_header_fields.enabled"
    ] == "true"
    assert attributes["routing.http.desync_mitigation_mode"] == "strictest"
    assert attributes["access_logs.s3.enabled"] == "true"


def test_control_hostname_has_a_concrete_route53_alias(query_templates):
    template = query_templates["control-plane"]
    parameters = template["Parameters"]
    assert "Default" not in parameters["PublicHostedZoneId"]
    assert parameters["PublicHostedZoneId"]["AllowedPattern"] == (
        "^Z[A-Z0-9]+$"
    )

    record = _one_resource(
        template,
        "AWS::Route53::RecordSet",
    )["Properties"]
    assert record["Type"] == "A"
    assert record["HostedZoneId"] == {"Ref": "PublicHostedZoneId"}
    assert "ControlPlaneDomainName" in json.dumps(record["Name"])
    assert record["AliasTarget"]["DNSName"]["Fn::Join"][1][0] == (
        "dualstack."
    )
    assert record["AliasTarget"]["HostedZoneId"]["Fn::GetAtt"][1] == (
        "CanonicalHostedZoneID"
    )
    assert record["AliasTarget"]["EvaluateTargetHealth"] is True


def test_control_plane_network_has_no_world_open_security_group_rule(
    query_templates,
):
    template = query_templates["control-plane"]
    security_resources = {
        logical_id: resource
        for logical_id, resource in template["Resources"].items()
        if resource["Type"].startswith("AWS::EC2::SecurityGroup")
    }
    serialized = json.dumps(security_resources)
    assert "0.0.0.0/0" not in serialized
    assert "::/0" not in serialized
    assert "ApprovedIngressPrefixListId" in serialized
    assert "ApprovedHttpsPrefixListId" in serialized

    subnets = _resources(template, "AWS::EC2::Subnet")
    assert len(subnets) == 4
    assert all(
        subnet["Properties"]["MapPublicIpOnLaunch"] is False
        for subnet in subnets
    )
    assert len(_resources(template, "AWS::EC2::NatGateway")) == 2


def test_control_plane_retains_bindings_but_has_no_query_execution_authority(
    query_templates,
):
    template = query_templates["control-plane"]
    endpoints = _resources(template, "AWS::EC2::VPCEndpoint")
    assert len(endpoints) == 7
    services = {
        str(endpoint["Properties"]["ServiceName"])
        for endpoint in endpoints
    }
    assert not any("athena" in service for service in services)
    assert not any(service.endswith(".sts") for service in services)

    task_policy = next(
        resource
        for logical_id, resource in template["Resources"].items()
        if logical_id.startswith("TaskRoleDefaultPolicy")
    )
    statements = task_policy["Properties"]["PolicyDocument"]["Statement"]
    assert not any(
        action.startswith(("athena:", "sts:"))
        for statement in statements
        for action in _actions(statement)
    )
    policies = _resources(template, "AWS::IAM::Policy")
    assert not any(
        role_arn in json.dumps(policies) for role_arn in _ROLE_ARNS
    )
    dynamodb = next(
        statement
        for statement in statements
        if "dynamodb:ConditionCheckItem" in _actions(statement)
    )
    assert "dynamodb:TransactWriteItems" not in _actions(dynamodb)
    assert "StateTableName" in json.dumps(dynamodb["Resource"])
    assert "*" not in dynamodb["Resource"]

    transaction_policy = next(
        resource
        for logical_id, resource in template["Resources"].items()
        if logical_id.startswith("TaskDynamoTransactionPolicy")
    )
    assert transaction_policy["Metadata"] == {
        "cfn-lint": {"config": {"ignore_checks": ["W3037"]}}
    }
    transaction_statements = transaction_policy["Properties"][
        "PolicyDocument"
    ]["Statement"]
    assert len(transaction_statements) == 1
    transaction = transaction_statements[0]
    assert _actions(transaction) == {"dynamodb:TransactWriteItems"}
    assert "StateTableName" in json.dumps(transaction["Resource"])
    assert "*" not in transaction["Resource"]


def test_control_plane_retains_logs_and_access_log_bucket(
    query_templates,
):
    template = query_templates["control-plane"]
    application_logs = next(
        log_group
        for log_group in _resources(template, "AWS::Logs::LogGroup")
        if log_group["DeletionPolicy"] == "Retain"
    )
    assert application_logs["Properties"]["RetentionInDays"] == 365
    assert "KmsKeyId" in application_logs["Properties"]

    bucket = _one_resource(template, "AWS::S3::Bucket")
    assert bucket["DeletionPolicy"] == "Retain"
    properties = bucket["Properties"]
    assert properties["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert properties["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
