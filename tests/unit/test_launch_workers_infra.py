"""Deployment contracts for the isolated launch-workers CDK target."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import pytest


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "infra"
_INFRA_PYTHON = _INFRA / ".venv" / "bin" / "python"
_ACTION_LOG_GROUP = "/aws/ecs/axonllm/launch-workers/action"
_CLEANUP_LOG_GROUP = "/aws/ecs/axonllm/launch-workers/cleanup"
_WORKER_SCRIPT = "scripts/operations/launch_activity_worker.py"
_HANDLER_MODULE = "launch_activity_domains"


def _run_synth(
    work_dir: Path,
    *,
    namespace: str = "",
    account: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    work_dir.mkdir(parents=True, exist_ok=True)
    out_dir = work_dir / "cdk.out"
    context = {
        "deployment_target": "launch-workers",
        "region": "us-east-1",
    }
    if namespace:
        context["deployment_namespace"] = namespace
    if account:
        context["account"] = account
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(context),
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
    return completed, out_dir


def _synth(
    work_dir: Path,
    *,
    namespace: str = "",
    account: str | None = None,
) -> tuple[dict, Path]:
    completed, out_dir = _run_synth(
        work_dir,
        namespace=namespace,
        account=account,
    )
    assert completed.returncode == 0, completed.stdout
    stack_name = "AxonLLMLaunchWorkersStack"
    if namespace:
        stack_name = f"{stack_name}-{namespace}"
    template_path = out_dir / f"{stack_name}.template.json"
    return json.loads(template_path.read_text(encoding="utf-8")), template_path


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [resource for resource in template["Resources"].values() if resource["Type"] == resource_type]


def _resource_entries(
    template: dict,
    resource_type: str,
) -> list[tuple[str, dict]]:
    return [
        (logical_id, resource)
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == resource_type
    ]


def _actions(statement: dict) -> set[str]:
    actions = statement["Action"]
    if isinstance(actions, str):
        return {actions}
    return set(actions)


@pytest.fixture(scope="module")
def synthesized_template(tmp_path_factory: pytest.TempPathFactory) -> dict:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")

    work_dir = tmp_path_factory.mktemp("launch-workers")
    template, _ = _synth(work_dir)
    return template


@pytest.fixture(scope="module")
def namespaced_template(tmp_path_factory: pytest.TempPathFactory) -> dict:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")

    work_dir = tmp_path_factory.mktemp("launch-workers-namespaced")
    template, template_path = _synth(
        work_dir,
        namespace="qa-1",
        account="123456789012",
    )
    assert template_path.name == "AxonLLMLaunchWorkersStack-qa-1.template.json"
    return template


def test_target_only_provisions_bounded_worker_compute(
    synthesized_template,
):
    expected_counts = {
        "AWS::ECS::Service": 2,
        "AWS::ECS::TaskDefinition": 2,
        "AWS::IAM::Role": 1,
        "AWS::KMS::Key": 1,
        "AWS::Logs::LogGroup": 2,
    }
    actual_counts: dict[str, int] = {}
    for resource in synthesized_template["Resources"].values():
        resource_type = resource["Type"]
        actual_counts[resource_type] = actual_counts.get(resource_type, 0) + 1
    assert actual_counts == expected_counts

    assert (
        _resources(
            synthesized_template,
            "AWS::ApplicationAutoScaling::ScalableTarget",
        )
        == []
    )
    assert (
        _resources(
            synthesized_template,
            "AWS::ApplicationAutoScaling::ScalingPolicy",
        )
        == []
    )
    assert _resources(synthesized_template, "AWS::ECS::Cluster") == []
    assert _resources(synthesized_template, "AWS::Lambda::Function") == []


def _assert_role_trust_domain(
    template: dict,
    *,
    qualifier: str,
) -> None:
    roles = _resources(template, "AWS::IAM::Role")
    for role in roles:
        properties = role["Properties"]
        assert (f"AxonLLMAgentCoreServiceBoundary-{qualifier}-us-east-1") in json.dumps(
            properties["PermissionsBoundary"]
        )
        tags = {tag["Key"]: tag["Value"] for tag in properties["Tags"]}
        assert tags["Application"] == "AxonLLM"
        assert tags["AxonLLMTrustDomain"] == qualifier


def test_worker_roles_have_matching_trust_domain_boundaries(
    synthesized_template,
    namespaced_template,
):
    _assert_role_trust_domain(
        synthesized_template,
        qualifier="axprod",
    )
    _assert_role_trust_domain(
        namespaced_template,
        qualifier="axqual",
    )


def test_import_parameters_are_exact_typed_and_have_no_defaults(
    synthesized_template,
):
    parameters = synthesized_template["Parameters"]
    imported = {name: value for name, value in parameters.items() if name != "BootstrapVersion"}
    assert set(imported) == {
        "ActionActivityArn",
        "ActionTaskRoleArn",
        "CleanupActivityArn",
        "CleanupTaskRoleArn",
        "ClusterArn",
        "LeaseTableArn",
        "PrivateSubnetIds",
        "QualificationMutationBrokerVersionArn",
        "RehearsalControlTableArn",
        "RuntimeIdentitySecretArn",
        "SecurityGroupIds",
        "WorkerImageRepositoryArn",
        "WorkerImageUri",
    }
    assert all("Default" not in value for value in imported.values())
    assert imported["PrivateSubnetIds"]["Type"] == ("List<AWS::EC2::Subnet::Id>")
    assert imported["SecurityGroupIds"]["Type"] == ("List<AWS::EC2::SecurityGroup::Id>")

    valid_arns = {
        "ActionActivityArn": ("arn:aws:states:us-east-1:123456789012:activity:axonllm-agentcore-launch-actions"),
        "ActionTaskRoleArn": ("arn:aws:iam::123456789012:role/AxonLLMLaunchActionWorkerRole"),
        "CleanupActivityArn": ("arn:aws:states:us-east-1:123456789012:activity:axonllm-agentcore-launch-cleanup"),
        "CleanupTaskRoleArn": ("arn:aws:iam::123456789012:role/AxonLLMLaunchCleanupWorkerRole"),
        "ClusterArn": ("arn:aws:ecs:us-east-1:123456789012:cluster/axonllm"),
        "LeaseTableArn": ("arn:aws:dynamodb:us-east-1:123456789012:table/axonllm-launch-rehearsal-leases"),
        "QualificationMutationBrokerVersionArn": (
            "arn:aws:lambda:us-east-1:123456789012:function:"
            "axonllm-qualification-selector-mutation-broker:7"
        ),
        "RehearsalControlTableArn": ("arn:aws:dynamodb:us-east-1:123456789012:table/axonllm-rehearsal-control-ledger"),
        "RuntimeIdentitySecretArn": (
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:axonllm/launch/runtime-identity-Ab12Cd"
        ),
        "WorkerImageRepositoryArn": ("arn:aws:ecr:us-east-1:123456789012:repository/axonllm/fargate"),
    }
    invalid_arns = {
        "ActionActivityArn": valid_arns["ActionActivityArn"].replace(
            "launch-actions",
            "foreign-actions",
        ),
        "ActionTaskRoleArn": valid_arns["ActionTaskRoleArn"].replace(
            "ActionWorkerRole",
            "ForeignRole",
        ),
        "CleanupActivityArn": valid_arns["CleanupActivityArn"].replace(
            "launch-cleanup",
            "foreign-cleanup",
        ),
        "CleanupTaskRoleArn": valid_arns["CleanupTaskRoleArn"].replace(
            "CleanupWorkerRole",
            "ForeignRole",
        ),
        "ClusterArn": valid_arns["ClusterArn"].replace(
            "us-east-1",
            "any",
        ),
        "LeaseTableArn": valid_arns["LeaseTableArn"].replace(
            "rehearsal-leases",
            "foreign-leases",
        ),
        "QualificationMutationBrokerVersionArn": (
            valid_arns["QualificationMutationBrokerVersionArn"].replace(
                "selector-mutation-broker:7",
                "selector-mutation-broker:$LATEST",
            )
        ),
        "RehearsalControlTableArn": valid_arns["RehearsalControlTableArn"].replace(
            "rehearsal-control-ledger",
            "foreign-control-ledger",
        ),
        "RuntimeIdentitySecretArn": valid_arns["RuntimeIdentitySecretArn"].replace(
            "runtime-identity",
            "foreign-identity",
        ),
        "WorkerImageRepositoryArn": (
            valid_arns["WorkerImageRepositoryArn"].replace(
                "axonllm/fargate",
                "foreign/repository",
            )
        ),
    }
    for name, valid_arn in valid_arns.items():
        pattern = imported[name]["AllowedPattern"]
        assert pattern.startswith("^")
        assert pattern.endswith("$")
        assert re.fullmatch(pattern, valid_arn)
        assert re.fullmatch(pattern, f"{valid_arn}*") is None
        assert re.fullmatch(pattern, invalid_arns[name]) is None
        assert imported[name]["ConstraintDescription"] == ("must be an exact supported AWS ARN")


def test_foundation_parameters_require_exact_region_name_and_secret_suffix(
    synthesized_template,
):
    parameters = synthesized_template["Parameters"]
    table_pattern = parameters["RehearsalControlTableArn"]["AllowedPattern"]
    secret_pattern = parameters["RuntimeIdentitySecretArn"]["AllowedPattern"]
    assert table_pattern == (
        r"^arn:aws:dynamodb:us-east-1:[0-9]{12}:"
        r"table/axonllm-rehearsal-control-ledger$"
    )
    assert secret_pattern == (
        r"^arn:aws:secretsmanager:us-east-1:[0-9]{12}:"
        r"secret:axonllm/launch/runtime-identity-[A-Za-z0-9]{6}$"
    )

    rejected_tables = [
        "arn:aws:dynamodb:us-west-2:123456789012:table/axonllm-rehearsal-control-ledger",
        "arn:aws:dynamodb:us-east-1:123456789012:table/axonllm-rehearsal-control",
    ]
    rejected_secrets = [
        "arn:aws:secretsmanager:us-west-2:123456789012:secret:axonllm/launch/runtime-identity-Ab12Cd",
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:axonllm/launch/runtime-identity",
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:axonllm/launch/runtime-identity-Ab12C",
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:axonllm/launch/runtime-identity-Ab12Cd7",
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:axonllm/launch/runtime-identity-Ab_2Cd",
    ]
    assert all(re.fullmatch(table_pattern, value) is None for value in rejected_tables)
    assert all(re.fullmatch(secret_pattern, value) is None for value in rejected_secrets)


def test_concrete_stack_account_pins_all_import_patterns(
    namespaced_template,
):
    parameters = namespaced_template["Parameters"]
    arn_parameters = {
        name: value for name, value in parameters.items() if name != "BootstrapVersion" and "AllowedPattern" in value
    }
    assert arn_parameters
    for parameter in arn_parameters.values():
        pattern = parameter["AllowedPattern"]
        assert "123456789012" in pattern
        assert "[0-9]{12}" not in pattern
        assert "210987654321" not in pattern


def test_image_parameter_only_accepts_private_ecr_digests(
    synthesized_template,
):
    image = synthesized_template["Parameters"]["WorkerImageUri"]
    pattern = re.compile(image["AllowedPattern"])
    digest = "a" * 64
    assert pattern.fullmatch(f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/fargate@sha256:{digest}")

    rejected = [
        f"public.ecr.aws/axonllm/worker@sha256:{digest}",
        f"ghcr.io/axonllm/worker@sha256:{digest}",
        f"123456789012.dkr.ecr.cn-north-1.amazonaws.com.cn/axonllm/fargate@sha256:{digest}",
        f"123456789012.dkr.ecr.us-east-1.amazonaws.com/foreign/worker@sha256:{digest}",
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/fargate:latest",
        f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/fargate@sha256:{digest.upper()}",
        f"123456789012.dkr.ecr.us-east-1.amazonaws.com/AxonLLM/fargate@sha256:{digest}",
    ]
    assert all(pattern.fullmatch(value) is None for value in rejected)
    assert image["ConstraintDescription"] == ("must be a private ECR image pinned by an exact sha256 digest")


def test_logs_are_exact_encrypted_protected_and_retained(
    synthesized_template,
):
    key = _resources(synthesized_template, "AWS::KMS::Key")[0]
    assert key["Properties"]["EnableKeyRotation"] is True
    assert key["Properties"]["PendingWindowInDays"] == 30
    assert key["DeletionPolicy"] == "Retain"
    assert key["UpdateReplacePolicy"] == "Retain"

    log_statement = next(
        statement
        for statement in key["Properties"]["KeyPolicy"]["Statement"]
        if statement["Sid"] == "EncryptOnlyExactWorkerLogGroups"
    )
    encryption_contexts = log_statement["Condition"]["ArnEquals"]["kms:EncryptionContext:aws:logs:arn"]
    assert {context["Fn::Sub"] for context in encryption_contexts} == {
        f"arn:${{AWS::Partition}}:logs:${{AWS::Region}}:${{AWS::AccountId}}:log-group:{_ACTION_LOG_GROUP}",
        f"arn:${{AWS::Partition}}:logs:${{AWS::Region}}:${{AWS::AccountId}}:log-group:{_CLEANUP_LOG_GROUP}",
    }
    assert "*" not in json.dumps(encryption_contexts)
    assert log_statement["Condition"]["StringEquals"]["kms:ViaService"] == {
        "Fn::Sub": "logs.${AWS::Region}.${AWS::URLSuffix}"
    }

    log_groups = _resources(
        synthesized_template,
        "AWS::Logs::LogGroup",
    )
    assert {group["Properties"]["LogGroupName"] for group in log_groups} == {_ACTION_LOG_GROUP, _CLEANUP_LOG_GROUP}
    for group in log_groups:
        properties = group["Properties"]
        assert properties["DeletionProtectionEnabled"] is True
        assert properties["KmsKeyId"] == {"Fn::GetAtt": ["WorkerLogKey", "Arn"]}
        assert properties["LogGroupClass"] == "STANDARD"
        assert properties["RetentionInDays"] == 3653
        assert group["DeletionPolicy"] == "Retain"
        assert group["UpdateReplacePolicy"] == "Retain"


def test_only_production_log_resources_are_retained(
    synthesized_template,
):
    retained = {
        logical_id: resource["Type"]
        for logical_id, resource in synthesized_template["Resources"].items()
        if resource.get("DeletionPolicy") == "Retain" or resource.get("UpdateReplacePolicy") == "Retain"
    }
    assert retained == {
        "ActionWorkerLogGroup": "AWS::Logs::LogGroup",
        "CleanupWorkerLogGroup": "AWS::Logs::LogGroup",
        "WorkerLogKey": "AWS::KMS::Key",
    }


def test_execution_role_is_only_for_exact_image_pull_and_log_delivery(
    synthesized_template,
):
    role = _resources(synthesized_template, "AWS::IAM::Role")[0]["Properties"]
    assert role["RoleName"] == "AxonLLMLaunchWorkerExecutionRole"
    trust = role["AssumeRolePolicyDocument"]["Statement"]
    assert trust == [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "ArnLike": {
                    "aws:SourceArn": {"Fn::Sub": ("arn:${AWS::Partition}:ecs:${AWS::Region}:${AWS::AccountId}:*")}
                },
                "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}},
            },
        }
    ]
    assert role["MaxSessionDuration"] == 3600
    assert len(role["Policies"]) == 1

    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    by_sid = {statement["Sid"]: statement for statement in statements}
    assert set(by_sid) == {
        "AuthorizePrivateEcr",
        "DeliverWorkerLogs",
        "PullExactWorkerRepository",
    }
    assert by_sid["AuthorizePrivateEcr"] == {
        "Sid": "AuthorizePrivateEcr",
        "Effect": "Allow",
        "Action": "ecr:GetAuthorizationToken",
        "Resource": "*",
    }
    pull = by_sid["PullExactWorkerRepository"]
    assert _actions(pull) == {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
    }
    assert pull["Resource"] == {"Ref": "WorkerImageRepositoryArn"}

    delivery = by_sid["DeliverWorkerLogs"]
    assert _actions(delivery) == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }
    assert {resource["Fn::Sub"] for resource in delivery["Resource"]} == {
        f"arn:${{AWS::Partition}}:logs:${{AWS::Region}}:${{AWS::AccountId}}:log-group:{_ACTION_LOG_GROUP}:log-stream:*",
        "arn:${AWS::Partition}:logs:${AWS::Region}:"
        "${AWS::AccountId}:log-group:"
        f"{_CLEANUP_LOG_GROUP}:log-stream:*",
    }

    wildcard_resources = [
        (statement["Sid"], resource)
        for statement in statements
        for resource in (statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]])
        if "*" in json.dumps(resource)
    ]
    assert wildcard_resources == [
        ("AuthorizePrivateEcr", "*"),
        (
            "DeliverWorkerLogs",
            {
                "Fn::Sub": (
                    "arn:${AWS::Partition}:logs:${AWS::Region}:"
                    "${AWS::AccountId}:log-group:"
                    f"{_ACTION_LOG_GROUP}:log-stream:*"
                )
            },
        ),
        (
            "DeliverWorkerLogs",
            {
                "Fn::Sub": (
                    "arn:${AWS::Partition}:logs:${AWS::Region}:"
                    "${AWS::AccountId}:log-group:"
                    f"{_CLEANUP_LOG_GROUP}:log-stream:*"
                )
            },
        ),
    ]
    all_actions = {action for statement in statements for action in _actions(statement)}
    assert not any(
        action.startswith(
            (
                "bedrock",
                "cloudformation",
                "dynamodb",
                "secretsmanager",
                "sqs",
                "states",
            )
        )
        for action in all_actions
    )


def _expected_command(mode: str, activity_parameter: str) -> list[object]:
    return [
        "python",
        _WORKER_SCRIPT,
        "--mode",
        mode,
        "--activity-arn",
        {"Ref": activity_parameter},
        "--region",
        {"Ref": "AWS::Region"},
        "--lease-table-arn",
        {"Ref": "LeaseTableArn"},
        "--owner-expiry-index-name",
        "owner-expiry",
        "--handler-module",
        _HANDLER_MODULE,
        "--poll-timeout-seconds",
        "70",
        "--api-timeout-seconds",
        "8",
        "--heartbeat-interval-seconds",
        "20",
        "--claim-ttl-seconds",
        "90",
    ]


def test_tasks_are_hardened_non_root_read_only_and_digest_pinned(
    synthesized_template,
):
    entries = _resource_entries(
        synthesized_template,
        "AWS::ECS::TaskDefinition",
    )
    tasks = {resource["Properties"]["Family"]: (logical_id, resource) for logical_id, resource in entries}
    expected = {
        "axonllm-launch-action-worker": (
            "action",
            "ActionActivityArn",
            "ActionTaskRoleArn",
            "ActionWorkerLogGroup",
            _ACTION_LOG_GROUP,
        ),
        "axonllm-launch-cleanup-worker": (
            "cleanup",
            "CleanupActivityArn",
            "CleanupTaskRoleArn",
            "CleanupWorkerLogGroup",
            _CLEANUP_LOG_GROUP,
        ),
    }
    assert set(tasks) == set(expected)

    for family, (
        mode,
        activity_parameter,
        role_parameter,
        log_group_logical_id,
        log_group_name,
    ) in expected.items():
        _, task = tasks[family]
        properties = task["Properties"]
        assert properties["Cpu"] == "256"
        assert properties["Memory"] == "512"
        assert properties["NetworkMode"] == "awsvpc"
        assert properties["RequiresCompatibilities"] == ["FARGATE"]
        assert properties["RuntimePlatform"] == {
            "CpuArchitecture": "X86_64",
            "OperatingSystemFamily": "LINUX",
        }
        assert properties["EnableFaultInjection"] is False
        assert properties["ExecutionRoleArn"] == {"Fn::GetAtt": ["WorkerExecutionRole", "Arn"]}
        assert properties["TaskRoleArn"] == {"Ref": role_parameter}
        assert properties["Volumes"] == [{"Name": "tmp"}]
        assert task["DependsOn"] == [log_group_logical_id]

        assert len(properties["ContainerDefinitions"]) == 1
        container = properties["ContainerDefinitions"][0]
        assert container["Command"] == _expected_command(
            mode,
            activity_parameter,
        )
        assert container["Image"] == {"Ref": "WorkerImageUri"}
        assert container["Name"] == f"launch-{mode}-worker"
        assert container["User"] == "10001:10001"
        assert container["ReadonlyRootFilesystem"] is True
        assert container["Privileged"] is False
        assert container["Interactive"] is False
        assert container["PseudoTerminal"] is False
        assert container["Essential"] is True
        assert container["StopTimeout"] == 120
        assert container["VersionConsistency"] == "enabled"
        assert container["WorkingDirectory"] == "/app"
        assert container["LinuxParameters"] == {
            "Capabilities": {"Drop": ["ALL"]},
            "InitProcessEnabled": True,
        }
        assert container["MountPoints"] == [
            {
                "ContainerPath": "/tmp",
                "ReadOnly": False,
                "SourceVolume": "tmp",
            }
        ]
        assert "Secrets" not in container
        assert "PortMappings" not in container
        assert container["LogConfiguration"] == {
            "LogDriver": "awslogs",
            "Options": {
                "awslogs-create-group": "false",
                "awslogs-group": log_group_name,
                "awslogs-region": {"Ref": "AWS::Region"},
                "awslogs-stream-prefix": mode,
            },
        }
        environment = {item["Name"]: item["Value"] for item in container["Environment"]}
        assert environment == {
            "AWS_DEFAULT_REGION": {"Ref": "AWS::Region"},
            "AWS_REGION": {"Ref": "AWS::Region"},
            "AXON_LAUNCH_REHEARSAL_IDENTITY_SECRET_ARN": {"Ref": "RuntimeIdentitySecretArn"},
                "AXON_LAUNCH_REHEARSAL_TABLE": {"Ref": "RehearsalControlTableArn"},
                "AXON_QUALIFICATION_MUTATION_BROKER_VERSION_ARN": {
                    "Ref": "QualificationMutationBrokerVersionArn"
                },
            "HOME": "/tmp",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "TMPDIR": "/tmp",
        }
        serialized = json.dumps(container)
        assert "{{resolve:" not in serialized
        assert "SecretString" not in serialized


def test_services_stay_private_continuous_and_bounded(
    synthesized_template,
):
    services = _resources(synthesized_template, "AWS::ECS::Service")
    assert len(services) == 2
    assert {service["Properties"]["ServiceName"] for service in services} == {
        "axonllm-launch-action-worker",
        "axonllm-launch-cleanup-worker",
    }
    assert {service["Properties"]["TaskDefinition"]["Ref"] for service in services} == {
        "ActionWorkerTaskDefinition",
        "CleanupWorkerTaskDefinition",
    }

    for service in services:
        properties = service["Properties"]
        assert properties["Cluster"] == {"Ref": "ClusterArn"}
        assert properties["DesiredCount"] == 2
        assert properties["SchedulingStrategy"] == "REPLICA"
        assert properties["AvailabilityZoneRebalancing"] == "ENABLED"
        assert properties["LaunchType"] == "FARGATE"
        assert properties["PlatformVersion"] == "1.4.0"
        assert properties["EnableExecuteCommand"] is False
        assert properties["EnableECSManagedTags"] is True
        assert properties["PropagateTags"] == "TASK_DEFINITION"
        assert properties["DeploymentController"] == {"Type": "ECS"}
        assert properties["DeploymentConfiguration"] == {
            "DeploymentCircuitBreaker": {
                "Enable": True,
                "Rollback": True,
            },
            "MaximumPercent": 200,
            "MinimumHealthyPercent": 100,
        }
        assert properties["NetworkConfiguration"] == {
            "AwsvpcConfiguration": {
                "AssignPublicIp": "DISABLED",
                "SecurityGroups": {"Ref": "SecurityGroupIds"},
                "Subnets": {"Ref": "PrivateSubnetIds"},
            }
        }
        assert "LoadBalancers" not in properties
        assert "Role" not in properties
        assert "ServiceConnectConfiguration" not in properties


def test_namespaced_worker_physical_names_are_isolated(
    namespaced_template,
):
    assert _resources(namespaced_template, "AWS::IAM::Role") == []
    assert namespaced_template["Outputs"]["LaunchWorkerExecutionRoleArn"]["Value"] == {
        "Fn::Sub": ("arn:${AWS::Partition}:iam::${AWS::AccountId}:role/AxonLLMLaunchWorkerExecutionRole-qa-1")
    }

    log_groups = _resources(namespaced_template, "AWS::Logs::LogGroup")
    assert {resource["Properties"]["LogGroupName"] for resource in log_groups} == {
        f"{_ACTION_LOG_GROUP}-qa-1",
        f"{_CLEANUP_LOG_GROUP}-qa-1",
    }

    tasks = _resources(namespaced_template, "AWS::ECS::TaskDefinition")
    assert {task["Properties"]["Family"] for task in tasks} == {
        "axonllm-launch-action-worker-qa-1",
        "axonllm-launch-cleanup-worker-qa-1",
    }
    assert {task["Properties"]["ContainerDefinitions"][0]["Name"] for task in tasks} == {
        "launch-action-worker-qa-1",
        "launch-cleanup-worker-qa-1",
    }
    services = _resources(namespaced_template, "AWS::ECS::Service")
    assert {service["Properties"]["ServiceName"] for service in services} == {
        "axonllm-launch-action-worker-qa-1",
        "axonllm-launch-cleanup-worker-qa-1",
    }

    key = _resources(namespaced_template, "AWS::KMS::Key")[0]
    encryption_contexts = next(
        statement
        for statement in key["Properties"]["KeyPolicy"]["Statement"]
        if statement["Sid"] == "EncryptOnlyExactWorkerLogGroups"
    )["Condition"]["ArnEquals"]["kms:EncryptionContext:aws:logs:arn"]
    assert all("-qa-1" in json.dumps(context) for context in encryption_contexts)


def test_namespaced_worker_resources_are_teardown_capable(
    namespaced_template,
):
    key = _resources(namespaced_template, "AWS::KMS::Key")[0]
    assert key["Properties"]["PendingWindowInDays"] == 7

    log_groups = _resources(namespaced_template, "AWS::Logs::LogGroup")
    assert all(group["Properties"]["DeletionProtectionEnabled"] is False for group in log_groups)

    assert namespaced_template["Resources"]
    for resource in namespaced_template["Resources"].values():
        assert resource["DeletionPolicy"] == "Delete"
        assert resource["UpdateReplacePolicy"] == "Delete"


@pytest.mark.parametrize("namespace", ["a", "a" * 16])
def test_namespace_boundaries_synthesize(
    tmp_path,
    namespace,
):
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    _, template_path = _synth(tmp_path / namespace, namespace=namespace)
    assert template_path.is_file()


@pytest.mark.parametrize(
    "namespace",
    ["A", "-starts", "ends-", "has_underscore", "a" * 17, "two..dots"],
)
def test_invalid_namespaces_fail_before_synthesis(
    tmp_path,
    namespace,
):
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    completed, _ = _run_synth(tmp_path / namespace.replace("/", "_"), namespace=namespace)
    assert completed.returncode != 0
    assert "deployment_namespace must be 1-16 lowercase" in completed.stdout


def test_outputs_expose_worker_operations_identifiers(
    synthesized_template,
):
    assert set(synthesized_template["Outputs"]) == {
        "LaunchActionWorkerLogGroupName",
        "LaunchActionWorkerServiceArn",
        "LaunchActionWorkerServiceName",
        "LaunchActionWorkerTaskDefinitionArn",
        "LaunchCleanupWorkerLogGroupName",
        "LaunchCleanupWorkerServiceArn",
        "LaunchCleanupWorkerServiceName",
        "LaunchCleanupWorkerTaskDefinitionArn",
        "LaunchWorkerExecutionRoleArn",
        "LaunchWorkerImageRepositoryArn",
        "LaunchWorkerImageUri",
        "LaunchWorkerLogKeyArn",
    }
    assert synthesized_template["Outputs"]["LaunchWorkerImageUri"]["Value"] == {"Ref": "WorkerImageUri"}
    assert synthesized_template["Outputs"]["LaunchWorkerImageRepositoryArn"]["Value"] == {
        "Ref": "WorkerImageRepositoryArn"
    }
    assert synthesized_template["Outputs"]["LaunchWorkerExecutionRoleArn"]["Value"] == {
        "Fn::GetAtt": ["WorkerExecutionRole", "Arn"]
    }
