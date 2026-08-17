"""Prepare-only CDK commands for AgentCore lifecycle changes."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.gateway.agentcore_setup import AgentCoreSetupConfig
from src.gateway.deployment.agentcore_deploy import (
    AgentCoreDeploymentError,
    agentcore_parked_change_set_command,
    managed_network_parked_change_set_command,
    prepare_change_set_command,
)


def _config() -> AgentCoreSetupConfig:
    return AgentCoreSetupConfig.from_mapping(
        {
            "schema_version": 2,
            "target": "agentcore",
            "identity_mode": "managed-cognito",
            "aws_region": "us-east-1",
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
                    f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/agentcore@sha256:{'a' * 64}"
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
                "domain_name": "axon.example.com",
                "verified_image_uri": (
                    f"123456789012.dkr.ecr.us-east-1.amazonaws.com/axonllm/control-plane@sha256:{'b' * 64}"
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


@pytest.mark.parametrize(
    ("builder", "stack_name", "target"),
    [
        (
            agentcore_parked_change_set_command,
            "AxonLLMAgentCoreStack",
            "agentcore-parked",
        ),
        (
            managed_network_parked_change_set_command,
            "AxonLLMManagedNetworkStack",
            "managed-network-parked",
        ),
    ],
)
def test_parked_commands_prepare_but_do_not_execute(
    builder,
    stack_name: str,
    target: str,
) -> None:
    command = builder(
        _config(),
        change_set_name="AxonLLMPark-20260816",
    )

    assert command[1:3] == ["deploy", stack_name]
    assert f"deployment_target={target}" in command
    assert command[command.index("--method") + 1] == ("prepare-change-set")
    assert command[command.index("--change-set-name") + 1] == ("AxonLLMPark-20260816")
    assert command[command.index("--require-approval") + 1] == "never"
    assert "--outputs-file" not in command
    assert "execute-change-set" not in command


def test_parked_command_preserves_qualification_namespace() -> None:
    command = agentcore_parked_change_set_command(
        _config(),
        change_set_name="AxonLLMParkQualification",
        deployment_namespace="qualification",
    )

    assert command[2] == "AxonLLMAgentCoreStack-qualification"
    assert "deployment_namespace=qualification" in command


def test_active_command_can_be_converted_to_prepare_only() -> None:
    command = prepare_change_set_command(
        [
            "/opt/cdk",
            "deploy",
            "AxonLLMAgentCoreStack",
            "--require-approval",
            "broadening",
            "--outputs-file",
            str(Path("/tmp") / "outputs.json"),
        ],
        change_set_name="AxonLLMResume-20260816",
    )

    assert command[:3] == [
        "/opt/cdk",
        "deploy",
        "AxonLLMAgentCoreStack",
    ]
    assert command[command.index("--method") + 1] == ("prepare-change-set")
    assert command[command.index("--change-set-name") + 1] == ("AxonLLMResume-20260816")
    assert command[command.index("--require-approval") + 1] == "never"
    assert "--outputs-file" not in command
    assert "execute-change-set" not in command


@pytest.mark.parametrize(
    "name",
    [
        "",
        "1starts-with-number",
        "contains_underscore",
        "contains space",
        "a" * 129,
    ],
)
def test_invalid_lifecycle_change_set_names_are_rejected(
    name: str,
) -> None:
    with pytest.raises(
        AgentCoreDeploymentError,
        match="lifecycle change-set name",
    ):
        agentcore_parked_change_set_command(
            _config(),
            change_set_name=name,
        )


def test_existing_deployment_method_cannot_be_rewritten() -> None:
    with pytest.raises(
        AgentCoreDeploymentError,
        match="already selects a deployment method",
    ):
        prepare_change_set_command(
            [
                "/opt/cdk",
                "deploy",
                "AxonLLMAgentCoreStack",
                "--method",
                "execute-change-set",
            ],
            change_set_name="AxonLLMResume",
        )
