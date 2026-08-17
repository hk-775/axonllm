"""Deployment handoff for request-independent serverless workers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.gateway.agentcore_setup import AgentCoreSetupConfig
from src.gateway.deployment.agentcore_deploy import (
    AgentCoreDeploymentError,
    ApplicationStateValues,
    AwsIdentity,
    ServerlessControlArtifactValues,
    deployment_names,
    serverless_workers_deploy_command,
    serverless_workers_values_from_outputs,
)

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_IMAGE_DIGEST = "a" * 64
_CONTROL_DIGEST = "b" * 64
_STATIC_DIGEST = "c" * 64
_SOURCE_REVISION = "d" * 40


def _config(*, athena: bool = False) -> AgentCoreSetupConfig:
    value = {
        "schema_version": 2,
        "target": "agentcore",
        "identity_mode": "managed-cognito",
        "aws_region": _REGION,
        "tenant": {
            "tenant_id": "tenant-a",
            "project_id": "project-a",
            "project_name": "Production",
        },
        "admin": {
            "user_name": "admin@example.com",
            "email": "admin@example.com",
        },
        "runtime": {
            "verified_image_uri": (
                f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/axonllm/agentcore@sha256:{_IMAGE_DIGEST}"
            ),
            "bedrock_invoke_resource_arns": ["arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6"],
            "approved_https_prefix_list_id": "pl-123abc",
        },
        "managed_cognito": {
            "hosted_ui_domain_prefix": "axonllm-123456789012",
        },
        "control_plane": {
            "endpoint_mode": "cloudfront",
            "verified_image_uri": (
                f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/axonllm/control-plane@sha256:{_IMAGE_DIGEST}"
            ),
            "approved_https_prefix_list_id": "pl-456def",
            "allowed_viewer_cidrs": ["1.1.1.0/24"],
        },
    }
    if athena:
        value["runtime"]["athena_query"] = {"role_arns": ["arn:aws:iam::123456789012:role/axon-query"]}
    return AgentCoreSetupConfig.from_mapping(value)


def _state(
    stack_name: str = "AxonLLMApplicationStateStack",
) -> ApplicationStateValues:
    return ApplicationStateValues(
        stack_name=stack_name,
        state_table_name="axonllm-agentcore-state",
        selected_state_table_name="axonllm-agentcore-state",
        data_key_arn=("arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555"),
        routing_config_signing_key_arn=("arn:aws:kms:us-east-1:123456789012:key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        provider_secret_arn=("arn:aws:secretsmanager:us-east-1:123456789012:secret:axonllm/providers-AbCd12"),
        event_outbox_queue_url=("https://sqs.us-east-1.amazonaws.com/123456789012/axonllm-security-events.fifo"),
        event_outbox_queue_arn=("arn:aws:sqs:us-east-1:123456789012:axonllm-security-events.fifo"),
        event_dead_letter_queue_url=(
            "https://sqs.us-east-1.amazonaws.com/123456789012/axonllm-security-events-dlq.fifo"
        ),
        event_dead_letter_queue_arn=("arn:aws:sqs:us-east-1:123456789012:axonllm-security-events-dlq.fifo"),
        security_event_topic_arn=("arn:aws:sns:us-east-1:123456789012:axonllm-security-events.fifo"),
        security_event_log_group_arn=("arn:aws:logs:us-east-1:123456789012:log-group:/aws/axonllm/security-events"),
        backup_vault_arn=("arn:aws:backup:us-east-1:123456789012:backup-vault:axonllm-state"),
        backup_role_arn=("arn:aws:iam::123456789012:role/axonllm-state-backup"),
    )


def _artifacts() -> ServerlessControlArtifactValues:
    return ServerlessControlArtifactValues(
        source_revision=_SOURCE_REVISION,
        artifact_bucket_name="axonllm-release-artifacts",
        artifact_bucket_key_arn=("arn:aws:kms:us-east-1:123456789012:key/99999999-2222-3333-4444-555555555555"),
        control_api_object_key=(f"v0.3.1/axonllm-control-api-{_CONTROL_DIGEST}.zip"),
        control_api_object_version="control-version",
        control_api_sha256=_CONTROL_DIGEST,
        static_assets_object_key=(f"v0.3.1/axonllm-static-assets-{_STATIC_DIGEST}.zip"),
        static_assets_object_version="static-version",
        static_assets_sha256=_STATIC_DIGEST,
    )


def test_command_binds_event_export_state_and_exact_worker_artifact(
    tmp_path: Path,
) -> None:
    command = serverless_workers_deploy_command(
        _config(),
        _state(),
        _artifacts(),
        aws_identity=AwsIdentity(
            account_id=_ACCOUNT,
            partition="aws",
        ),
        outputs_file=tmp_path / "workers.json",
        assume_yes=False,
    )

    stack = "AxonLLMServerlessWorkersStack"
    assert command[2] == stack
    assert "deployment_target=serverless-workers" in command
    assert f"{stack}:SourceRevision={_SOURCE_REVISION}" in command
    assert f"{stack}:WorkerCodeObjectVersion=control-version" in command
    assert (
        f"{stack}:ApplicationStateSecurityEventOutboxQueueArn="
        "arn:aws:sqs:us-east-1:123456789012:"
        "axonllm-security-events.fifo"
    ) in command
    joined = "\n".join(command)
    assert "ApplicationStateSecurityEventOutboxQueueUrl" not in joined
    assert "ApplicationStateRoutingConfigSigningKeyArn" not in joined
    assert f"{stack}:PrimaryStateTableName=axonllm-agentcore-state" in command
    assert f"{stack}:RuntimeStateTableName=" in command
    assert "StaticAssetsObjectKey" not in joined
    assert "ArtifactBucketKeyArn" not in joined
    assert "broadening" in command


def test_namespaced_state_must_match_worker_stack(tmp_path: Path) -> None:
    names = deployment_names("managed")
    assert names.serverless_workers_stack == ("AxonLLMServerlessWorkersStack-managed")

    with pytest.raises(
        AgentCoreDeploymentError,
        match="application-state descriptor",
    ):
        serverless_workers_deploy_command(
            _config(),
            _state(),
            _artifacts(),
            aws_identity=AwsIdentity(
                account_id=_ACCOUNT,
                partition="aws",
            ),
            outputs_file=tmp_path / "workers.json",
            assume_yes=True,
            deployment_namespace="managed",
        )


def test_worker_command_rejects_unverified_artifact_binding(
    tmp_path: Path,
) -> None:
    artifacts = replace(
        _artifacts(),
        control_api_object_key="v0.3.1/not-content-addressed.zip",
    )

    with pytest.raises(
        AgentCoreDeploymentError,
        match="control API artifact binding",
    ):
        serverless_workers_deploy_command(
            _config(),
            _state(),
            artifacts,
            aws_identity=AwsIdentity(
                account_id=_ACCOUNT,
                partition="aws",
            ),
            outputs_file=tmp_path / "workers.json",
            assume_yes=True,
        )


def test_athena_enabled_command_binds_state_table_and_query_context(
    tmp_path: Path,
) -> None:
    command = serverless_workers_deploy_command(
        _config(athena=True),
        _state(),
        _artifacts(),
        aws_identity=AwsIdentity(
            account_id=_ACCOUNT,
            partition="aws",
        ),
        outputs_file=tmp_path / "workers.json",
        assume_yes=True,
    )

    stack = "AxonLLMServerlessWorkersStack"
    assert f"{stack}:PrimaryStateTableName=axonllm-agentcore-state" in command
    assert f"{stack}:RuntimeStateTableName=" in command
    assert any(
        value.startswith("athena_query_bindings=") and "arn:aws:iam::123456789012:role/axon-query" in value
        for value in command
    )


def test_worker_outputs_bind_exact_export_resources() -> None:
    values = serverless_workers_values_from_outputs(
        {
            "ServerlessWorkersStackName": ("AxonLLMServerlessWorkersStack"),
            "ExportQueueUrl": ("https://sqs.us-east-1.amazonaws.com/123456789012/axonllm-exports.fifo"),
            "ExportQueueArn": ("arn:aws:sqs:us-east-1:123456789012:axonllm-exports.fifo"),
            "ExportBucketName": "axonllm-exports-123456789012",
            "ExportBucketArn": ("arn:aws:s3:::axonllm-exports-123456789012"),
        },
        identity=AwsIdentity(
            account_id=_ACCOUNT,
            partition="aws",
        ),
        region=_REGION,
        expected_stack_name="AxonLLMServerlessWorkersStack",
    )

    assert values.control_plane_parameters() == {
        "ExportQueueUrl": ("https://sqs.us-east-1.amazonaws.com/123456789012/axonllm-exports.fifo"),
        "ExportQueueArn": ("arn:aws:sqs:us-east-1:123456789012:axonllm-exports.fifo"),
        "ExportBucketName": "axonllm-exports-123456789012",
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "ServerlessWorkersStackName",
            "OtherStack",
            "unexpected stack",
        ),
        (
            "ExportQueueUrl",
            "https://sqs.us-west-2.amazonaws.com/123456789012/axonllm-exports.fifo",
            "deployment account and region",
        ),
        (
            "ExportQueueArn",
            "arn:aws:sqs:us-east-1:123456789012:other.fifo",
            "ARN and URL do not match",
        ),
        (
            "ExportBucketArn",
            "arn:aws:s3:::other-bucket",
            "does not match its name",
        ),
    ],
)
def test_worker_output_handoff_rejects_mismatches(
    field: str,
    value: str,
    message: str,
) -> None:
    outputs = {
        "ServerlessWorkersStackName": ("AxonLLMServerlessWorkersStack"),
        "ExportQueueUrl": ("https://sqs.us-east-1.amazonaws.com/123456789012/axonllm-exports.fifo"),
        "ExportQueueArn": ("arn:aws:sqs:us-east-1:123456789012:axonllm-exports.fifo"),
        "ExportBucketName": "axonllm-exports-123456789012",
        "ExportBucketArn": ("arn:aws:s3:::axonllm-exports-123456789012"),
    }
    outputs[field] = value

    with pytest.raises(AgentCoreDeploymentError, match=message):
        serverless_workers_values_from_outputs(
            outputs,
            identity=AwsIdentity(
                account_id=_ACCOUNT,
                partition="aws",
            ),
            region=_REGION,
            expected_stack_name="AxonLLMServerlessWorkersStack",
        )
