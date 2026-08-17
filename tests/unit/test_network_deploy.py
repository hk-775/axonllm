"""Deployment command binding for verified AgentCore networking."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.gateway.agentcore_setup import AgentCoreSetupConfig
from src.gateway.deployment.agentcore_deploy import (
    AgentCoreDeploymentError,
    ApplicationStateValues,
    IdentityValues,
    agentcore_deploy_command,
    managed_network_deploy_command,
)
from src.gateway.deployment.network_preflight import (
    NetworkPreflightResult,
)


_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_DIGEST = "a" * 64
_CANDIDATE = "candidate_" + "b" * 32


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
                    f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/axonllm/agentcore@sha256:{_DIGEST}"
                ),
                "bedrock_invoke_resource_arns": [
                    "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6"
                ],
                "approved_https_prefix_list_id": ("pl-0123456789abcdef0"),
            },
            "managed_cognito": {
                "hosted_ui_domain_prefix": "axonllm-123456789012",
            },
            "control_plane": {
                "domain_name": "axon.example.com",
                "verified_image_uri": (
                    f"{_ACCOUNT}.dkr.ecr.{_REGION}.amazonaws.com/axonllm/control-plane@sha256:{_DIGEST}"
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


def _identity() -> IdentityValues:
    return IdentityValues(
        issuer=("https://cognito-idp.us-east-1.amazonaws.com/us-east-1_EXAMPLE"),
        discovery_url=(
            "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_EXAMPLE/.well-known/openid-configuration"
        ),
        client_id="runtime-client",
        audience="runtime-client",
        tenant_claim="custom:tenant_id",
        project_claim="custom:project_id",
    )


def _state() -> ApplicationStateValues:
    return ApplicationStateValues(
        stack_name="AxonLLMApplicationStateStack",
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


def _managed_preflight() -> NetworkPreflightResult:
    return NetworkPreflightResult(
        mode="managed",
        egress_mode="managed-nat",
        runtime_context=None,
        managed_stack_context={
            "deployment_profile": "production",
            "managed_network_egress_mode": "managed-nat",
            "managed_network_vpc_cidr": "10.42.0.0/16",
            "managed_network_availability_zones": [
                "us-east-1a",
                "us-east-1c",
            ],
            "managed_network_availability_zone_ids": [
                "use1-az4",
                "use1-az1",
            ],
            "managed_network_nat_gateway_count": 2,
            "managed_network_cost_acknowledgement": True,
        },
        required_services=(),
        approved_https_prefix_list_id="pl-0123456789abcdef0",
    )


def _managed_outputs() -> dict[str, str]:
    return {
        "AvailabilityZoneIds": "use1-az4,use1-az1",
        "AvailabilityZones": "us-east-1a,us-east-1c",
        "DeploymentNamespace": "production",
        "EgressMode": "managed-nat",
        "ManagedNetworkStackName": "AxonLLMManagedNetworkStack",
        "NatGatewayCount": "2",
        "PrivateSubnetIds": ("subnet-0123456789abcdef0,subnet-0fedcba9876543210"),
        "RuntimeSecurityGroupIds": "sg-0123456789abcdef0",
        "VpcCidr": "10.42.0.0/16",
        "VpcId": "vpc-0123456789abcdef0",
    }


def test_managed_network_command_binds_state_and_preflight(
    tmp_path: Path,
) -> None:
    command = managed_network_deploy_command(
        _config(),
        _managed_preflight(),
        _state(),
        outputs_file=tmp_path / "network.json",
        assume_yes=False,
    )

    assert command[2] == "AxonLLMManagedNetworkStack"
    assert "deployment_target=managed-network" in command
    assert "managed_network_egress_mode=managed-nat" in command
    assert ('managed_network_availability_zone_ids=["use1-az4","use1-az1"]') in command
    assert ("AxonLLMManagedNetworkStack:SelectedStateTableName=axonllm-agentcore-state") in command
    assert ("AxonLLMManagedNetworkStack:ApprovedHttpsPrefixListId=pl-0123456789abcdef0") in command
    assert "broadening" in command


def test_agentcore_command_consumes_verified_managed_receipt(
    tmp_path: Path,
) -> None:
    command = agentcore_deploy_command(
        _config(),
        _identity(),
        outputs_file=tmp_path / "runtime.json",
        assume_yes=True,
        candidate_endpoint_name=_CANDIDATE,
        network_preflight=_managed_preflight(),
        managed_network_outputs=_managed_outputs(),
    )

    assert "runtime_network_mode=managed" in command
    assert ('runtime_network_private_subnet_ids=["subnet-0123456789abcdef0","subnet-0fedcba9876543210"]') in command
    assert ('runtime_network_security_group_ids=["sg-0123456789abcdef0"]') in command
    assert not any("AxonLLMAgentCoreStack:ApprovedHttpsPrefixListId=" in item for item in command)


def test_existing_axon_owned_security_group_binds_prefix_list(
    tmp_path: Path,
) -> None:
    preflight = NetworkPreflightResult(
        mode="existing",
        egress_mode="existing-egress",
        runtime_context={
            "deployment_profile": "production",
            "runtime_network_mode": "existing",
            "runtime_network_egress_mode": "existing-egress",
            "runtime_network_vpc_id": "vpc-0123456789abcdef0",
            "runtime_network_vpc_cidr": "10.20.0.0/16",
            "runtime_network_private_subnet_ids": [
                "subnet-0123456789abcdef0",
                "subnet-0fedcba9876543210",
            ],
            "runtime_network_availability_zones": [
                "us-east-1a",
                "us-east-1c",
            ],
            "runtime_network_security_group_ids": [],
        },
        managed_stack_context=None,
        required_services=(),
        approved_https_prefix_list_id="pl-0123456789abcdef0",
    )
    command = agentcore_deploy_command(
        _config(),
        _identity(),
        outputs_file=tmp_path / "runtime.json",
        assume_yes=True,
        candidate_endpoint_name=_CANDIDATE,
        network_preflight=preflight,
    )

    assert ("AxonLLMAgentCoreStack:ApprovedHttpsPrefixListId=pl-0123456789abcdef0") in command


def test_managed_outputs_are_rejected_without_preflight(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        AgentCoreDeploymentError,
        match="require network preflight",
    ):
        agentcore_deploy_command(
            _config(),
            _identity(),
            outputs_file=tmp_path / "runtime.json",
            assume_yes=True,
            candidate_endpoint_name=_CANDIDATE,
            managed_network_outputs=_managed_outputs(),
        )
