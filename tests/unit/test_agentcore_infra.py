"""Synthesized security contracts for the Bedrock AgentCore deployment."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "infra"
_STACK = _INFRA / "agentcore_stack.py"
_INFRA_PYTHON = _INFRA / ".venv" / "bin" / "python"
_DOCKERFILE = _INFRA / "agentcore-image" / "Dockerfile"
_REQUIRED_PARAMETERS = {
    "OidcIssuer",
    "OidcDiscoveryUrl",
    "OidcClientId",
    "OidcAudience",
    "ApprovedHttpsPrefixListId",
    "BedrockInvokeResourceArns",
    "VerifiedImageUri",
}
_BEDROCK_ACTIONS = {
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
}
_SQS_ACTIONS = {
    "sqs:ChangeMessageVisibility",
    "sqs:DeleteMessage",
    "sqs:GetQueueAttributes",
    "sqs:ReceiveMessage",
    "sqs:SendMessage",
}
_DYNAMODB_ACTIONS = {
    "dynamodb:BatchGetItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:ConditionCheckItem",
    "dynamodb:DeleteItem",
    "dynamodb:DescribeTable",
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:UpdateItem",
}


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == resource_type
    ]


def _one_resource(template: dict, resource_type: str) -> dict:
    resources = _resources(template, resource_type)
    assert len(resources) == 1, f"expected one {resource_type}, got {len(resources)}"
    return resources[0]


def _logical_resource(
    template: dict,
    resource_type: str,
    description_prefix: str,
) -> tuple[str, dict]:
    matches = [
        (logical_id, resource)
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == resource_type
        and resource["Properties"].get("GroupDescription", "").startswith(
            description_prefix
        )
    ]
    assert len(matches) == 1
    return matches[0]


def _actions(statement: dict) -> set[str]:
    actions = statement["Action"]
    if isinstance(actions, str):
        return {actions}
    return set(actions)


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


@pytest.fixture(scope="module")
def synthesized(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[dict, dict]:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")

    work_dir = tmp_path_factory.mktemp("agentcore-infra")
    out_dir = work_dir / "cdk.out"
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(
                {
                    "deployment_target": "agentcore",
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
    template = json.loads(
        (out_dir / "AxonLLMAgentCoreStack.template.json").read_text(
            encoding="utf-8"
        )
    )
    assets = json.loads(
        (out_dir / "AxonLLMAgentCoreStack.assets.json").read_text(
            encoding="utf-8"
        )
    )
    return template, assets


@pytest.fixture(scope="module")
def synthesized_template(synthesized: tuple[dict, dict]) -> dict:
    return synthesized[0]


def test_deployment_inputs_are_required_and_bedrock_arns_are_concrete(
    synthesized_template,
):
    parameters = synthesized_template["Parameters"]
    assert _REQUIRED_PARAMETERS <= set(parameters)
    for name in _REQUIRED_PARAMETERS:
        assert "Default" not in parameters[name]

    assert parameters["OidcIssuer"]["AllowedPattern"].startswith("^https://")
    assert parameters["OidcDiscoveryUrl"]["AllowedPattern"].endswith(
        r"/\.well-known/openid-configuration$"
    )
    assert parameters["ApprovedHttpsPrefixListId"]["AllowedPattern"] == (
        "^pl-[0-9a-fA-F]+$"
    )
    assert parameters["VerifiedImageUri"]["AllowedPattern"].endswith(
        r"@sha256:[0-9a-f]{64}$"
    )
    bedrock = parameters["BedrockInvokeResourceArns"]
    assert bedrock["Type"] == "CommaDelimitedList"
    assert "foundation-model" in bedrock["AllowedPattern"]
    assert "inference-profile" in bedrock["AllowedPattern"]
    assert "without wildcards" in bedrock["ConstraintDescription"]


def test_runtime_uses_two_private_azs_and_explicit_security_groups(
    synthesized_template,
):
    assert len(_resources(synthesized_template, "AWS::EC2::NatGateway")) == 2
    subnets = _resources(synthesized_template, "AWS::EC2::Subnet")
    assert len(subnets) == 4
    assert all(
        subnet["Properties"]["MapPublicIpOnLaunch"] is False
        for subnet in subnets
    )

    runtime = _one_resource(
        synthesized_template,
        "AWS::BedrockAgentCore::Runtime",
    )["Properties"]
    network = runtime["NetworkConfiguration"]
    assert network["NetworkMode"] == "VPC"
    config = network["NetworkModeConfig"]
    assert len(config["SecurityGroups"]) == 1
    assert config["SecurityGroups"][0]["Fn::GetAtt"][0].startswith(
        "RuntimeSecurityGroup"
    )
    assert len(config["Subnets"]) == 2
    assert all(
        subnet["Ref"].startswith("VpcRuntimeSubnet")
        for subnet in config["Subnets"]
    )


def test_endpoint_ingress_is_only_from_the_runtime_security_group(
    synthesized_template,
):
    runtime_group_id, runtime_group = _logical_resource(
        synthesized_template,
        "AWS::EC2::SecurityGroup",
        "AxonLLM AgentCore",
    )
    endpoint_group_id, endpoint_group = _logical_resource(
        synthesized_template,
        "AWS::EC2::SecurityGroup",
        "Private AWS service endpoints",
    )
    assert "SecurityGroupIngress" not in endpoint_group["Properties"]

    ingress = _resources(
        synthesized_template,
        "AWS::EC2::SecurityGroupIngress",
    )
    assert len(ingress) == 1
    rule = ingress[0]["Properties"]
    assert rule["Description"] == "HTTPS from the AgentCore runtime"
    assert rule["FromPort"] == rule["ToPort"] == 443
    assert rule["GroupId"] == {
        "Fn::GetAtt": [endpoint_group_id, "GroupId"]
    }
    assert rule["SourceSecurityGroupId"] == {
        "Fn::GetAtt": [runtime_group_id, "GroupId"]
    }

    security_group_resources = {
        logical_id: resource
        for logical_id, resource in synthesized_template["Resources"].items()
        if resource["Type"]
        in {
            "AWS::EC2::SecurityGroup",
            "AWS::EC2::SecurityGroupEgress",
            "AWS::EC2::SecurityGroupIngress",
        }
    }
    assert "0.0.0.0/0" not in _values_for_key(
        security_group_resources,
        "CidrIp",
    )
    assert "::/0" not in _values_for_key(
        security_group_resources,
        "CidrIpv6",
    )

    runtime_egress = {
        resource["Properties"]["Description"]
        for resource in _resources(
            synthesized_template,
            "AWS::EC2::SecurityGroupEgress",
        )
        if resource["Properties"]["GroupId"]["Fn::GetAtt"][0]
        == runtime_group_id
    }
    assert runtime_egress == {
        "HTTPS to explicitly approved external destinations",
        "DynamoDB through the VPC gateway endpoint",
        "AWS services through private interface endpoints",
    }
    assert {
        rule["Description"]
        for rule in runtime_group["Properties"]["SecurityGroupEgress"]
    } == {
        "DNS to the VPC resolver",
        "DNS fallback to the VPC resolver",
    }


def test_service_endpoints_are_private_and_resource_scoped(
    synthesized_template,
):
    endpoints = _resources(synthesized_template, "AWS::EC2::VPCEndpoint")
    assert len(endpoints) == 5
    gateway = next(
        endpoint
        for endpoint in endpoints
        if endpoint["Properties"]["VpcEndpointType"] == "Gateway"
    )["Properties"]
    interfaces = [
        endpoint
        for endpoint in endpoints
        if endpoint["Properties"]["VpcEndpointType"] == "Interface"
    ]
    bedrock_interface = next(
        endpoint["Properties"]
        for endpoint in interfaces
        if endpoint["Properties"]["ServiceName"].endswith(
            ".bedrock-runtime"
        )
    )
    sqs_interface = next(
        endpoint["Properties"]
        for endpoint in interfaces
        if endpoint["Properties"]["ServiceName"].endswith(".sqs")
    )
    sns_interface = next(
        endpoint["Properties"]
        for endpoint in interfaces
        if endpoint["Properties"]["ServiceName"].endswith(".sns")
    )
    logs_interface = next(
        endpoint["Properties"]
        for endpoint in interfaces
        if endpoint["Properties"]["ServiceName"].endswith(".logs")
    )

    assert len(gateway["RouteTableIds"]) == 2
    assert all(
        route["Ref"].startswith("VpcRuntimeSubnet")
        for route in gateway["RouteTableIds"]
    )
    dynamodb_statement = gateway["PolicyDocument"]["Statement"][0]
    assert _actions(dynamodb_statement) == _DYNAMODB_ACTIONS
    assert dynamodb_statement["Resource"][0]["Fn::GetAtt"][1] == "Arn"

    assert len(interfaces) == 4
    for interface in (
        bedrock_interface,
        sqs_interface,
        sns_interface,
        logs_interface,
    ):
        assert interface["PrivateDnsEnabled"] is True
        assert len(interface["SubnetIds"]) == 2
        assert all(
            subnet["Ref"].startswith("VpcRuntimeSubnet")
            for subnet in interface["SubnetIds"]
        )
    bedrock_statement = bedrock_interface["PolicyDocument"]["Statement"][0]
    assert _actions(bedrock_statement) == _BEDROCK_ACTIONS
    assert bedrock_statement["Resource"] == {
        "Ref": "BedrockInvokeResourceArns"
    }
    sqs_statement = sqs_interface["PolicyDocument"]["Statement"][0]
    assert _actions(sqs_statement) == _SQS_ACTIONS
    assert sqs_statement["Resource"]["Fn::GetAtt"][0].startswith(
        "SecurityEventOutboxQueue"
    )
    sns_statement = sns_interface["PolicyDocument"]["Statement"][0]
    assert _actions(sns_statement) == {"sns:Publish"}
    assert sns_statement["Resource"]["Ref"].startswith(
        "SecurityEventTopic"
    )
    logs_statement = logs_interface["PolicyDocument"]["Statement"][0]
    assert _actions(logs_statement) == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }
    assert logs_statement["Resource"][0]["Fn::GetAtt"][0].startswith(
        "SecurityEventLogGroup"
    )
    assert "SecurityEventLogGroup" in json.dumps(
        logs_statement["Resource"][1]
    )


def test_runtime_enforces_jwt_identity_and_bounded_lifecycle(
    synthesized_template,
):
    runtime = _one_resource(
        synthesized_template,
        "AWS::BedrockAgentCore::Runtime",
    )["Properties"]
    assert runtime["AuthorizerConfiguration"] == {
        "CustomJWTAuthorizer": {
            "AllowedAudience": [{"Ref": "OidcAudience"}],
            "AllowedClients": [{"Ref": "OidcClientId"}],
            "DiscoveryUrl": {"Ref": "OidcDiscoveryUrl"},
        }
    }
    assert runtime["RequestHeaderConfiguration"] == {
        "RequestHeaderAllowlist": ["Authorization"]
    }
    assert runtime["LifecycleConfiguration"] == {
        "IdleRuntimeSessionTimeout": 600,
        "MaxLifetime": 14400,
    }
    assert runtime["ProtocolConfiguration"] == "HTTP"

    environment = runtime["EnvironmentVariables"]
    assert environment["AXON_AUTH_MODE"] == "ENFORCE"
    assert environment["AXON_DEPLOYMENT_PROFILE"] == "production"
    assert environment["AXON_LOAD_DEMO_DATA"] == "false"
    assert environment["AXON_OIDC_ISSUER"] == {"Ref": "OidcIssuer"}
    assert environment["AXON_OIDC_AUDIENCE"] == {"Ref": "OidcAudience"}
    assert environment["AXON_REQUIRE_CANONICAL_IDENTITY"] == "true"
    assert environment["AXON_ENABLED_PROVIDERS"] == "bedrock"
    assert environment["LLM_ROUTER_DYNAMODB_ENABLED"] == "true"
    assert environment["AXON_AWS_ACCOUNT_ID"] == {
        "Ref": "AWS::AccountId"
    }
    assert environment["AXON_EVENT_OUTBOX_QUEUE_URL"]["Ref"].startswith(
        "SecurityEventOutboxQueue"
    )
    assert environment["AXON_SECURITY_EVENT_SNS_TOPIC_ARN"][
        "Ref"
    ].startswith("SecurityEventTopic")
    assert environment["AXON_SECURITY_EVENT_LOG_GROUP_ARN"][
        "Fn::GetAtt"
    ][0].startswith("SecurityEventLogGroup")

    endpoint = _one_resource(
        synthesized_template,
        "AWS::BedrockAgentCore::RuntimeEndpoint",
    )["Properties"]
    assert endpoint["Name"] == "production"
    assert endpoint["AgentRuntimeVersion"]["Fn::GetAtt"][1] == (
        "AgentRuntimeVersion"
    )


def test_runtime_role_is_scoped_and_supports_state_transactions(
    synthesized_template,
):
    runtime_policy = next(
        resource
        for logical_id, resource in synthesized_template["Resources"].items()
        if logical_id.startswith("RuntimeExecutionRoleDefaultPolicy")
    )
    statements = runtime_policy["Properties"]["PolicyDocument"]["Statement"]
    bedrock = next(
        statement
        for statement in statements
        if "bedrock:InvokeModel" in _actions(statement)
    )
    assert _actions(bedrock) == _BEDROCK_ACTIONS
    assert bedrock["Resource"] == {"Ref": "BedrockInvokeResourceArns"}

    state_access = next(
        statement
        for statement in statements
        if "dynamodb:ConditionCheckItem" in _actions(statement)
    )
    assert {
        "dynamodb:ConditionCheckItem",
        "dynamodb:DeleteItem",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
    } <= _actions(state_access)
    assert state_access["Resource"][0]["Fn::GetAtt"][0].startswith(
        "StateTable"
    )
    assert state_access["Resource"][0]["Fn::GetAtt"][1] == "Arn"
    all_actions = {
        action
        for statement in statements
        for action in _actions(statement)
    }
    assert "dynamodb:TransactGetItems" not in all_actions
    assert "dynamodb:TransactWriteItems" not in all_actions

    queue_statements = [
        statement
        for statement in statements
        if any(action.startswith("sqs:") for action in _actions(statement))
    ]
    assert len(queue_statements) == 2
    assert _SQS_ACTIONS <= {
        action
        for statement in queue_statements
        for action in _actions(statement)
    }
    assert all(
        statement["Resource"]["Fn::GetAtt"][0].startswith(
            "SecurityEventOutboxQueue"
        )
        for statement in queue_statements
    )
    publish = next(
        statement
        for statement in statements
        if _actions(statement) == {"sns:Publish"}
    )
    assert publish["Resource"]["Ref"].startswith("SecurityEventTopic")
    security_log_write = next(
        statement
        for statement in statements
        if _actions(statement)
        == {"logs:CreateLogStream", "logs:PutLogEvents"}
        and isinstance(statement["Resource"], dict)
        and "Fn::GetAtt" in statement["Resource"]
    )
    assert security_log_write["Resource"]["Fn::GetAtt"][0].startswith(
        "SecurityEventLogGroup"
    )

    image_pull = next(
        statement
        for statement in statements
        if "ecr:BatchGetImage" in _actions(statement)
    )
    assert _actions(image_pull) == {
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
    }
    assert ":repository/" in image_pull["Resource"]["Fn::Join"][1]
    assert "VerifiedImageUri" in json.dumps(image_pull["Resource"])
    image_authorization = next(
        statement
        for statement in statements
        if "ecr:GetAuthorizationToken" in _actions(statement)
    )
    assert image_authorization["Resource"] == "*"

    runtime_role = next(
        resource
        for resource in _resources(synthesized_template, "AWS::IAM::Role")
        if resource["Properties"].get("Description")
        == "Execution role for Bedrock Agent Core Runtime"
    )
    trust = runtime_role["Properties"]["AssumeRolePolicyDocument"][
        "Statement"
    ][0]
    assert trust["Principal"] == {
        "Service": "bedrock-agentcore.amazonaws.com"
    }
    assert trust["Condition"]["StringEquals"]["aws:SourceAccount"] == {
        "Ref": "AWS::AccountId"
    }
    assert "aws:SourceArn" in trust["Condition"]["ArnLike"]


def test_state_and_backups_are_encrypted_retained_and_recoverable(
    synthesized_template,
):
    table = _one_resource(synthesized_template, "AWS::DynamoDB::Table")
    properties = table["Properties"]
    assert properties["DeletionProtectionEnabled"] is True
    assert properties["PointInTimeRecoverySpecification"] == {
        "PointInTimeRecoveryEnabled": True
    }
    assert properties["SSESpecification"]["SSEEnabled"] is True
    assert properties["SSESpecification"]["SSEType"] == "KMS"
    assert properties["TimeToLiveSpecification"] == {
        "AttributeName": "expires_at",
        "Enabled": True,
    }
    assert table["DeletionPolicy"] == "Retain"
    assert table["UpdateReplacePolicy"] == "Retain"

    keys = _resources(synthesized_template, "AWS::KMS::Key")
    assert len(keys) == 2
    assert all(key["Properties"]["EnableKeyRotation"] is True for key in keys)
    assert all(key["DeletionPolicy"] == "Retain" for key in keys)

    vault = _one_resource(
        synthesized_template,
        "AWS::Backup::BackupVault",
    )
    assert vault["DeletionPolicy"] == "Retain"
    assert vault["Properties"]["LockConfiguration"] == {
        "MaxRetentionDays": 365,
        "MinRetentionDays": 30,
    }
    vault_name = vault["Properties"]["BackupVaultName"]
    assert vault_name["Fn::Join"][1][0] == "axon-agent"
    assert {"Ref": "AWS::StackId"} in _values_for_key(
        vault_name,
        "Fn::Split",
    )[0]
    rule = _one_resource(
        synthesized_template,
        "AWS::Backup::BackupPlan",
    )["Properties"]["BackupPlan"]["BackupPlanRule"][0]
    assert rule["ScheduleExpression"] == "cron(30 5 * * ? *)"
    assert rule["Lifecycle"] == {
        "DeleteAfterDays": 365,
        "MoveToColdStorageAfterDays": 30,
    }
    selection = _one_resource(
        synthesized_template,
        "AWS::Backup::BackupSelection",
    )
    assert selection["Properties"]["BackupSelection"]["Resources"][0][
        "Fn::GetAtt"
    ][1] == "Arn"


def test_security_event_outbox_is_fifo_encrypted_and_redriven(
    synthesized_template,
):
    queues = _resources(synthesized_template, "AWS::SQS::Queue")
    assert len(queues) == 2
    assert all(queue["Properties"]["FifoQueue"] is True for queue in queues)
    assert all(
        queue["Properties"]["ContentBasedDeduplication"] is False
        for queue in queues
    )
    assert all("KmsMasterKeyId" in queue["Properties"] for queue in queues)
    assert all(
        queue["Properties"]["MessageRetentionPeriod"] == 1209600
        for queue in queues
    )
    outbox = next(
        queue
        for queue in queues
        if "RedrivePolicy" in queue["Properties"]
    )
    assert outbox["Properties"]["ReceiveMessageWaitTimeSeconds"] == 20
    assert outbox["Properties"]["VisibilityTimeout"] == 120
    assert outbox["Properties"]["RedrivePolicy"]["maxReceiveCount"] == 5
    assert outbox["DeletionPolicy"] == "Retain"
    assert outbox["UpdateReplacePolicy"] == "Retain"

    queue_policies = _resources(
        synthesized_template,
        "AWS::SQS::QueuePolicy",
    )
    assert len(queue_policies) == 2
    for policy in queue_policies:
        statement = policy["Properties"]["PolicyDocument"]["Statement"][0]
        assert statement["Effect"] == "Deny"
        assert statement["Condition"] == {
            "Bool": {"aws:SecureTransport": "false"}
        }

    security_topic = next(
        topic
        for topic in _resources(synthesized_template, "AWS::SNS::Topic")
        if topic["Properties"].get("DisplayName")
        == "AxonLLM AgentCore durable security events"
    )
    assert security_topic["Properties"]["FifoTopic"] is True
    assert security_topic["Properties"]["ContentBasedDeduplication"] is False
    assert "KmsMasterKeyId" in security_topic["Properties"]
    security_topic_policy = next(
        policy
        for policy in _resources(
            synthesized_template,
            "AWS::SNS::TopicPolicy",
        )
        if policy["Properties"]["PolicyDocument"]["Statement"][0].get("Sid")
        == "AllowPublishThroughSSLOnly"
    )
    transport_statement = security_topic_policy["Properties"][
        "PolicyDocument"
    ]["Statement"][0]
    assert transport_statement["Effect"] == "Deny"
    assert transport_statement["Condition"] == {
        "Bool": {"aws:SecureTransport": "false"}
    }

    outputs = synthesized_template["Outputs"]
    assert outputs["SecurityEventOutboxQueueUrl"]["Value"]["Ref"].startswith(
        "SecurityEventOutboxQueue"
    )
    assert outputs["SecurityEventDeadLetterQueueUrl"]["Value"][
        "Ref"
    ].startswith("SecurityEventDeadLetterQueue")
    assert outputs["SecurityEventTopicArn"]["Value"]["Ref"].startswith(
        "SecurityEventTopic"
    )
    assert outputs["SecurityEventLogGroupArn"]["Value"]["Fn::GetAtt"][
        0
    ].startswith("SecurityEventLogGroup")
    assert outputs["DataKeyArn"]["Value"]["Fn::GetAtt"][0].startswith(
        "DataKey"
    )


def test_encrypted_logs_and_alarm_delivery_have_service_permissions(
    synthesized_template,
):
    retained_logs = [
        log_group
        for log_group in _resources(
            synthesized_template,
            "AWS::Logs::LogGroup",
        )
        if log_group["DeletionPolicy"] == "Retain"
    ]
    assert len(retained_logs) == 3
    assert all(
        log_group["Properties"]["RetentionInDays"] == 365
        for log_group in retained_logs
    )
    assert all(
        "KmsKeyId" in log_group["Properties"]
        for log_group in retained_logs
    )
    delivery_types = {
        resource["Properties"]["LogType"]
        for resource in _resources(
            synthesized_template,
            "AWS::Logs::DeliverySource",
        )
    }
    assert delivery_types == {"APPLICATION_LOGS", "TRACES", "USAGE_LOGS"}

    data_key = next(
        key
        for key in _resources(synthesized_template, "AWS::KMS::Key")
        if key["Properties"]["Description"].endswith("state and logs")
    )
    key_statements = data_key["Properties"]["KeyPolicy"]["Statement"]
    statements_by_sid = {
        statement.get("Sid"): statement for statement in key_statements
    }
    assert statements_by_sid["AllowCloudWatchLogsEncryption"][
        "Principal"
    ]["Service"]["Fn::Join"][1] == [
        "logs.us-east-1.",
        {"Ref": "AWS::URLSuffix"},
    ]
    assert statements_by_sid["AllowCloudWatchAlarmEncryption"][
        "Principal"
    ] == {"Service": "cloudwatch.amazonaws.com"}

    topic = next(
        topic
        for topic in _resources(synthesized_template, "AWS::SNS::Topic")
        if topic["Properties"].get("DisplayName")
        == "AxonLLM AgentCore production alarms"
    )
    assert "KmsMasterKeyId" in topic["Properties"]
    topic_policy = next(
        policy
        for policy in _resources(
            synthesized_template,
            "AWS::SNS::TopicPolicy",
        )
        if policy["Properties"]["PolicyDocument"]["Statement"][0].get("Sid")
        == "AllowAccountCloudWatchAlarms"
    )["Properties"]["PolicyDocument"]["Statement"][0]
    assert topic_policy["Action"] == "sns:Publish"
    assert topic_policy["Principal"] == {
        "Service": "cloudwatch.amazonaws.com"
    }
    assert topic_policy["Condition"]["StringEquals"][
        "aws:SourceAccount"
    ] == {"Ref": "AWS::AccountId"}

    alarms = _resources(synthesized_template, "AWS::CloudWatch::Alarm")
    assert len(alarms) == 4
    assert all(alarm["Properties"]["AlarmActions"] for alarm in alarms)
    assert all(alarm["Properties"]["OKActions"] for alarm in alarms)
    assert {
        alarm["Properties"].get("MetricName") for alarm in alarms
    } >= {
        "ApproximateNumberOfMessagesVisible",
        "SystemErrors",
        "Throttles",
    }
    monitored_operations = {
        dimension["Value"]
        for alarm in alarms
        for metric in alarm["Properties"].get("Metrics", [])
        for dimension in metric.get("MetricStat", {})
        .get("Metric", {})
        .get("Dimensions", [])
        if dimension["Name"] == "Operation"
    }
    assert {
        "TransactGetItems",
        "TransactWriteItems",
    } <= monitored_operations
    assert len(
        _resources(synthesized_template, "AWS::CloudWatch::Dashboard")
    ) == 1


def test_runtime_image_is_release_verified_digest_pinned_and_non_root(
    synthesized,
):
    template, assets = synthesized
    assert assets["dockerImages"] == {}

    runtime = _one_resource(
        template,
        "AWS::BedrockAgentCore::Runtime",
    )["Properties"]
    assert runtime["AgentRuntimeArtifact"] == {
        "ContainerConfiguration": {
            "ContainerUri": {"Ref": "VerifiedImageUri"}
        }
    }
    assert template["Outputs"]["RuntimeImageUri"]["Value"] == {
        "Ref": "VerifiedImageUri"
    }
    assert "from_asset" not in _STACK.read_text(encoding="utf-8")

    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    assert (
        "# syntax=docker/dockerfile:1.7@sha256:"
        "a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e"
    ) in dockerfile
    assert (
        "FROM docker.io/library/python:3.12-slim@sha256:"
        "adbc7c33e0abc183557d1d14ce5eb5d261aaadff5451c81a8db636b3ebefcdf6"
    ) in dockerfile
    assert (
        "COPY --from=ghcr.io/astral-sh/uv:0.10.7@sha256:"
        "5fe7b2b0499a485ee86e1e0d2154e1556a08dffeb5f72b204895af5212a2069c"
    ) in dockerfile
    assert "COPY --from=project pyproject.toml uv.lock ./" in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
    assert "UV_NO_CACHE=1" in dockerfile
    assert "HOME=/tmp" in dockerfile
    assert "chmod -R a-w /app" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "/ready" in dockerfile
