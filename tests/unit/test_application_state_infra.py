"""Migration guards for the retained application-state stack."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import pytest


_REPO = Path(__file__).resolve().parents[2]
_INFRA = _REPO / "src" / "gateway" / "deployment" / "infra"
_INFRA_PYTHON = _REPO / "infra" / ".venv" / "bin" / "python"
_MIGRATION = (
    _INFRA / "application-state-migration-v1.json"
)
_BACKUP_VAULT_NAME = "axon-agent-existing-vault"
_SECURITY_TOPIC_NAME = "AxonLLMExistingSecurityEvents.fifo"


def _synth(
    tmp_path: Path,
    *,
    target: str,
    state_mode: str | None = None,
) -> tuple[dict, dict]:
    if not _INFRA_PYTHON.is_file():
        pytest.skip("infra/.venv is required for CDK synthesis tests")
    suffix = f"-{state_mode}" if state_mode else ""
    out_dir = tmp_path / f"{target}{suffix}"
    context = {
        "account": "123456789012",
        "application_state_backup_vault_name": (
            _BACKUP_VAULT_NAME
        ),
        "application_state_security_event_topic_name": (
            _SECURITY_TOPIC_NAME
        ),
        "deployment_target": target,
        "region": "us-east-1",
    }
    if state_mode is not None:
        context["application_state_mode"] = state_mode
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(context),
            "CDK_OUTDIR": str(out_dir),
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(
                tmp_path / f"jsii-{target}{suffix}"
            ),
            "PYTHONPYCACHEPREFIX": str(
                tmp_path / f"pycache-{target}{suffix}"
            ),
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
    stack_name = {
        "agentcore": "AxonLLMAgentCoreStack",
        "application-state": "AxonLLMApplicationStateStack",
        "control-plane": "AxonLLMControlPlaneStack",
    }[target]
    template = json.loads(
        (out_dir / f"{stack_name}.template.json").read_text(
            encoding="utf-8"
        )
    )
    manifest = json.loads(
        (out_dir / "manifest.json").read_text(encoding="utf-8")
    )
    return template, manifest


@pytest.fixture(scope="module")
def state_synthesis(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[dict, dict, dict]:
    root = tmp_path_factory.mktemp("application-state")
    agentcore, _ = _synth(root, target="agentcore")
    state, manifest = _synth(root, target="application-state")
    return agentcore, state, manifest


@pytest.fixture(scope="module")
def external_state_synthesis(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[dict, dict]:
    root = tmp_path_factory.mktemp("external-application-state")
    agentcore, _ = _synth(
        root,
        target="agentcore",
        state_mode="external",
    )
    control_plane, _ = _synth(
        root,
        target="control-plane",
        state_mode="external",
    )
    return agentcore, control_plane


def _imports(value: object) -> list[object]:
    if isinstance(value, dict):
        found = [
            child
            for key, child in value.items()
            if key == "Fn::ImportValue"
        ]
        for child in value.values():
            found.extend(_imports(child))
        return found
    if isinstance(value, list):
        found: list[object] = []
        for child in value:
            found.extend(_imports(child))
        return found
    return []


def test_migration_manifest_locks_every_state_resource(
    state_synthesis: tuple[dict, dict, dict],
) -> None:
    _, state, _ = state_synthesis
    migration = json.loads(_MIGRATION.read_text(encoding="utf-8"))
    resources = {
        logical_id: resource["Type"]
        for logical_id, resource in state["Resources"].items()
        if resource["Type"] != "AWS::CDK::Metadata"
    }

    assert migration["schema"] == (
        "axonllm.application-state-migration/v1"
    )
    assert migration["resources"] == resources
    assert migration["physical_name_context"] == {
        "StateBackupVault4657BA35": (
            "application_state_backup_vault_name"
        ),
        "SecurityEventTopicE539676F": (
            "application_state_security_event_topic_name"
        ),
    }


def test_source_and_destination_state_definitions_are_identical(
    state_synthesis: tuple[dict, dict, dict],
) -> None:
    agentcore, state, _ = state_synthesis
    for logical_id, resource in state["Resources"].items():
        if resource["Type"] == "AWS::CDK::Metadata":
            continue
        assert agentcore["Resources"][logical_id] == resource


def test_state_stack_contains_no_runtime_network_or_web_compute(
    state_synthesis: tuple[dict, dict, dict],
) -> None:
    _, state, _ = state_synthesis
    serialized = json.dumps(state, sort_keys=True)
    forbidden = {
        "AWS::BedrockAgentCore::Runtime",
        "AWS::CloudFront::Distribution",
        "AWS::EC2::NatGateway",
        "AWS::EC2::Subnet",
        "AWS::EC2::VPC",
        "AWS::EC2::VPCEndpoint",
        "AWS::ECS::Service",
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "AWS::Lambda::Function",
    }

    assert forbidden.isdisjoint(
        resource["Type"] for resource in state["Resources"].values()
    )
    assert "Fn::ImportValue" not in serialized
    assert all("Export" not in output for output in state["Outputs"].values())


def test_production_state_is_retained_and_deletion_protected(
    state_synthesis: tuple[dict, dict, dict],
) -> None:
    _, state, manifest = state_synthesis
    table = state["Resources"]["StateTable9728C7E5"]
    assert table["Properties"]["DeletionProtectionEnabled"] is True

    retained_types = {
        "AWS::Backup::BackupPlan",
        "AWS::Backup::BackupVault",
        "AWS::DynamoDB::Table",
        "AWS::KMS::Key",
        "AWS::Logs::LogGroup",
        "AWS::Logs::LogStream",
        "AWS::SNS::Topic",
        "AWS::SQS::Queue",
        "AWS::SecretsManager::Secret",
    }
    retained = [
        resource
        for resource in state["Resources"].values()
        if resource["Type"] in retained_types
    ]
    assert retained
    assert all(resource["DeletionPolicy"] == "Retain" for resource in retained)
    assert all(
        resource["UpdateReplacePolicy"] == "Retain"
        for resource in retained
    )

    artifact = manifest["artifacts"]["AxonLLMApplicationStateStack"]
    assert artifact["properties"]["terminationProtection"] is True


def test_state_output_contract_is_explicit_and_non_secret(
    state_synthesis: tuple[dict, dict, dict],
) -> None:
    _, state, _ = state_synthesis

    assert set(state["Outputs"]) == {
        "ApplicationStateStackName",
        "DataKeyArn",
        "ProviderSecretArn",
        "RoutingConfigSigningKeyArn",
        "SecurityEventDeadLetterQueueArn",
        "SecurityEventDeadLetterQueueUrl",
        "SecurityEventLogGroupArn",
        "SecurityEventOutboxQueueArn",
        "SecurityEventOutboxQueueUrl",
        "SecurityEventTopicArn",
        "SelectedRuntimeStateTableName",
        "StateBackupRoleArn",
        "StateBackupVaultArn",
        "StateTableName",
    }
    serialized = json.dumps(state["Outputs"], sort_keys=True).lower()
    assert "secretstring" not in serialized
    assert "dynamicreference" not in serialized


def test_external_agentcore_uses_only_explicit_state_parameters(
    external_state_synthesis: tuple[dict, dict],
) -> None:
    agentcore, _ = external_state_synthesis
    migration = json.loads(_MIGRATION.read_text(encoding="utf-8"))

    assert set(migration["resources"]).isdisjoint(agentcore["Resources"])
    assert _imports(agentcore) == []
    assert all(
        "Export" not in output
        for output in agentcore["Outputs"].values()
    )
    assert {
        "ApplicationStateBackupRoleArn",
        "ApplicationStateBackupVaultArn",
        "ApplicationStateDataKeyArn",
        "ApplicationStateProviderSecretArn",
        "ApplicationStateRoutingConfigSigningKeyArn",
        "ApplicationStateSecurityEventDeadLetterQueueArn",
        "ApplicationStateSecurityEventDeadLetterQueueUrl",
        "ApplicationStateSecurityEventLogGroupArn",
        "ApplicationStateSecurityEventOutboxQueueArn",
        "ApplicationStateSecurityEventOutboxQueueUrl",
        "ApplicationStateSecurityEventTopicArn",
        "ApplicationStateStackName",
        "PrimaryStateTableName",
    } <= set(agentcore["Parameters"])
    assert agentcore["Outputs"]["StateTableName"]["Value"] == {
        "Ref": "PrimaryStateTableName"
    }
    assert agentcore["Outputs"]["ProviderSecretArn"]["Value"] == {
        "Ref": "ApplicationStateProviderSecretArn"
    }
    assert agentcore["Outputs"]["ApplicationStateStackName"]["Value"] == {
        "Ref": "ApplicationStateStackName"
    }


def test_external_control_plane_has_no_agentcore_state_imports(
    external_state_synthesis: tuple[dict, dict],
) -> None:
    _, control_plane = external_state_synthesis
    serialized_imports = json.dumps(
        _imports(control_plane),
        sort_keys=True,
    )

    for state_output in (
        "DataKeyArn",
        "RoutingConfigSigningKeyArn",
        "SecurityEventLogGroupArn",
        "SecurityEventOutboxQueueArn",
        "SecurityEventOutboxQueueUrl",
        "SecurityEventTopicArn",
    ):
        assert state_output not in serialized_imports
    assert {
        "ApplicationStateDataKeyArn",
        "ApplicationStateRoutingConfigSigningKeyArn",
        "ApplicationStateSecurityEventLogGroupArn",
        "ApplicationStateSecurityEventOutboxQueueArn",
        "ApplicationStateSecurityEventOutboxQueueUrl",
        "ApplicationStateSecurityEventTopicArn",
        "ApplicationStateStackName",
    } <= set(control_plane["Parameters"])
    assert control_plane["Outputs"]["ApplicationStateStackName"]["Value"] == {
        "Ref": "ApplicationStateStackName"
    }
    assert not any(
        resource["Type"]
        in {
            "AWS::Backup::BackupPlan",
            "AWS::Backup::BackupVault",
            "AWS::DynamoDB::Table",
            "AWS::KMS::Key",
            "AWS::SecretsManager::Secret",
            "AWS::SQS::Queue",
        }
        for resource in control_plane["Resources"].values()
    )


def test_external_state_parameter_patterns_bind_account_and_region(
    external_state_synthesis: tuple[dict, dict],
) -> None:
    agentcore, _ = external_state_synthesis
    parameters = agentcore["Parameters"]
    examples = {
        "ApplicationStateBackupRoleArn": (
            "arn:aws:iam::123456789012:role/axonllm-backup"
        ),
        "ApplicationStateBackupVaultArn": (
            "arn:aws:backup:us-east-1:123456789012:"
            "backup-vault:axonllm-state"
        ),
        "ApplicationStateDataKeyArn": (
            "arn:aws:kms:us-east-1:123456789012:"
            "key/11111111-2222-3333-4444-555555555555"
        ),
        "ApplicationStateProviderSecretArn": (
            "arn:aws:secretsmanager:us-east-1:123456789012:"
            "secret:axonllm/providers-AbCd12"
        ),
        "ApplicationStateSecurityEventOutboxQueueArn": (
            "arn:aws:sqs:us-east-1:123456789012:"
            "axonllm-security-events.fifo"
        ),
        "ApplicationStateSecurityEventOutboxQueueUrl": (
            "https://sqs.us-east-1.amazonaws.com/123456789012/"
            "axonllm-security-events.fifo"
        ),
        "ApplicationStateSecurityEventTopicArn": (
            "arn:aws:sns:us-east-1:123456789012:"
            "axonllm-security-events.fifo"
        ),
    }
    for parameter_name, example in examples.items():
        assert re.fullmatch(
            parameters[parameter_name]["AllowedPattern"],
            example,
        )


def test_unknown_application_state_mode_fails_closed(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "CDK_CONTEXT_JSON": json.dumps(
                {
                    "account": "123456789012",
                    "application_state_mode": "automatic",
                    "deployment_target": "agentcore",
                    "region": "us-east-1",
                }
            ),
            "CDK_OUTDIR": str(tmp_path / "invalid-mode"),
            "JSII_RUNTIME_PACKAGE_CACHE_ROOT": str(
                tmp_path / "jsii-invalid-mode"
            ),
            "PYTHONPYCACHEPREFIX": str(
                tmp_path / "pycache-invalid-mode"
            ),
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

    assert completed.returncode != 0
    assert (
        "application_state_mode must be 'embedded' or 'external'"
        in completed.stderr
    )
