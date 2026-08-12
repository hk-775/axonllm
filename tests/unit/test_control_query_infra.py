"""CDK security contracts for the AgentCore query and ECS control planes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import types

import pytest


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "infra"
_INFRA_PYTHON = _INFRA / ".venv" / "bin" / "python"
_ROLE_ARNS = {
    "arn:aws:iam::123456789012:role/AxonAthenaReader",
    "arn:aws:iam::210987654321:role/data/AxonAthenaReader",
}
_SCIM_SECRET_ARN = (
    "arn:aws:secretsmanager:us-east-1:123456789012:"
    "secret:axonllm/scim-AbCd12"
)
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


def _service_endpoint(template: dict, service_suffix: str) -> dict:
    matches = [
        endpoint["Properties"]
        for endpoint in _resources(template, "AWS::EC2::VPCEndpoint")
        if service_suffix
        in json.dumps(endpoint["Properties"]["ServiceName"])
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
    namespace = (extra_context or {}).get("deployment_namespace", "")
    if namespace:
        stack_name = f"{stack_name}-{namespace}"
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


@pytest.fixture(scope="module")
def control_template_with_scim_secret(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    return _synth(
        target="control-plane",
        work_dir=tmp_path_factory.mktemp("control-scim-secret"),
        extra_context={
            "scim_tenants_secret_arn": _SCIM_SECRET_ARN,
        },
    )


@pytest.fixture(scope="module")
def managed_control_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    return _synth(
        target="control-plane",
        work_dir=tmp_path_factory.mktemp(
            "managed-control-launch-workers"
        ),
        extra_context={
            "account": "123456789012",
            "deployment_namespace": "managed",
        },
    )


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
    assert len(endpoints) == 8
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
    table_selector = environment["AXON_DYNAMODB_TABLE"]["Fn::If"]
    assert table_selector[:2] == [
        "UseRecoveredState",
        {"Ref": "RuntimeStateTableName"},
    ]
    assert "StateTableName" in json.dumps(table_selector[2])
    assert environment["AXON_CONTROL_PLANE_ONLY"] == "true"
    assert environment["AXON_SAML_FEDERATION_MODE"] == "managed-cognito"
    assert environment["AXON_SAML_LOGIN_PATH"] == {
        "Ref": "SamlLoginPath"
    }
    assert environment["AXON_ENABLED_PROVIDERS"] == "bedrock"
    assert template["Parameters"]["SamlLoginPath"]["Default"] == (
        "/admin/dashboard"
    )
    login_path_pattern = re.compile(
        template["Parameters"]["SamlLoginPath"]["AllowedPattern"]
    )
    assert login_path_pattern.fullmatch("/chat")
    for unsafe_path in (
        "/",
        "//evil.example/login",
        "/saml/login",
        "/SCIM/v2/Users",
        "/oauth2/authorize",
        "/admin/../ready",
        "/admin dashboard",
        "/caf\u00e9",
    ):
        assert login_path_pattern.fullmatch(unsafe_path) is None
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


def test_control_plane_kms_access_is_service_and_context_bound(
    query_templates,
):
    template = query_templates["control-plane"]
    task_policy = next(
        resource
        for logical_id, resource in template["Resources"].items()
        if logical_id.startswith("TaskRoleDefaultPolicy")
    )
    statements = task_policy["Properties"]["PolicyDocument"]["Statement"]
    kms_statements = [
        statement
        for statement in statements
        if any(action.startswith("kms:") for action in _actions(statement))
    ]
    assert len(kms_statements) == 2
    assert all(
        _actions(statement)
        == {"kms:Decrypt", "kms:GenerateDataKey*"}
        for statement in kms_statements
    )
    assert not any(
        action in {"kms:Encrypt", "kms:ReEncrypt*"}
        for statement in statements
        for action in _actions(statement)
    )
    assert all(
        statement["Resource"] != "*"
        and "DataKeyArn" in json.dumps(statement["Resource"])
        for statement in kms_statements
    )

    def for_service(service: str) -> dict:
        matches = [
            statement
            for statement in kms_statements
            if f"{service}." in json.dumps(
                statement["Condition"]["StringEquals"][
                    "kms:ViaService"
                ]
            )
        ]
        assert len(matches) == 1
        return matches[0]

    queue_condition = for_service("sqs")["Condition"]["StringEquals"]
    assert queue_condition["kms:CallerAccount"] == {
        "Ref": "AWS::AccountId"
    }
    assert "AWS::URLSuffix" in json.dumps(
        queue_condition["kms:ViaService"]
    )
    assert set(queue_condition) == {
        "kms:CallerAccount",
        "kms:ViaService",
    }
    topic_condition = for_service("sns")["Condition"]["StringEquals"]
    assert topic_condition["kms:CallerAccount"] == {
        "Ref": "AWS::AccountId"
    }
    assert "AWS::URLSuffix" in json.dumps(
        topic_condition["kms:ViaService"]
    )
    assert "SecurityEventTopicArn" in json.dumps(
        topic_condition["kms:EncryptionContext:aws:sns:topicArn"]
    )


def test_control_plane_recovery_selector_is_fail_closed(
    query_templates,
):
    template = query_templates["control-plane"]
    parameters = template["Parameters"]
    primary_table = parameters["PrimaryStateTableName"]
    assert "Default" not in primary_table
    assert primary_table["MinLength"] == 3
    assert primary_table["MaxLength"] == 255
    assert primary_table["AllowedPattern"] == (
        r"^[A-Za-z0-9_.-]{3,255}$"
    )
    assert parameters["RuntimeStateTableName"]["Default"] == ""
    assert parameters["RecoveryCutoverMode"]["AllowedValues"] == [
        "normal",
        "quiesced",
        "selected",
    ]
    assert parameters["RecoveryApprovalId"]["Default"] == ""
    assert parameters["DeploymentTransitionId"] == {
        "Type": "String",
        "Default": "",
        "MaxLength": 64,
        "AllowedPattern": r"^$|^[0-9a-f]{64}$",
        "ConstraintDescription": (
            "must be blank or the signed 64-character deployment "
            "transition identifier"
        ),
        "Description": (
            "Signed production transition that owns this exact "
            "control-plane deployment"
        ),
    }
    assert template["Conditions"]["RecoveryAccessBlocked"] == {
        "Fn::Or": [
            {"Condition": "RecoveryQuiesced"},
            {"Condition": "RecoverySelected"},
        ]
    }

    task = _one_resource(
        template,
        "AWS::ECS::TaskDefinition",
    )["Properties"]
    environment = {
        item["Name"]: item["Value"]
        for item in task["ContainerDefinitions"][0]["Environment"]
    }
    selected = environment["AXON_DYNAMODB_TABLE"]["Fn::If"]
    assert selected[:2] == [
        "UseRecoveredState",
        {"Ref": "RuntimeStateTableName"},
    ]

    task_policy = next(
        resource
        for logical_id, resource in template["Resources"].items()
        if logical_id.startswith("TaskRoleDefaultPolicy")
    )
    state_access = next(
        statement
        for statement in task_policy["Properties"]["PolicyDocument"][
            "Statement"
        ]
        if "dynamodb:ConditionCheckItem" in _actions(statement)
    )
    assert "UseRecoveredState" in json.dumps(state_access["Resource"])
    assert "RuntimeStateTableName" in json.dumps(
        state_access["Resource"]
    )

    deny = next(
        resource
        for logical_id, resource in template["Resources"].items()
        if logical_id.startswith("RecoveryStateAccessDeny")
    )
    deny_statement = deny["Properties"]["PolicyDocument"]["Statement"][0]
    assert deny_statement["Effect"] == "Deny"
    assert "dynamodb:TransactWriteItems" not in _actions(deny_statement)
    assert deny_statement["Resource"]["Fn::If"][0] == (
        "RecoveryAccessBlocked"
    )
    assert deny_statement["Resource"]["Fn::If"][1] == "*"

    transaction_deny = next(
        resource
        for logical_id, resource in template["Resources"].items()
        if logical_id.startswith("RecoveryStateTransactionAccessDeny")
    )
    assert transaction_deny["Metadata"] == {
        "cfn-lint": {"config": {"ignore_checks": ["W3037"]}}
    }
    transaction_deny_statement = transaction_deny["Properties"][
        "PolicyDocument"
    ]["Statement"][0]
    assert _actions(transaction_deny_statement) == {
        "dynamodb:TransactWriteItems"
    }
    assert (
        transaction_deny_statement["Resource"]
        == deny_statement["Resource"]
    )

    guard = template["Resources"]["RecoveryGuard"]
    assert guard["Properties"]["PrimaryTable"] == {
        "Ref": "PrimaryStateTableName"
    }
    assert guard["Properties"]["SelectedTable"] == (
        environment["AXON_DYNAMODB_TABLE"]
    )
    assert guard["Properties"]["Mode"] == {
        "Ref": "RecoveryCutoverMode"
    }
    assert guard["Properties"]["ApprovalId"] == {
        "Ref": "RecoveryApprovalId"
    }

    service = _one_resource(template, "AWS::ECS::Service")
    assert service["Properties"]["DesiredCount"] == {
        "Fn::If": ["RecoveryNormal", 2, 0]
    }
    assert "RecoveryGuard" in service["DependsOn"]
    scaling = _one_resource(
        template,
        "AWS::ApplicationAutoScaling::ScalableTarget",
    )
    assert scaling["Properties"]["MinCapacity"] == {
        "Fn::If": ["RecoveryNormal", 2, 0]
    }
    suspended = scaling["Properties"]["SuspendedState"]["Fn::If"]
    assert suspended[0] == "RecoveryNormal"
    assert not any(suspended[1].values())
    assert all(suspended[2].values())
    assert "RecoveryGuard" in scaling["DependsOn"]

    dynamodb_endpoint = next(
        endpoint["Properties"]
        for endpoint in _resources(template, "AWS::EC2::VPCEndpoint")
        if endpoint["Properties"]["VpcEndpointType"] == "Gateway"
        and "dynamodb:GetItem" in json.dumps(
            endpoint["Properties"]["PolicyDocument"]
        )
    )
    endpoint_resources = dynamodb_endpoint["PolicyDocument"][
        "Statement"
    ][0]["Resource"]
    assert "UseRecoveredState" in json.dumps(endpoint_resources)

    outputs = template["Outputs"]
    assert outputs["PrimaryStateTableName"]["Value"] == {
        "Ref": "PrimaryStateTableName"
    }
    assert outputs["SelectedRuntimeStateTableName"]["Value"] == (
        environment["AXON_DYNAMODB_TABLE"]
    )
    assert outputs["RecoveryCutoverMode"]["Value"] == {
        "Ref": "RecoveryCutoverMode"
    }
    assert outputs["RecoveryApprovalId"]["Value"] == {
        "Ref": "RecoveryApprovalId"
    }
    assert outputs["DeploymentTransitionId"]["Value"] == {
        "Ref": "DeploymentTransitionId"
    }

    handler = next(
        resource
        for logical_id, resource in template["Resources"].items()
        if logical_id.startswith("RecoveryGuardHandler")
        and resource["Type"] == "AWS::Lambda::Function"
    )
    code = handler["Properties"]["Code"]["ZipFile"]
    assert "_assert_control_plane_quiesced" in code
    assert "_assert_agentcore" in code
    assert "quiesced <-> selected transition" in code


def test_control_plane_recovery_guard_enforces_cross_stack_phases(
    query_templates,
    monkeypatch,
):
    template = query_templates["control-plane"]
    handler = next(
        resource
        for logical_id, resource in template["Resources"].items()
        if logical_id.startswith("RecoveryGuardHandler")
        and resource["Type"] == "AWS::Lambda::Function"
    )
    agentcore = {
        "mode": "normal",
        "selected": "axonllm-agentcore-state",
        "approval": "",
    }

    class _CloudFormation:
        def describe_stacks(self, *, StackName):
            if StackName == "AxonLLMControlPlaneStack":
                outputs = {
                    "ClusterName": "control",
                    "ServiceName": "web",
                }
            else:
                outputs = {
                    "RecoveryApprovalId": agentcore["approval"],
                    "RecoveryCutoverMode": agentcore["mode"],
                    "SelectedRuntimeStateTableName": (
                        agentcore["selected"]
                    ),
                    "StateTableName": "axonllm-agentcore-state",
                }
            return {
                "Stacks": [
                    {
                        "Outputs": [
                            {
                                "OutputKey": name,
                                "OutputValue": value,
                            }
                            for name, value in outputs.items()
                        ]
                    }
                ]
            }

    class _Ecs:
        def describe_services(self, **_kwargs):
            return {
                "failures": [],
                "services": [
                    {
                        "desiredCount": 0,
                        "pendingCount": 0,
                        "runningCount": 0,
                    }
                ],
            }

    class _Scaling:
        def describe_scalable_targets(self, **_kwargs):
            return {
                "ScalableTargets": [
                    {
                        "MinCapacity": 0,
                        "SuspendedState": {
                            "DynamicScalingInSuspended": True,
                            "DynamicScalingOutSuspended": True,
                            "ScheduledScalingSuspended": True,
                        },
                    }
                ]
            }

    clients = {
        "application-autoscaling": _Scaling(),
        "cloudformation": _CloudFormation(),
        "ecs": _Ecs(),
    }
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        types.SimpleNamespace(client=lambda name: clients[name]),
    )
    namespace: dict[str, object] = {}
    exec(
        compile(
            handler["Properties"]["Code"]["ZipFile"],
            "control_plane_recovery_guard.py",
            "exec",
        ),
        namespace,
    )

    base = {
        "AgentCoreStackName": "AxonLLMAgentCoreStack",
        "ApprovalId": "",
        "ControlPlaneStackName": "AxonLLMControlPlaneStack",
        "Mode": "normal",
        "PrimaryTable": "axonllm-agentcore-state",
        "SelectedTable": "axonllm-agentcore-state",
    }
    created = namespace["handler"](
        {"RequestType": "Create", "ResourceProperties": base},
        None,
    )
    quiesced = {
        **base,
        "ApprovalId": "CHG-2026-030",
        "Mode": "quiesced",
    }
    namespace["handler"](
        {
            "RequestType": "Update",
            "PhysicalResourceId": created["PhysicalResourceId"],
            "OldResourceProperties": base,
            "ResourceProperties": quiesced,
        },
        None,
    )

    restored = (
        "axonllm-agentcore-state-restore-validation-reviewed"
    )
    selected = {
        **quiesced,
        "Mode": "selected",
        "SelectedTable": restored,
    }
    agentcore.update(
        mode="quiesced",
        selected="axonllm-agentcore-state",
        approval="CHG-2026-030",
    )
    namespace["handler"](
        {
            "RequestType": "Update",
            "PhysicalResourceId": created["PhysicalResourceId"],
            "OldResourceProperties": quiesced,
            "ResourceProperties": selected,
        },
        None,
    )

    agentcore.update(mode="normal", selected=restored)
    namespace["handler"](
        {
            "RequestType": "Update",
            "PhysicalResourceId": created["PhysicalResourceId"],
            "OldResourceProperties": selected,
            "ResourceProperties": {
                **selected,
                "Mode": "normal",
            },
        },
        None,
    )

    with pytest.raises(RuntimeError, match="table changes require"):
        namespace["handler"](
            {
                "RequestType": "Update",
                "PhysicalResourceId": created["PhysicalResourceId"],
                "OldResourceProperties": base,
                "ResourceProperties": {
                    **base,
                    "SelectedTable": restored,
                },
            },
            None,
        )

    agentcore.update(mode="selected", selected=restored)
    with pytest.raises(RuntimeError, match="does not authorize"):
        namespace["handler"](
            {
                "RequestType": "Update",
                "PhysicalResourceId": created["PhysicalResourceId"],
                "OldResourceProperties": quiesced,
                "ResourceProperties": selected,
            },
            None,
        )


def test_control_tasks_are_private_and_alb_requires_https_cognito(
    query_templates,
):
    template = query_templates["control-plane"]
    service = _one_resource(template, "AWS::ECS::Service")["Properties"]
    network = service["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert network["AssignPublicIp"] == "DISABLED"
    assert service["DesiredCount"] == {
        "Fn::If": ["RecoveryNormal", 2, 0]
    }
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
    listener_rule = _one_resource(
        template,
        "AWS::ElasticLoadBalancingV2::ListenerRule",
    )["Properties"]
    assert listener_rule["Priority"] == 10
    assert [action["Type"] for action in listener_rule["Actions"]] == [
        "forward"
    ]
    assert listener_rule["Conditions"] == [
        {
            "Field": "path-pattern",
            "PathPatternConfig": {
                "Values": ["/scim/*"],
            },
        }
    ]

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
    assert template["Outputs"]["TargetGroupArn"]["Value"] == {
        "Ref": next(
            logical_id
            for logical_id, resource in template["Resources"].items()
            if resource["Type"]
            == "AWS::ElasticLoadBalancingV2::TargetGroup"
        )
    }


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


def test_control_plane_scim_secret_is_exact_and_private(
    control_template_with_scim_secret,
):
    template = control_template_with_scim_secret
    task = _one_resource(
        template,
        "AWS::ECS::TaskDefinition",
    )["Properties"]
    container = task["ContainerDefinitions"][0]
    secrets = {
        item["Name"]: item["ValueFrom"]
        for item in container["Secrets"]
    }
    assert secrets == {
        "AXON_SCIM_TENANTS": _SCIM_SECRET_ARN,
    }
    environment_names = {
        item["Name"] for item in container["Environment"]
    }
    assert {
        "AXON_SAML_SP_ENTITY_ID",
        "AXON_SAML_ACS_URL",
        "AXON_SAML_IDP_ENTITY_ID",
        "AXON_SAML_IDP_SSO_URL",
        "AXON_SAML_IDP_CERT",
        "AXON_SAML_IDP_CERT_FILE",
    }.isdisjoint(environment_names | set(secrets))

    secrets_endpoint = _interface_endpoint(template, ".secretsmanager")
    statement = secrets_endpoint["PolicyDocument"]["Statement"][0]
    assert _actions(statement) == {
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
    }
    assert _as_set(statement["Resource"]) == {_SCIM_SECRET_ARN}
    assert statement["Principal"]["AWS"]["Fn::GetAtt"][0].startswith(
        "ExecutionRole"
    )

    execution_policy = next(
        resource
        for logical_id, resource in template["Resources"].items()
        if logical_id.startswith("ExecutionRoleDefaultPolicy")
    )
    secret_reads = [
        statement
        for statement in execution_policy["Properties"][
            "PolicyDocument"
        ]["Statement"]
        if "secretsmanager:GetSecretValue" in _actions(statement)
    ]
    assert {statement["Resource"] for statement in secret_reads} == {
        _SCIM_SECRET_ARN,
    }
    assert template["Outputs"]["ScimTenantsSecretArn"]["Value"] == (
        _SCIM_SECRET_ARN
    )
    assert "SamlConfigSecretArn" not in template["Outputs"]


def test_managed_control_plane_authorizes_exact_launch_worker_principals(
    managed_control_template,
):
    template = managed_control_template
    task_roles = {
        "AxonLLMLaunchActionWorkerRole",
        "AxonLLMLaunchCleanupWorkerRole",
    }
    execution_role_marker = "LaunchWorkerExecutionRole"

    endpoints = _resources(template, "AWS::EC2::VPCEndpoint")
    assert len(endpoints) == 14
    interface_services = {
        str(endpoint["Properties"]["ServiceName"])
        for endpoint in endpoints
        if endpoint["Properties"]["VpcEndpointType"] == "Interface"
    }
    for suffix in (
        ".application-autoscaling",
        ".bedrock-agentcore",
        ".cloudformation",
        ".ecs",
        ".monitoring",
        ".secretsmanager",
        ".states",
    ):
        assert any(service.endswith(suffix) for service in interface_services)

    worker_interfaces = [
        endpoint["Properties"]
        for endpoint in endpoints
        if endpoint["Properties"]["VpcEndpointType"] == "Interface"
        and any(
            str(endpoint["Properties"]["ServiceName"]).endswith(suffix)
            for suffix in (
                ".application-autoscaling",
                ".bedrock-agentcore",
                ".cloudformation",
                ".ecs",
                ".monitoring",
                ".secretsmanager",
                ".states",
            )
        )
    ]
    assert len(worker_interfaces) == 7
    endpoint_security_groups = {
        json.dumps(endpoint["SecurityGroupIds"], sort_keys=True)
        for endpoint in worker_interfaces
    }
    assert len(endpoint_security_groups) == 1
    assert "EndpointSecurityGroup" in next(
        iter(endpoint_security_groups)
    )
    assert all(
        endpoint["PrivateDnsEnabled"] is True
        and len(endpoint["SubnetIds"]) == 2
        and all(
            "ControlSubnet" in json.dumps(subnet)
            for subnet in endpoint["SubnetIds"]
        )
        for endpoint in worker_interfaces
    )
    security_group_rules = [
        resource
        for resource in template["Resources"].values()
        if resource["Type"]
        in {
            "AWS::EC2::SecurityGroupEgress",
            "AWS::EC2::SecurityGroupIngress",
        }
        and resource["Properties"].get("FromPort") == 443
        and resource["Properties"].get("ToPort") == 443
    ]
    task_endpoint_rules = [
        rule
        for rule in security_group_rules
        if "TaskSecurityGroup" in json.dumps(rule)
        and "EndpointSecurityGroup" in json.dumps(rule)
    ]
    assert {
        rule["Type"] for rule in task_endpoint_rules
    } == {
        "AWS::EC2::SecurityGroupEgress",
        "AWS::EC2::SecurityGroupIngress",
    }

    worker_statements: list[dict] = []
    for endpoint in endpoints:
        statements = endpoint["Properties"].get(
            "PolicyDocument",
            {},
        ).get("Statement", [])
        for statement in statements:
            principal_text = json.dumps(statement.get("Principal", {}))
            names = {
                role_name
                for role_name in (
                    *task_roles,
                    execution_role_marker,
                )
                if role_name in principal_text
            }
            if not names:
                continue
            worker_statements.append(statement)
            assert statement["Effect"] == "Allow"
            assert statement["Principal"]["AWS"] != "*"
            if execution_role_marker in names:
                assert names == {execution_role_marker}
            else:
                assert names == task_roles

    assert worker_statements
    worker_policy = json.dumps(worker_statements)
    for role_name in (*task_roles, execution_role_marker):
        assert role_name in worker_policy
    assert ":iam::*:" not in worker_policy

    execution_roles = [
        role["Properties"]
        for role in _resources(template, "AWS::IAM::Role")
        if role["Properties"].get("RoleName")
        == "AxonLLMLaunchWorkerExecutionRole-managed"
    ]
    assert len(execution_roles) == 1


def test_managed_launch_worker_private_paths_match_domain_calls(
    managed_control_template,
):
    template = managed_control_template
    task_role_marker = "AxonLLMLaunchActionWorkerRole"

    expected_actions = {
        ".application-autoscaling": {
            "application-autoscaling:DescribeScalableTargets",
            "application-autoscaling:RegisterScalableTarget",
        },
        ".bedrock-agentcore": {
            "bedrock-agentcore:GetAgentRuntime",
            "bedrock-agentcore:GetAgentRuntimeEndpoint",
            "bedrock-agentcore:InvokeAgentRuntime",
        },
        ".cloudformation": {
            "cloudformation:DescribeStacks",
            "cloudformation:UpdateStack",
        },
        ".dynamodb": {
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
        },
        ".ecs": {
            "ecs:DescribeServices",
            "ecs:UpdateService",
        },
        ".logs": {
            "logs:CreateLogStream",
            "logs:DeleteLogStream",
            "logs:FilterLogEvents",
        },
        ".monitoring": {
            "cloudwatch:DescribeAlarms",
            "cloudwatch:PutMetricData",
        },
        ".secretsmanager": {
            "secretsmanager:DescribeSecret",
            "secretsmanager:GetSecretValue",
        },
        ".sqs": {
            "sqs:ChangeMessageVisibility",
            "sqs:DeleteMessage",
            "sqs:GetQueueAttributes",
            "sqs:ReceiveMessage",
            "sqs:SendMessage",
        },
        ".states": {
            "states:GetActivityTask",
            "states:SendTaskFailure",
            "states:SendTaskHeartbeat",
            "states:SendTaskSuccess",
        },
    }
    for service_suffix, actions in expected_actions.items():
        endpoint = _service_endpoint(template, service_suffix)
        worker_statements = [
            statement
            for statement in endpoint["PolicyDocument"]["Statement"]
            if task_role_marker
            in json.dumps(statement.get("Principal", {}))
        ]
        assert worker_statements
        assert {
            action
            for statement in worker_statements
            for action in _actions(statement)
        } == actions

    serialized = json.dumps(
        [
            statement
            for endpoint in _resources(
                template,
                "AWS::EC2::VPCEndpoint",
            )
            for statement in endpoint["Properties"]
            .get("PolicyDocument", {})
            .get("Statement", [])
            if task_role_marker
            in json.dumps(statement.get("Principal", {}))
        ]
    )
    for resource_fragment in (
        "activity:axonllm-agentcore-launch-actions",
        "activity:axonllm-agentcore-launch-cleanup",
        "runtime/axonllm_managed-*",
        "stack/AxonLLMAgentCoreStack-managed/*",
        "stack/AxonLLMControlPlaneStack-managed/*",
        "table/axonllm-agentcore-state-managed",
        "table/axonllm-agentcore-state-managed-restore-validation-*",
        "table/axonllm-launch-rehearsal-leases",
        "RehearsalControlTableArn",
        "AxonLLMAgentCoreStack-managed-SecurityEventOutboxQueue*",
        "AxonLLMAgentCoreStack-managed-SecurityEventDeadLetterQueue*",
        "AxonLLMAgentCoreStack-managed-SecurityEventLogGroup*",
        "AxonLLMControlPlaneStack-managed-Service*",
        "secret:axonllm/launch/runtime-identity-*",
    ):
        assert resource_fragment in serialized


def test_managed_worker_execution_role_has_private_image_and_log_paths(
    managed_control_template,
):
    template = managed_control_template
    execution_role_marker = "LaunchWorkerExecutionRole"
    expected_actions = {
        ".ecr.api": {
            "ecr:BatchCheckLayerAvailability",
            "ecr:BatchGetImage",
            "ecr:GetAuthorizationToken",
            "ecr:GetDownloadUrlForLayer",
        },
        ".ecr.dkr": {
            "ecr:BatchCheckLayerAvailability",
            "ecr:BatchGetImage",
            "ecr:GetDownloadUrlForLayer",
        },
        ".logs": {
            "logs:CreateLogStream",
            "logs:PutLogEvents",
        },
        ".s3": {"s3:GetObject"},
    }
    statements: list[dict] = []
    for service_suffix, actions in expected_actions.items():
        endpoint = _service_endpoint(template, service_suffix)
        matches = [
            statement
            for statement in endpoint["PolicyDocument"]["Statement"]
            if execution_role_marker
            in json.dumps(statement.get("Principal", {}))
        ]
        assert matches
        statements.extend(matches)
        assert {
            action
            for statement in matches
            for action in _actions(statement)
        } == actions

    serialized = json.dumps(statements)
    assert "repository/axonllm/fargate" in serialized
    assert "prod-us-east-1-starport-layer-bucket/*" in serialized
    assert (
        "/aws/ecs/axonllm/launch-workers/action-managed:"
        "log-stream:*"
    ) in serialized
    assert (
        "/aws/ecs/axonllm/launch-workers/cleanup-managed:"
        "log-stream:*"
    ) in serialized

    execution_role_id, execution_role = next(
        (logical_id, role["Properties"])
        for logical_id, role in template["Resources"].items()
        if role["Type"] == "AWS::IAM::Role"
        and role["Properties"].get("RoleName")
        == "AxonLLMLaunchWorkerExecutionRole-managed"
    )
    assert execution_role["MaxSessionDuration"] == 3600
    execution_policy = next(
        policy["Properties"]
        for policy in _resources(template, "AWS::IAM::Policy")
        if {"Ref": execution_role_id}
        in policy["Properties"].get("Roles", [])
    )
    role_statements = execution_policy["PolicyDocument"]["Statement"]
    by_sid = {
        statement["Sid"]: statement for statement in role_statements
    }
    assert set(by_sid) == {
        "AuthorizeLaunchWorkerImagePull",
        "DeliverLaunchWorkerLogs",
        "PullExactLaunchWorkerImage",
    }
    assert _actions(by_sid["AuthorizeLaunchWorkerImagePull"]) == {
        "ecr:GetAuthorizationToken"
    }
    assert by_sid["AuthorizeLaunchWorkerImagePull"]["Resource"] == "*"
    assert _actions(by_sid["PullExactLaunchWorkerImage"]) == {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
    }
    assert "repository/axonllm/fargate" in json.dumps(
        by_sid["PullExactLaunchWorkerImage"]["Resource"]
    )
    assert _actions(by_sid["DeliverLaunchWorkerLogs"]) == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }


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
