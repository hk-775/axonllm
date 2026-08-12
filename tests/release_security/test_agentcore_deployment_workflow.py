from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from scripts.ci.validate_workflows import WorkflowLoader, validate_workflow


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (
    ROOT
    / ".github"
    / "workflows"
    / "deploy-agentcore-production.yml"
)


def _workflow() -> dict[str, Any]:
    value = yaml.load(
        WORKFLOW.read_text(encoding="utf-8"),
        Loader=WorkflowLoader,
    )
    assert isinstance(value, dict)
    return value


def test_production_workflow_uses_oidc_environment_and_signed_targets() -> None:
    workflow = _workflow()
    deploy = workflow["jobs"]["deploy"]

    assert deploy["environment"] == "production"
    assert deploy["permissions"]["id-token"] == "write"
    assert deploy["needs"] == [
        "verify-agentcore",
        "verify-control-plane",
    ]
    assert workflow["jobs"]["verify-agentcore"]["with"]["target"] == "agentcore"
    assert workflow["jobs"]["verify-control-plane"]["with"]["target"] == "fargate"
    assert (
        workflow["jobs"]["verify-agentcore"]["secrets"]["AWS_ROLE_ARN"]
        == "${{ secrets.AXON_RELEASE_VERIFY_ROLE_ARN }}"
    )
    credential_step = next(
        step
        for step in deploy["steps"]
        if step.get("name")
        == "Configure production AWS credentials through OIDC"
    )
    assert credential_step["with"]["role-to-assume"] == (
        "${{ secrets.AXON_AGENTCORE_DEPLOY_ROLE_ARN }}"
    )
    assert credential_step["with"]["allowed-account-ids"] == (
        "${{ vars.AXON_AWS_ACCOUNT_ID }}"
    )
    assert "aws-access-key-id" not in credential_step["with"]


def test_workflow_gates_deployment_before_signed_promotion() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["deploy"]["steps"]
    names = [step["name"] for step in steps]

    assert names.index("Synthesize and validate production infrastructure") < names.index(
        "Deploy identity, runtime, and control plane in order"
    )
    assert names.index("Deploy identity, runtime, and control plane in order") < names.index(
        "Require stable recovery and backup posture"
    )
    assert names.index("Require stable recovery and backup posture") < names.index(
        "Bind and run direct AgentCore certification"
    )
    assert names.index("Bind and run direct AgentCore certification") < names.index(
        "Create and KMS-sign deployment evidence"
    )
    assert names.index("Create and KMS-sign deployment evidence") < names.index(
        "Persist immutable deployment evidence"
    )

    body = WORKFLOW.read_text(encoding="utf-8")
    assert "synthesize_and_verify_cdk.sh" in body
    assert "trivy config" in body
    assert "--require-vault-lock" in body
    assert "agentcore_recovery.py" in body
    assert "certify_agentcore.py" in body
    assert "deployment_evidence.py create" in body
    assert "kms_evidence.py sign" in body
    assert body.count("kms_evidence.py verify") >= 3


def test_workflow_supports_immutable_rollback_without_rebuild() -> None:
    workflow = _workflow()
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    body = WORKFLOW.read_text(encoding="utf-8")

    assert inputs["operation"]["options"] == ["deploy", "rollback"]
    assert "rollback_provider_secret_version" in inputs
    assert "--rollback-provider-secret-version" in body
    assert "verified_image_uri" in body
    assert "docker build" not in body
    assert "buildx" not in body
    assert "latest" not in body


def test_workflow_persists_only_signed_redacted_evidence_with_object_lock() -> None:
    workflow = _workflow()
    deploy = workflow["jobs"]["deploy"]
    body = WORKFLOW.read_text(encoding="utf-8")
    upload = next(
        step
        for step in deploy["steps"]
        if step.get("name") == "Retain signed evidence with the workflow run"
    )

    assert "--object-lock-mode COMPLIANCE" in body
    assert "--if-none-match '*'" in body
    assert "get-object-lock-configuration" in body
    assert "get-bucket-versioning" in body
    assert "get-bucket-encryption" in body
    assert "agentcore-deployment-kms-signature.json" in body
    assert "provider-secret-version.json" in body
    assert "--runtime-outputs" in body
    assert "--certification-report" in body
    assert upload["with"]["retention-days"] == 90
    assert "provider.env" not in upload["with"]["path"]
    assert "certification-credentials" not in upload["with"]["path"]
    assert "AWS_ACCESS_KEY_ID" not in body
    assert "AWS_SECRET_ACCESS_KEY" not in body


def test_production_workflow_passes_repository_policy() -> None:
    assert validate_workflow(WORKFLOW) == 8
