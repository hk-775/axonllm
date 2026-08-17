"""Deployment handoff for immutable serverless control-plane artifacts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.gateway.agentcore_setup import AgentCoreSetupConfig
from src.gateway.deployment.agentcore_deploy import (
    AgentCoreDeploymentError,
    ApplicationStateValues,
    AwsIdentity,
    IdentityValues,
    ServerlessControlArtifactValues,
    ServerlessWorkersValues,
    control_plane_deploy_command,
    deployment_names,
    production_edge_values_from_outputs,
    serverless_edge_values_from_outputs,
    serverless_control_plane_deploy_command,
)

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_IMAGE_DIGEST = "a" * 64
_CONTROL_DIGEST = "b" * 64
_STATIC_DIGEST = "c" * 64
_SOURCE_REVISION = "d" * 40


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
                    f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/axonllm/agentcore@sha256:{_IMAGE_DIGEST}"
                ),
                "bedrock_invoke_resource_arns": [
                    "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6"
                ],
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
                "allowed_viewer_cidrs": [
                    "1.1.1.0/24",
                    "8.8.8.8/32",
                ],
            },
        }
    )


def _identity() -> IdentityValues:
    issuer = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_EXAMPLE"
    return IdentityValues(
        issuer=issuer,
        discovery_url=f"{issuer}/.well-known/openid-configuration",
        client_id="runtime-client",
        audience="runtime-client",
        tenant_claim="custom:tenant_id",
        project_claim="custom:project_id",
        user_pool_id="us-east-1_EXAMPLE",
        hosted_ui_domain=("https://axonllm-123456789012.auth.us-east-1.amazoncognito.com"),
        certification_client_id="certification-client",
    )


def _state(
    *,
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


def _workers(
    *,
    stack_name: str = "AxonLLMServerlessWorkersStack",
) -> ServerlessWorkersValues:
    return ServerlessWorkersValues(
        stack_name=stack_name,
        export_queue_url=("https://sqs.us-east-1.amazonaws.com/123456789012/axonllm-exports.fifo"),
        export_queue_arn=("arn:aws:sqs:us-east-1:123456789012:axonllm-exports.fifo"),
        export_bucket_name="axonllm-exports-123456789012",
        export_bucket_arn=("arn:aws:s3:::axonllm-exports-123456789012"),
    )


def _production_edge_outputs() -> dict[str, str]:
    return {
        "EndpointMode": "cloudfront",
        "ControlPlaneAuthMode": "application-oidc",
        "DistributionId": "E1234567890ABC",
        "DistributionDomainName": ("d111111abcdef8.cloudfront.net"),
        "ControlPlaneUrl": ("https://d111111abcdef8.cloudfront.net"),
        "PrimaryStateTableName": "axonllm-agentcore-state",
        "BrowserClientId": "production-browser-client",
        "WebAclArn": (
            "arn:aws:wafv2:us-east-1:123456789012:global/webacl/axonllm/11111111-2222-3333-4444-555555555555"
        ),
    }


def _serverless_edge_outputs() -> dict[str, str]:
    return {
        "EndpointMode": "cloudfront",
        "ControlPlaneAuthMode": "application-oidc",
        "ProductionDistributionArn": ("arn:aws:cloudfront::123456789012:distribution/E1234567890ABC"),
        "ProductionDistributionId": "E1234567890ABC",
        "ProductionControlPlaneHostname": ("d111111abcdef8.cloudfront.net"),
        "PrimaryStateTableName": "axonllm-agentcore-state",
        "SourceRevision": _SOURCE_REVISION,
        "ControlApiArtifactSha256": _CONTROL_DIGEST,
        "StaticAssetsSha256": _STATIC_DIGEST,
        "ControlApiOriginDomainName": ("abc123.execute-api.us-east-1.amazonaws.com"),
        "ControlApiOriginPath": "/prod",
        "OriginCredentialSecretArn": ("arn:aws:secretsmanager:us-east-1:123456789012:secret:axonllm/origin-AbCd12"),
        "StaticSiteBucketRegionalDomainName": ("axonllm-static-123456789012.s3.us-east-1.amazonaws.com"),
        "ControlPlaneUrl": ("https://d222222abcdef8.cloudfront.net"),
        "ControlPlaneDomainName": ("d222222abcdef8.cloudfront.net"),
        "DistributionId": "EABCDEFGHIJKLM",
    }


def test_command_binds_state_identity_and_exact_artifact_versions(
    tmp_path: Path,
) -> None:
    command = serverless_control_plane_deploy_command(
        _config(),
        _identity(),
        _state(),
        _artifacts(),
        _workers(),
        aws_identity=AwsIdentity(
            account_id=_ACCOUNT,
            partition="aws",
        ),
        outputs_file=tmp_path / "serverless.json",
        assume_yes=False,
    )

    stack = "AxonLLMServerlessControlPlaneStack"
    assert command[2] == stack
    assert "deployment_target=serverless-control-plane" in command
    assert f"{stack}:SourceRevision={_SOURCE_REVISION}" in command
    assert f"{stack}:ControlApiCodeObjectVersion=control-version" in command
    assert f"{stack}:StaticAssetsObjectVersion=static-version" in command
    assert (f"{stack}:ApplicationStateStackName=AxonLLMApplicationStateStack") in command
    assert f"{stack}:IdentityUserPoolId=us-east-1_EXAMPLE" in command
    assert (f"{stack}:IdentityHostedUiDomainName=axonllm-123456789012.auth.us-east-1.amazoncognito.com") in command
    assert f"{stack}:AllowedViewerCidrs=1.1.1.0/24,8.8.8.8/32" in command
    assert (f"{stack}:ExportQueueArn=arn:aws:sqs:us-east-1:123456789012:axonllm-exports.fifo") in command
    assert f"{stack}:ExportBucketName=axonllm-exports-123456789012" in command
    assert "broadening" in command
    joined = "\n".join(command).casefold()
    assert "secretstring" not in joined
    assert "dynamicreference" not in joined


def test_edge_receipts_bind_attachment_and_reversible_cutover(
    tmp_path: Path,
) -> None:
    aws_identity = AwsIdentity(
        account_id=_ACCOUNT,
        partition="aws",
    )
    production_edge = production_edge_values_from_outputs(
        _production_edge_outputs(),
        identity=aws_identity,
        expected_stack_name="AxonLLMControlPlaneStack",
    )
    attach = serverless_control_plane_deploy_command(
        _config(),
        _identity(),
        _state(),
        _artifacts(),
        _workers(),
        aws_identity=aws_identity,
        outputs_file=tmp_path / "serverless.json",
        assume_yes=False,
        production_edge=production_edge,
    )

    assert "edge_attachment_enabled=true" in attach
    assert ("AxonLLMServerlessControlPlaneStack:ProductionDistributionId=E1234567890ABC") in attach
    assert ("AxonLLMServerlessControlPlaneStack:ProductionControlPlaneHostname=d111111abcdef8.cloudfront.net") in attach

    serverless_edge = serverless_edge_values_from_outputs(
        _serverless_edge_outputs(),
        identity=aws_identity,
        region=_REGION,
        expected_stack_name=("AxonLLMServerlessControlPlaneStack"),
        production_edge=production_edge,
        artifacts=_artifacts(),
    )
    prepare = control_plane_deploy_command(
        _config(),
        primary_state_table_name="axonllm-agentcore-state",
        outputs_file=tmp_path / "control.json",
        assume_yes=False,
        application_state=_state(),
        serverless_edge=serverless_edge,
        edge_backend_mode="fargate",
        edge_migration_id="e" * 64,
    )

    assert "edge_cutover_enabled=true" in prepare
    assert ("AxonLLMControlPlaneStack:EdgeBackendMode=fargate") in prepare
    assert (f"AxonLLMControlPlaneStack:EdgeMigrationId={'e' * 64}") in prepare
    assert (
        "AxonLLMControlPlaneStack:ServerlessControlApiDomainName=abc123.execute-api.us-east-1.amazonaws.com"
    ) in prepare
    assert (
        "AxonLLMControlPlaneStack:"
        "ServerlessStaticBucketRegionalDomainName="
        "axonllm-static-123456789012.s3.us-east-1.amazonaws.com"
    ) in prepare


def test_edge_handoff_rejects_drift_and_unqualified_selection(
    tmp_path: Path,
) -> None:
    aws_identity = AwsIdentity(
        account_id=_ACCOUNT,
        partition="aws",
    )
    production_edge = production_edge_values_from_outputs(
        _production_edge_outputs(),
        identity=aws_identity,
        expected_stack_name="AxonLLMControlPlaneStack",
    )
    drifted = _serverless_edge_outputs()
    drifted["StaticAssetsSha256"] = "f" * 64

    with pytest.raises(
        AgentCoreDeploymentError,
        match="reviewed production bindings",
    ):
        serverless_edge_values_from_outputs(
            drifted,
            identity=aws_identity,
            region=_REGION,
            expected_stack_name=("AxonLLMServerlessControlPlaneStack"),
            production_edge=production_edge,
            artifacts=_artifacts(),
        )

    with pytest.raises(
        AgentCoreDeploymentError,
        match="requires a qualified serverless edge descriptor",
    ):
        control_plane_deploy_command(
            _config(),
            primary_state_table_name="axonllm-agentcore-state",
            outputs_file=tmp_path / "control.json",
            assume_yes=True,
            edge_backend_mode="serverless",
            edge_migration_id="e" * 64,
        )


def test_edge_handoff_rejects_cross_account_and_state_mismatch(
    tmp_path: Path,
) -> None:
    outputs = _production_edge_outputs()
    outputs["WebAclArn"] = outputs["WebAclArn"].replace(
        _ACCOUNT,
        "999999999999",
    )
    with pytest.raises(
        AgentCoreDeploymentError,
        match="deployment account",
    ):
        production_edge_values_from_outputs(
            outputs,
            identity=AwsIdentity(
                account_id=_ACCOUNT,
                partition="aws",
            ),
            expected_stack_name="AxonLLMControlPlaneStack",
        )

    production_edge = production_edge_values_from_outputs(
        _production_edge_outputs(),
        identity=AwsIdentity(
            account_id=_ACCOUNT,
            partition="aws",
        ),
        expected_stack_name="AxonLLMControlPlaneStack",
    )
    with pytest.raises(
        AgentCoreDeploymentError,
        match="share canonical state",
    ):
        serverless_control_plane_deploy_command(
            _config(),
            _identity(),
            replace(
                _state(),
                state_table_name="other-state",
            ),
            _artifacts(),
            _workers(),
            aws_identity=AwsIdentity(
                account_id=_ACCOUNT,
                partition="aws",
            ),
            outputs_file=tmp_path / "serverless.json",
            assume_yes=True,
            production_edge=production_edge,
        )


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        (
            "artifact_bucket_key_arn",
            "arn:aws:kms:us-east-1:999999999999:key/99999999-2222-3333-4444-555555555555",
            "account and region",
        ),
        (
            "control_api_object_key",
            "v0.3.1/axonllm-control-api-wrong.zip",
            "control API artifact binding",
        ),
        (
            "static_assets_object_version",
            "../latest",
            "static assets artifact binding",
        ),
    ],
)
def test_artifact_binding_rejects_mismatched_publication_receipt(
    tmp_path: Path,
    field: str,
    replacement: str,
    message: str,
) -> None:
    artifacts = replace(_artifacts(), **{field: replacement})

    with pytest.raises(AgentCoreDeploymentError, match=message):
        serverless_control_plane_deploy_command(
            _config(),
            _identity(),
            _state(),
            artifacts,
            _workers(),
            aws_identity=AwsIdentity(
                account_id=_ACCOUNT,
                partition="aws",
            ),
            outputs_file=tmp_path / "serverless.json",
            assume_yes=True,
        )


def test_namespaced_state_cannot_bind_to_another_serverless_stack(
    tmp_path: Path,
) -> None:
    names = deployment_names("managed")
    assert names.serverless_control_plane_stack == ("AxonLLMServerlessControlPlaneStack-managed")

    with pytest.raises(
        AgentCoreDeploymentError,
        match="application-state descriptor",
    ):
        serverless_control_plane_deploy_command(
            _config(),
            _identity(),
            _state(),
            _artifacts(),
            _workers(),
            aws_identity=AwsIdentity(
                account_id=_ACCOUNT,
                partition="aws",
            ),
            outputs_file=tmp_path / "serverless.json",
            assume_yes=True,
            deployment_namespace="managed",
        )


def test_namespaced_workers_must_match_control_plane(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        AgentCoreDeploymentError,
        match="serverless-workers descriptor",
    ):
        serverless_control_plane_deploy_command(
            _config(),
            _identity(),
            _state(stack_name="AxonLLMApplicationStateStack-managed"),
            _artifacts(),
            _workers(),
            aws_identity=AwsIdentity(
                account_id=_ACCOUNT,
                partition="aws",
            ),
            outputs_file=tmp_path / "serverless.json",
            assume_yes=True,
            deployment_namespace="managed",
        )
