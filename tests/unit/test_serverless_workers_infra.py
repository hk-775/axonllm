"""CDK contracts for request-independent serverless workers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "infra"
_INFRA_PYTHON = _INFRA / ".venv" / "bin" / "python"


@pytest.fixture(scope="module")
def serverless_workers_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    work_dir = tmp_path_factory.mktemp("serverless-workers")
    out_dir = work_dir / "cdk.out"
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(
                {
                    "deployment_namespace": "contract",
                    "deployment_target": "serverless-workers",
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
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads((out_dir / "AxonLLMServerlessWorkersStack-contract.template.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def serverless_query_workers_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    work_dir = tmp_path_factory.mktemp("serverless-query-workers")
    out_dir = work_dir / "cdk.out"
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(
                {
                    "athena_query_bindings": json.dumps(
                        [
                            {
                                "project_id": "project-a",
                                "role_arn": ("arn:aws:iam::123456789012:role/axon-query"),
                                "tenant_id": "tenant-a",
                            }
                        ],
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    "deployment_namespace": "contract",
                    "deployment_target": "serverless-workers",
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
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads((out_dir / "AxonLLMServerlessWorkersStack-contract.template.json").read_text(encoding="utf-8"))


def _resources(template: dict, resource_type: str) -> list[tuple[str, dict]]:
    return [
        (logical_id, resource)
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == resource_type
    ]


def _one(template: dict, resource_type: str) -> tuple[str, dict]:
    resources = _resources(template, resource_type)
    assert len(resources) == 1
    return resources[0]


def _function_by_handler(template: dict, handler: str) -> tuple[str, dict]:
    return next(
        item for item in _resources(template, "AWS::Lambda::Function") if item[1]["Properties"]["Handler"] == handler
    )


def test_worker_stack_contains_only_request_independent_compute(
    serverless_workers_template: dict,
) -> None:
    allowed_types = {
        "AWS::IAM::Policy",
        "AWS::IAM::Role",
        "AWS::Lambda::EventSourceMapping",
        "AWS::Lambda::Function",
        "AWS::Logs::LogGroup",
        "AWS::S3::Bucket",
        "AWS::S3::BucketPolicy",
        "AWS::SQS::Queue",
        "AWS::SQS::QueuePolicy",
        "Custom::S3AutoDeleteObjects",
    }
    resource_types = {resource["Type"] for resource in serverless_workers_template["Resources"].values()}

    assert resource_types == allowed_types
    assert "Fn::ImportValue" not in json.dumps(serverless_workers_template)
    assert (
        _resources(
            serverless_workers_template,
            "AWS::Scheduler::Schedule",
        )
        == []
    )
    assert serverless_workers_template["Outputs"]["QueryReconciliationEnabled"]["Value"] == "false"
    assert "QueryReconciliationRoleArn" in (serverless_workers_template["Outputs"])


def test_export_bucket_is_private_short_lived_and_download_only_cors(
    serverless_workers_template: dict,
) -> None:
    _, bucket = _one(serverless_workers_template, "AWS::S3::Bucket")
    properties = bucket["Properties"]

    assert properties["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert properties["OwnershipControls"] == {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
    assert properties["CorsConfiguration"] == {
        "CorsRules": [
            {
                "AllowedMethods": ["GET"],
                "AllowedOrigins": ["*"],
                "ExposedHeaders": [
                    "Content-Disposition",
                    "Content-Length",
                    "ETag",
                ],
                "MaxAge": 300,
            }
        ]
    }
    assert properties["LifecycleConfiguration"]["Rules"] == [
        {
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
            "ExpirationInDays": 1,
            "Status": "Enabled",
        }
    ]


def test_export_storage_uses_the_customer_managed_state_key(
    serverless_workers_template: dict,
) -> None:
    queues = _resources(
        serverless_workers_template,
        "AWS::SQS::Queue",
    )
    _, bucket = _one(
        serverless_workers_template,
        "AWS::S3::Bucket",
    )

    assert len(queues) == 2
    assert all(
        queue["Properties"]["KmsMasterKeyId"]
        == {"Ref": "ApplicationStateDataKeyArn"}
        for _, queue in queues
    )
    assert bucket["Properties"]["BucketEncryption"] == {
        "ServerSideEncryptionConfiguration": [
            {
                "ServerSideEncryptionByDefault": {
                    "KMSMasterKeyID": {
                        "Ref": "ApplicationStateDataKeyArn"
                    },
                    "SSEAlgorithm": "aws:kms",
                }
            }
        ]
    }


def test_worker_uses_exact_arm64_artifact_without_networking(
    serverless_workers_template: dict,
) -> None:
    _, worker = _function_by_handler(
        serverless_workers_template,
        "src.gateway.serverless_workers.security_event_lambda_handler",
    )
    properties = worker["Properties"]

    assert properties["Architectures"] == ["arm64"]
    assert properties["Code"] == {
        "S3Bucket": {"Ref": "ArtifactBucketName"},
        "S3Key": {"Ref": "WorkerCodeObjectKey"},
        "S3ObjectVersion": {"Ref": "WorkerCodeObjectVersion"},
    }
    assert properties["Handler"] == ("src.gateway.serverless_workers.security_event_lambda_handler")
    assert properties["MemorySize"] == 512
    assert properties["ReservedConcurrentExecutions"] == 10
    assert properties["Timeout"] == 45
    assert "VpcConfig" not in properties
    assert properties["Environment"]["Variables"] == {
        "AWS_STS_REGIONAL_ENDPOINTS": "regional",
        "AXON_AWS_ACCOUNT_ID": {"Ref": "AWS::AccountId"},
        "AXON_DEPLOYMENT_PROFILE": "production",
        "AXON_SECURITY_EVENT_LOG_GROUP_ARN": {"Ref": "ApplicationStateSecurityEventLogGroupArn"},
        "AXON_SECURITY_EVENT_SNS_TOPIC_ARN": {"Ref": "ApplicationStateSecurityEventTopicArn"},
        "AXON_SOURCE_REVISION": {"Ref": "SourceRevision"},
        "HOME": "/tmp",
    }


def test_fifo_event_mapping_uses_partial_batch_and_bounded_concurrency(
    serverless_workers_template: dict,
) -> None:
    _, mapping = next(
        item
        for item in _resources(
            serverless_workers_template,
            "AWS::Lambda::EventSourceMapping",
        )
        if item[1]["Properties"]["EventSourceArn"] == {"Ref": "ApplicationStateSecurityEventOutboxQueueArn"}
    )
    properties = mapping["Properties"]

    assert properties["BatchSize"] == 1
    assert properties["Enabled"] is True
    assert properties["EventSourceArn"] == {"Ref": "ApplicationStateSecurityEventOutboxQueueArn"}
    assert properties["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert properties["ScalingConfig"] == {"MaximumConcurrency": 10}


def test_worker_iam_is_limited_to_event_delivery_resources(
    serverless_workers_template: dict,
) -> None:
    _, policy = next(
        item
        for item in _resources(
            serverless_workers_template,
            "AWS::IAM::Policy",
        )
        if "UseSecurityEventOutboxKey" in json.dumps(item[1])
    )
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    by_sid = {statement["Sid"]: statement for statement in statements if "Sid" in statement}
    queue = next(
        statement
        for statement in statements
        if "sqs:ReceiveMessage"
        in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
    )
    topic = next(statement for statement in statements if statement["Action"] == "sns:Publish")
    log_write = next(
        statement
        for statement in statements
        if "logs:PutLogEvents"
        in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
    )

    assert queue["Resource"] == {"Ref": "ApplicationStateSecurityEventOutboxQueueArn"}
    assert set(queue["Action"]) == {
        "sqs:ChangeMessageVisibility",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "sqs:GetQueueUrl",
        "sqs:ReceiveMessage",
    }
    assert topic["Resource"] == {"Ref": "ApplicationStateSecurityEventTopicArn"}
    assert log_write["Resource"]["Fn::Join"][1][-1] == ":*"
    assert by_sid["UseSecurityEventOutboxKey"]["Action"] == "kms:Decrypt"
    assert "sqs.us-east-1." in json.dumps(by_sid["UseSecurityEventOutboxKey"]["Condition"])
    assert set(by_sid["UseSecurityEventTopicKey"]["Action"]) == {
        "kms:Decrypt",
        "kms:GenerateDataKey*",
    }
    assert "aws:sns:topicArn" in json.dumps(by_sid["UseSecurityEventTopicKey"]["Condition"])


def test_worker_parameters_exclude_unneeded_state_and_ui_inputs(
    serverless_workers_template: dict,
) -> None:
    parameters = set(serverless_workers_template["Parameters"])
    required = {
        "ApplicationStateDataKeyArn",
        "ApplicationStateSecurityEventLogGroupArn",
        "ApplicationStateSecurityEventOutboxQueueArn",
        "ApplicationStateSecurityEventTopicArn",
        "ApplicationStateStackName",
        "ArtifactBucketName",
        "SourceRevision",
        "WorkerCodeObjectKey",
        "WorkerCodeObjectVersion",
        "WorkerCodeSha256",
    }
    forbidden = {
        "ApplicationStateRoutingConfigSigningKeyArn",
        "ApplicationStateSecurityEventOutboxQueueUrl",
        "IdentityUserPoolId",
        "StaticAssetsObjectKey",
    }

    assert required.issubset(parameters)
    assert {"PrimaryStateTableName", "RuntimeStateTableName"} <= parameters
    assert parameters.isdisjoint(forbidden)


def test_export_worker_is_bounded_and_uses_ephemeral_private_state(
    serverless_workers_template: dict,
) -> None:
    _, function = _function_by_handler(
        serverless_workers_template,
        "src.gateway.serverless_workers.export_lambda_handler",
    )
    properties = function["Properties"]
    queues = _resources(serverless_workers_template, "AWS::SQS::Queue")
    outbox_id, outbox = next(item for item in queues if "RedrivePolicy" in item[1]["Properties"])
    _, bucket = _one(serverless_workers_template, "AWS::S3::Bucket")
    _, mapping = next(
        item
        for item in _resources(
            serverless_workers_template,
            "AWS::Lambda::EventSourceMapping",
        )
        if item[1]["Properties"]["EventSourceArn"] == {"Fn::GetAtt": [outbox_id, "Arn"]}
    )

    assert properties["Architectures"] == ["arm64"]
    assert properties["Handler"] == ("src.gateway.serverless_workers.export_lambda_handler")
    assert properties["MemorySize"] == 1024
    assert properties["ReservedConcurrentExecutions"] == 2
    assert properties["Timeout"] == 600
    assert properties["EphemeralStorage"] == {"Size": 1024}
    assert "VpcConfig" not in properties
    assert properties["Environment"]["Variables"]["AXON_DYNAMODB_TABLE"] == {
        "Fn::If": [
            "UseRecoveredState",
            {"Ref": "RuntimeStateTableName"},
            {"Ref": "PrimaryStateTableName"},
        ]
    }
    assert outbox["Properties"]["VisibilityTimeout"] == 3600
    assert outbox["Properties"]["MessageRetentionPeriod"] == 86400
    assert outbox["Properties"]["ReceiveMessageWaitTimeSeconds"] == 20
    assert bucket["Properties"]["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert bucket["Properties"]["LifecycleConfiguration"]["Rules"] == [
        {
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
            "ExpirationInDays": 1,
            "Status": "Enabled",
        }
    ]
    assert mapping["Properties"]["BatchSize"] == 1
    assert mapping["Properties"]["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert mapping["Properties"]["ScalingConfig"] == {"MaximumConcurrency": 2}


def test_export_worker_iam_has_no_control_plane_or_provider_access(
    serverless_workers_template: dict,
) -> None:
    function_id, _ = _function_by_handler(
        serverless_workers_template,
        "src.gateway.serverless_workers.export_lambda_handler",
    )
    role_id = serverless_workers_template["Resources"][function_id]["Properties"]["Role"]["Fn::GetAtt"][0]
    policies = [
        policy
        for _, policy in _resources(
            serverless_workers_template,
            "AWS::IAM::Policy",
        )
        if {"Ref": role_id} in policy["Properties"]["Roles"]
    ]
    actions = {
        action
        for policy in policies
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
    }

    assert {
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:UpdateItem",
        "s3:PutObject",
        "sqs:ReceiveMessage",
    } <= actions
    assert not any(action.startswith("cognito-idp:") for action in actions)
    assert not any(action.startswith("secretsmanager:") for action in actions)
    assert not any(action.startswith("bedrock:") for action in actions)
    assert not any(action.startswith("athena:") for action in actions)


def test_query_reconciliation_uses_a_bounded_scheduler_target(
    serverless_query_workers_template: dict,
) -> None:
    functions = _resources(
        serverless_query_workers_template,
        "AWS::Lambda::Function",
    )
    _, query_worker = next(
        item
        for item in functions
        if item[1]["Properties"]["Handler"] == ("src.gateway.serverless_workers.query_reconciliation_lambda_handler")
    )
    _, schedule = _one(
        serverless_query_workers_template,
        "AWS::Scheduler::Schedule",
    )
    properties = query_worker["Properties"]
    target = schedule["Properties"]["Target"]

    assert properties["Architectures"] == ["arm64"]
    assert properties["ReservedConcurrentExecutions"] == 1
    assert properties["Timeout"] == 120
    assert "VpcConfig" not in properties
    assert properties["Environment"]["Variables"]["AXON_QUERY_RECONCILIATION_MAX_PAGES"] == "1"
    assert properties["Environment"]["Variables"]["AXON_QUERY_RECONCILIATION_PAGE_SIZE"] == "2"
    assert schedule["Properties"]["ScheduleExpression"] == "rate(1 minute)"
    assert schedule["Properties"]["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert target["Input"] == ('{"schema":"axonllm.query-reconciliation/v1"}')
    assert target["RetryPolicy"] == {
        "MaximumEventAgeInSeconds": 60,
        "MaximumRetryAttempts": 0,
    }


def test_query_reconciliation_role_is_dedicated_and_least_privilege(
    serverless_query_workers_template: dict,
) -> None:
    roles = _resources(
        serverless_query_workers_template,
        "AWS::IAM::Role",
    )
    query_role_id, query_role = next(
        item for item in roles if item[1]["Properties"].get("RoleName") == "axonllm-query-reconciliation-contract"
    )
    policies = _resources(
        serverless_query_workers_template,
        "AWS::IAM::Policy",
    )
    query_policy = next(policy for _, policy in policies if {"Ref": query_role_id} in policy["Properties"]["Roles"])
    statements = query_policy["Properties"]["PolicyDocument"]["Statement"]
    actions = {
        action
        for statement in statements
        for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
    }
    sts = next(
        statement
        for statement in statements
        if "sts:AssumeRole" in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
    )
    dynamodb = next(
        statement
        for statement in statements
        if "dynamodb:TransactWriteItems"
        in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
    )

    assert query_role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {
        "Service": "lambda.amazonaws.com"
    }
    assert sts["Resource"] == ("arn:aws:iam::123456789012:role/axon-query")
    assert "dynamodb:Scan" in actions
    assert "dynamodb:TransactWriteItems" in actions
    assert not any(action.startswith("athena:") for action in actions)
    assert not any(action.startswith("s3:") for action in actions)
    assert "PrimaryStateTableName" in (serverless_query_workers_template["Parameters"])
    assert "RuntimeStateTableName" in (serverless_query_workers_template["Parameters"])
