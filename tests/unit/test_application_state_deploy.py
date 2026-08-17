"""Explicit deployment handoff for retained AxonLLM application state."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.gateway.agentcore_setup import AgentCoreSetupConfig
from src.gateway.deployment.agentcore_deploy import (
    AgentCoreDeploymentError,
    AwsIdentity,
    IdentityValues,
    agentcore_deploy_command,
    application_state_deploy_command,
    application_state_values_from_outputs,
    control_plane_deploy_command,
    deployment_names,
)


_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_DIGEST = "a" * 64
_CANDIDATE = "candidate_" + "b" * 32
_STATE_STACK = "AxonLLMApplicationStateStack"
_PRIMARY_TABLE = "axonllm-agentcore-state"
_OUTBOX_NAME = "axonllm-security-events.fifo"
_DLQ_NAME = "axonllm-security-events-dlq.fifo"


def _config() -> AgentCoreSetupConfig:
    return AgentCoreSetupConfig.from_mapping(
        {
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
                    f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/"
                    f"axonllm/agentcore@sha256:{_DIGEST}"
                ),
                "bedrock_invoke_resource_arns": [
                    "arn:aws:bedrock:us-east-1::foundation-model/"
                    "anthropic.claude-sonnet-4-6"
                ],
                "approved_https_prefix_list_id": "pl-123abc",
            },
            "managed_cognito": {
                "hosted_ui_domain_prefix": "axonllm-123456789012",
            },
            "control_plane": {
                "domain_name": "axon.example.com",
                "verified_image_uri": (
                    f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/"
                    f"axonllm/control-plane@sha256:{_DIGEST}"
                ),
                "certificate_arn": (
                    "arn:aws:acm:us-east-1:123456789012:certificate/"
                    "11111111-2222-3333-4444-555555555555"
                ),
                "public_hosted_zone_id": "Z123ABC",
                "approved_ingress_prefix_list_id": "pl-123abc",
                "approved_https_prefix_list_id": "pl-456def",
            },
        }
    )


def _identity() -> IdentityValues:
    return IdentityValues(
        issuer=(
            "https://cognito-idp.us-east-1.amazonaws.com/"
            "us-east-1_EXAMPLE"
        ),
        discovery_url=(
            "https://cognito-idp.us-east-1.amazonaws.com/"
            "us-east-1_EXAMPLE/.well-known/openid-configuration"
        ),
        client_id="runtime-client",
        audience="runtime-client",
        tenant_claim="custom:tenant_id",
        project_claim="custom:project_id",
    )


def _outputs(
    *,
    stack_name: str = _STATE_STACK,
    selected_table: str = _PRIMARY_TABLE,
) -> dict[str, str]:
    return {
        "ApplicationStateStackName": stack_name,
        "StateTableName": _PRIMARY_TABLE,
        "SelectedRuntimeStateTableName": selected_table,
        "DataKeyArn": (
            "arn:aws:kms:us-east-1:123456789012:"
            "key/11111111-2222-3333-4444-555555555555"
        ),
        "RoutingConfigSigningKeyArn": (
            "arn:aws:kms:us-east-1:123456789012:"
            "key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        ),
        "ProviderSecretArn": (
            "arn:aws:secretsmanager:us-east-1:123456789012:"
            "secret:axonllm/providers-AbCd12"
        ),
        "SecurityEventOutboxQueueUrl": (
            f"https://sqs.{_REGION}.amazonaws.com/{_ACCOUNT}/"
            f"{_OUTBOX_NAME}"
        ),
        "SecurityEventOutboxQueueArn": (
            f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:{_OUTBOX_NAME}"
        ),
        "SecurityEventDeadLetterQueueUrl": (
            f"https://sqs.{_REGION}.amazonaws.com/{_ACCOUNT}/"
            f"{_DLQ_NAME}"
        ),
        "SecurityEventDeadLetterQueueArn": (
            f"arn:aws:sqs:{_REGION}:{_ACCOUNT}:{_DLQ_NAME}"
        ),
        "SecurityEventTopicArn": (
            f"arn:aws:sns:{_REGION}:{_ACCOUNT}:"
            "axonllm-security-events.fifo"
        ),
        "SecurityEventLogGroupArn": (
            f"arn:aws:logs:{_REGION}:{_ACCOUNT}:"
            "log-group:/aws/axonllm/security-events"
        ),
        "StateBackupVaultArn": (
            f"arn:aws:backup:{_REGION}:{_ACCOUNT}:"
            "backup-vault:axonllm-state"
        ),
        "StateBackupRoleArn": (
            f"arn:aws:iam::{_ACCOUNT}:role/axonllm-state-backup"
        ),
    }


def _state_values(**kwargs):
    return application_state_values_from_outputs(
        _outputs(**kwargs),
        identity=AwsIdentity(account_id=_ACCOUNT, partition="aws"),
        region=_REGION,
        expected_stack_name=kwargs.get("stack_name", _STATE_STACK),
    )


def test_state_stack_command_is_independent_and_migration_bound(
    tmp_path: Path,
) -> None:
    command = application_state_deploy_command(
        _config(),
        outputs_file=tmp_path / "state.json",
        assume_yes=False,
        backup_vault_name="existing-vault",
        security_event_topic_name="ExistingSecurityEvents.fifo",
    )

    assert command[2] == _STATE_STACK
    assert "deployment_target=application-state" in command
    assert "application_state_backup_vault_name=existing-vault" in command
    assert (
        "application_state_security_event_topic_name="
        "ExistingSecurityEvents.fifo"
    ) in command
    assert "broadening" in command


def test_state_descriptor_binds_agentcore_and_control_plane(
    tmp_path: Path,
) -> None:
    state = _state_values()
    agentcore = agentcore_deploy_command(
        _config(),
        _identity(),
        outputs_file=tmp_path / "runtime.json",
        assume_yes=True,
        candidate_endpoint_name=_CANDIDATE,
        application_state=state,
    )
    control = control_plane_deploy_command(
        _config(),
        primary_state_table_name=_PRIMARY_TABLE,
        outputs_file=tmp_path / "control.json",
        assume_yes=True,
        application_state=state,
    )

    assert "application_state_mode=external" in agentcore
    assert "application_state_mode=external" in control
    for parameter_name, value in state.agentcore_parameters().items():
        assert (
            f"AxonLLMAgentCoreStack:{parameter_name}={value}"
        ) in agentcore
    for parameter_name, value in state.common_parameters().items():
        assert (
            f"AxonLLMControlPlaneStack:{parameter_name}={value}"
        ) in control
    joined = "\n".join([*agentcore, *control]).casefold()
    assert "secretstring" not in joined
    assert "dynamicreference" not in joined


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        (
            "DataKeyArn",
            "arn:aws:kms:us-east-1:999999999999:"
            "key/11111111-2222-3333-4444-555555555555",
        ),
        (
            "SecurityEventTopicArn",
            "arn:aws:sns:us-west-2:123456789012:"
            "axonllm-security-events.fifo",
        ),
        (
            "SecurityEventOutboxQueueUrl",
            "https://sqs.us-east-1.amazonaws.com/999999999999/"
            "axonllm-security-events.fifo",
        ),
    ],
)
def test_state_descriptor_rejects_cross_boundary_identifiers(
    field: str,
    replacement: str,
) -> None:
    outputs = _outputs()
    outputs[field] = replacement

    with pytest.raises(
        AgentCoreDeploymentError,
        match="deployment account and region",
    ):
        application_state_values_from_outputs(
            outputs,
            identity=AwsIdentity(
                account_id=_ACCOUNT,
                partition="aws",
            ),
            region=_REGION,
            expected_stack_name=_STATE_STACK,
        )


def test_state_descriptor_rejects_stack_and_recovery_mismatch(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        AgentCoreDeploymentError,
        match="unexpected stack",
    ):
        application_state_values_from_outputs(
            _outputs(stack_name="OtherStack"),
            identity=AwsIdentity(
                account_id=_ACCOUNT,
                partition="aws",
            ),
            region=_REGION,
            expected_stack_name=_STATE_STACK,
        )

    restored = (
        _PRIMARY_TABLE + "-restore-validation-reviewed"
    )
    state = _state_values(selected_table=restored)
    with pytest.raises(
        AgentCoreDeploymentError,
        match="selected tables do not match",
    ):
        control_plane_deploy_command(
            _config(),
            primary_state_table_name=_PRIMARY_TABLE,
            outputs_file=tmp_path / "control.json",
            assume_yes=True,
            application_state=state,
        )


def test_namespaced_state_descriptor_cannot_cross_deployments(
    tmp_path: Path,
) -> None:
    names = deployment_names("managed")
    state = _state_values()
    assert names.application_state_stack == (
        "AxonLLMApplicationStateStack-managed"
    )

    with pytest.raises(
        AgentCoreDeploymentError,
        match="deployment namespace",
    ):
        agentcore_deploy_command(
            _config(),
            _identity(),
            outputs_file=tmp_path / "runtime.json",
            assume_yes=True,
            candidate_endpoint_name=_CANDIDATE,
            application_state=state,
            deployment_namespace="managed",
            rehearsal_control_table_arn=(
                "arn:aws:dynamodb:us-east-1:123456789012:"
                "table/axonllm-rehearsal-control-ledger"
            ),
        )
