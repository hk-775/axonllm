"""Security contracts for release-foundation deployment automation."""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts.ci.validate_workflows import WorkflowLoader


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-release-foundation.yml"


def _workflow() -> dict:
    loaded = yaml.load(
        WORKFLOW.read_text(encoding="utf-8"),
        Loader=WorkflowLoader,
    )
    assert isinstance(loaded, dict)
    return loaded


def test_foundation_deployment_is_manual_protected_and_oidc_only():
    workflow = _workflow()
    assert set(workflow["on"]) == {"workflow_dispatch"}
    operation = workflow["on"]["workflow_dispatch"]["inputs"]["operation"]
    assert operation["type"] == "choice"
    assert operation["options"] == ["prepare", "execute"]
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["deploy"]
    assert job["environment"] == "release-foundation"
    assert job["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }
    assert job["runs-on"] == {
        "group": "axonllm-production",
        "labels": "axonllm-production-allowlisted",
    }
    serialized = WORKFLOW.read_text(encoding="utf-8")
    assert "refs/heads/main" in serialized
    assert "AXON_RELEASE_FOUNDATION_DEPLOY_ROLE_ARN" in serialized
    assert "aws-actions/configure-aws-credentials@" in serialized
    assert "AWS_ACCESS_KEY_ID" not in serialized
    assert "AWS_SECRET_ACCESS_KEY" not in serialized


def test_foundation_deployment_requires_exact_bounded_bootstrap():
    serialized = WORKFLOW.read_text(encoding="utf-8")
    assert serialized.count(
        "src.gateway.deployment.release_foundation_bootstrap"
    ) == 2
    assert "-c cdk_qualifier=axrel" in serialized
    assert "/cdk-bootstrap/axrel/version" in serialized
    assert "cdk-axrel-cfn-exec-role-" in serialized
    assert "AxonLLMReleaseFoundationRoleBoundary-axrel-us-east-1" in (
        serialized
    )
    assert "AdministratorAccess" not in serialized
    hardened_template_check = serialized.split(
        "Synthesize and validate bounded foundation template",
        maxsplit=1,
    )[1].split(
        "Configure bounded foundation credentials through OIDC",
        maxsplit=1,
    )[0]
    assert 'contains("hnb659fds")' in hardened_template_check
    assert (
        'infra_cfn_lint="${RUNNER_TEMP}/foundation-infra-venv/bin/cfn-lint"'
        in hardened_template_check
    )
    assert '"${infra_cfn_lint}" -i W3005 -t "${template}"' in (
        hardened_template_check
    )
    assert "-m cfnlint" not in hardened_template_check


def test_foundation_deployment_preserves_every_live_parameter():
    serialized = WORKFLOW.read_text(encoding="utf-8")
    parameter_names = {
        "AgentCoreStateTableName",
        "ExternalOidcProviderSourceKmsKeyArn",
        "ExternalOidcProviderSourceSecretArn",
        "FargateStateTableName",
        "GitHubOidcSubjectPrefix",
        "LaunchAlarmEmail",
        "ProductionProviderSourceKmsKeyArn",
        "ProductionProviderSourceSecretArn",
        "QualificationProviderSourceKmsKeyArn",
        "QualificationProviderSourceSecretArn",
    }
    for name in parameter_names:
        assert f"AxonLLMReleaseFoundationStack:{name}=" in serialized
    assert "describe-stacks" in serialized
    assert "UsePreviousValue" not in serialized
    assert 'select(.key != "BootstrapVersion")' in serialized
    assert (
        "AxonLLMReleaseFoundationStack:BootstrapVersion="
        "/cdk-bootstrap/axrel/version"
    ) in serialized
    assert (
        '--change-set-name "AxonLLMReleaseFoundation-${GITHUB_SHA}"'
        in serialized
    )
    assert "--method prepare-change-set" in serialized
    assert "--method execute-change-set" in serialized
    assert "ExecutionStatus == \"AVAILABLE\"" in serialized
    assert "sort_by(.ParameterKey)" in serialized


def test_null_change_set_role_requires_exact_live_stack_role():
    workflow = _workflow()
    steps = workflow["jobs"]["deploy"]["steps"]
    names = {
        "Publish reviewable foundation change set",
        "Validate reviewed foundation change set",
    }
    validation_steps = {
        step["name"]: step["run"]
        for step in steps
        if step.get("name") in names
    }
    assert set(validation_steps) == names
    for script in validation_steps.values():
        assert "aws cloudformation describe-stacks" in script
        assert "--query 'Stacks[0].RoleARN'" in script
        assert '[[ "${live_role}" == "${expected_role}" ]]' in script
        assert "cdk-axrel-cfn-exec-role-" in script
        assert "(.RoleARN == null or .RoleARN == $role)" in script
