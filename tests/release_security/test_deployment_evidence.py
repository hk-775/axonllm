from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "release"))

import deployment_evidence  # noqa: E402


ACCOUNT = "123456789012"
REGION = "us-east-1"
AGENTCORE_DIGEST = "sha256:" + "a" * 64
FARGATE_DIGEST = "sha256:" + "b" * 64
AGENTCORE_IMAGE = (
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
    f"axonllm/agentcore@{AGENTCORE_DIGEST}"
)
FARGATE_IMAGE = (
    f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/"
    f"axonllm/fargate@{FARGATE_DIGEST}"
)
RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
    "runtime/AxonRuntime-1234567890"
)
ENDPOINT_ARN = RUNTIME_ARN + "/runtime-endpoint/production"


def _write(path: Path, value: object) -> Path:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _fixtures(tmp_path: Path) -> argparse.Namespace:
    release = {
        "schema": deployment_evidence.RELEASE_SCHEMA,
        "source": {
            "repository": "owner/repo",
            "commit": "1" * 40,
            "ref": "refs/tags/v1.2.3",
            "workflowRef": (
                "owner/repo/.github/workflows/"
                "release-security.yml@refs/tags/v1.2.3"
            ),
            "runId": "41",
            "runAttempt": "1",
            "eventName": "push",
        },
        "signing": {
            "keyArn": (
                f"arn:aws:kms:{REGION}:{ACCOUNT}:"
                "key/11111111-1111-1111-1111-111111111111"
            )
        },
        "targets": {
            "agentcore": {
                "digest": AGENTCORE_DIGEST,
                "platform": "linux/arm64",
            },
            "fargate": {
                "digest": FARGATE_DIGEST,
                "platform": "linux/amd64",
            },
        },
    }
    identity = {
        deployment_evidence.IDENTITY_STACK: {
            "OidcIssuer": "https://cognito-idp.us-east-1.amazonaws.com/pool",
            "OidcClientId": "client-id",
            "OidcAudience": "client-id",
            "UserPoolId": "us-east-1_pool",
            "AlbClientSecretArn": (
                f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:"
                "secret:alb-client"
            ),
        }
    }
    runtime_outputs = {
        "RuntimeImageUri": AGENTCORE_IMAGE,
        "ProviderSecretVersion": "version-2",
        "RecoveryCutoverMode": "normal",
        "RuntimeEndpointName": "production",
        "RuntimeVersion": "7",
        "StateTableName": "axonllm-agentcore-state",
        "SelectedRuntimeStateTableName": "axonllm-agentcore-state",
        "RecoveryApprovalId": "CHG-123",
        "RuntimeArn": RUNTIME_ARN,
        "RuntimeEndpointArn": ENDPOINT_ARN,
        "ProviderSecretArn": (
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:"
            "secret:providers"
        ),
    }
    runtime = {deployment_evidence.AGENTCORE_STACK: runtime_outputs}
    control_outputs = {
        "AgentCoreStackName": deployment_evidence.AGENTCORE_STACK,
        "PrimaryStateTableName": "axonllm-agentcore-state",
        "SelectedRuntimeStateTableName": "axonllm-agentcore-state",
        "RecoveryCutoverMode": "normal",
        "RecoveryApprovalId": "CHG-123",
        "ClusterName": "cluster",
        "ServiceName": "service",
    }
    control = {deployment_evidence.CONTROL_PLANE_STACK: control_outputs}
    provider = {
        "secretArn": runtime_outputs["ProviderSecretArn"],
        "versionId": "version-2",
        "previousVersionId": "version-1",
        "changed": True,
        "configuredFields": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
        "fingerprint": "c" * 64,
    }
    recovery = {
        "tableArn": (
            f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:"
            "table/axonllm-agentcore-state"
        ),
        "pointInTimeRecovery": "ENABLED",
        "latestRestorableAgeMinutes": 4.0,
        "backupVault": "axon-agent-vault",
        "backupVaultLocked": True,
        "backupVaultLockMode": "GOVERNANCE",
        "backupVaultMinRetentionDays": 30,
        "backupVaultMaxRetentionDays": 365,
        "latestBackupAgeHours": 2.0,
        "restoreExercise": None,
    }
    transition = {
        "phase": "status",
        "approvalId": "CHG-123",
        "mode": "normal",
        "primaryTable": "axonllm-agentcore-state",
        "selectedTable": "axonllm-agentcore-state",
        "quiescedAt": "not-quiesced",
        "minimumQuiescenceSeconds": 14700,
        "endpoint": {
            "name": "production",
            "arn": ENDPOINT_ARN,
            "status": "READY",
            "version": "7",
        },
        "controlPlane": {
            "agentCoreStackName": deployment_evidence.AGENTCORE_STACK,
            "recoveryMode": "normal",
            "selectedTable": "axonllm-agentcore-state",
            "pendingCount": 0,
            "desiredCount": 2,
            "runningCount": 2,
        },
    }
    certification = {
        "schema": deployment_evidence.CERTIFICATION_SCHEMA,
        "generatedAt": "2026-08-11T12:00:00+00:00",
        "overallStatus": "PASS",
        "endpoint": {
            "runtimeArn": RUNTIME_ARN,
            "endpointArn": ENDPOINT_ARN,
            "endpointName": "production",
            "status": "READY",
            "runtimeVersion": "7",
        },
        "summary": {
            "checkCount": 2,
            "passed": 2,
            "failed": 0,
            "providerCount": 1,
            "queryBackendExercised": True,
            "agentcoreHttpsInvoked": True,
        },
        "checks": [
            {"name": "auth", "passed": True},
            {"name": "query", "passed": True},
        ],
    }
    setup = {
        "schema_version": 2,
        "identity_mode": "managed-cognito",
        "aws_region": REGION,
        "runtime": {"verified_image_uri": AGENTCORE_IMAGE},
        "control_plane": {"verified_image_uri": FARGATE_IMAGE},
    }

    return argparse.Namespace(
        output=tmp_path / "deployment.json",
        repository="owner/repo",
        deployment_commit="2" * 40,
        release_commit="1" * 40,
        release_run_id="41",
        release_manifest=_write(tmp_path / "release.json", release),
        workflow_ref=(
            "owner/repo/.github/workflows/"
            "deploy-agentcore-production.yml@refs/heads/main"
        ),
        run_id="99",
        run_attempt="1",
        actor="operator",
        actor_id="123",
        triggering_actor="operator",
        change_id="CHG-123",
        operation="deploy",
        region=REGION,
        agentcore_image=AGENTCORE_IMAGE,
        fargate_image=FARGATE_IMAGE,
        setup_config=_write(tmp_path / "setup.json", setup),
        certification_config=_write(
            tmp_path / "certification-config.json",
            {"scenario": "production"},
        ),
        identity_outputs=_write(tmp_path / "identity.json", identity),
        runtime_outputs=_write(tmp_path / "runtime.json", runtime),
        control_outputs=_write(tmp_path / "control.json", control),
        provider_secret=_write(tmp_path / "provider.json", provider),
        recovery_report=_write(tmp_path / "recovery.json", recovery),
        transition_report=_write(tmp_path / "transition.json", transition),
        certification_report=_write(
            tmp_path / "certification.json",
            certification,
        ),
    )


def test_create_evidence_binds_release_runtime_secret_and_canaries(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)

    evidence = deployment_evidence.create_evidence(args)
    deployment_evidence._atomic_write(args.output, evidence)

    assert evidence["deployment"]["commit"] == "2" * 40
    assert evidence["release"]["commit"] == "1" * 40
    assert evidence["images"]["agentcore"]["digest"] == AGENTCORE_DIGEST
    assert evidence["stacks"]["runtime"]["RuntimeVersion"] == "7"
    assert evidence["providerSecret"]["versionId"] == "version-2"
    assert evidence["recovery"]["transition"]["mode"] == "normal"
    assert evidence["certification"]["overallStatus"] == "PASS"
    assert "AlbClientSecretArn" in evidence["stacks"]["identity"]

    serialized = args.output.read_text(encoding="utf-8")
    assert "actual-provider-secret" not in serialized
    assert stat_mode(args.output) == 0o600


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_verify_rejects_a_different_expected_image(tmp_path: Path) -> None:
    args = _fixtures(tmp_path)
    deployment_evidence._atomic_write(
        args.output,
        deployment_evidence.create_evidence(args),
    )
    verify = argparse.Namespace(
        evidence=args.output,
        repository=args.repository,
        deployment_commit=args.deployment_commit,
        release_commit=args.release_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        agentcore_image=AGENTCORE_IMAGE.replace("a" * 64, "d" * 64),
        fargate_image=FARGATE_IMAGE,
    )

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="images do not match",
    ):
        deployment_evidence.verify_evidence(verify)


def test_create_rejects_failed_certification(tmp_path: Path) -> None:
    args = _fixtures(tmp_path)
    report = json.loads(args.certification_report.read_text(encoding="utf-8"))
    report["overallStatus"] = "FAIL"
    _write(args.certification_report, report)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="certification",
    ):
        deployment_evidence.create_evidence(args)


def test_stack_outputs_reject_secret_values_but_allow_secret_metadata(
    tmp_path: Path,
) -> None:
    args = _fixtures(tmp_path)
    identity = json.loads(args.identity_outputs.read_text(encoding="utf-8"))
    identity[deployment_evidence.IDENTITY_STACK]["ApiKey"] = "must-not-persist"
    _write(args.identity_outputs, identity)

    with pytest.raises(
        deployment_evidence.DeploymentEvidenceError,
        match="unsafe output",
    ):
        deployment_evidence.create_evidence(args)
