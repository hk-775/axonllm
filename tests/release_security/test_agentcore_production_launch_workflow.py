"""Structural contracts for the protected AgentCore production launch."""

from __future__ import annotations

from pathlib import Path
import re
import sys
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (
    ROOT
    / ".github"
    / "workflows"
    / "launch-agentcore-production.yml"
)
sys.path.insert(0, str(ROOT / "scripts" / "ci"))

import validate_workflows  # noqa: E402


REUSABLES = {
    "verify-agentcore": "deploy-verification.yml",
    "verify-control-plane": "deploy-verification.yml",
    "external-oidc": "certify-agentcore-external-oidc.yml",
    "launch-gates": "agentcore-launch-gates.yml",
    "rehearsal-evidence": (
        "agentcore-launch-rehearsal-evidence.yml"
    ),
    "production": "deploy-agentcore-production.yml",
}
DISPATCH_INPUTS = {
    "evidence_run_id",
    "release_commit_sha",
    "agentcore_image_reference",
    "control_plane_image_reference",
    "external_oidc_setup_config_s3_version_id",
    "external_oidc_setup_config_sha256",
    "setup_config_s3_version_id",
    "setup_config_sha256",
    "certification_config_s3_version_id",
    "certification_config_sha256",
    "production_validation_config_s3_version_id",
    "production_validation_config_sha256",
    "gate_config_s3_version_id",
    "gate_config_sha256",
    "change_id",
}
QUALIFICATION_ROLE = (
    "${{ secrets.AXON_AGENTCORE_QUALIFICATION_ROLE_ARN }}"
)


def _load(path: Path = WORKFLOW) -> dict[str, Any]:
    loaded = yaml.load(
        path.read_text(encoding="utf-8"),
        Loader=validate_workflows.WorkflowLoader,
    )
    assert isinstance(loaded, dict)
    return loaded


def _steps(job_name: str) -> list[dict[str, Any]]:
    steps = _load()["jobs"][job_name]["steps"]
    assert isinstance(steps, list)
    return steps


def _step(job_name: str, name: str) -> dict[str, Any]:
    matches = [
        step
        for step in _steps(job_name)
        if step.get("name") == name
    ]
    assert len(matches) == 1
    return matches[0]


def _needs(job: dict[str, Any]) -> set[str]:
    value = job.get("needs", [])
    if isinstance(value, str):
        return {value}
    assert isinstance(value, list)
    return set(value)


def _job_body(job_name: str) -> str:
    return "\n".join(
        str(step.get("run", ""))
        for step in _steps(job_name)
    )


def test_is_dispatch_only_and_requires_protected_main() -> None:
    workflow = _load()
    assert set(workflow["on"]) == {"workflow_dispatch"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == DISPATCH_INPUTS
    assert len(inputs) <= 25
    assert all(
        value["required"] in (True, "true")
        and value["type"] == "string"
        for value in inputs.values()
    )
    assert workflow["concurrency"]["group"] == (
        "agentcore-production-launch"
    )
    assert workflow["concurrency"]["cancel-in-progress"] in (
        False,
        "false",
    )
    assert workflow["env"]["AWS_REGION"] == "us-east-1"
    assert workflow["env"]["QUALIFICATION_NAMESPACE"] == "managed"
    assert workflow["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }

    authorize = workflow["jobs"]["authorize"]
    assert authorize["environment"] == "agentcore-qualification"
    body = _job_body("authorize")
    assert '"${GITHUB_EVENT_NAME}" == "workflow_dispatch"' in body
    assert '"${GITHUB_REF}" == "refs/heads/main"' in body
    authorize_step = _step(
        "authorize",
        "Require workflow dispatch from protected main",
    )
    assert authorize_step["env"]["EXPECTED_WORKFLOW_REF"] == (
        "${{ github.repository }}/.github/workflows/"
        "launch-agentcore-production.yml@refs/heads/main"
    )
    assert '"${GITHUB_WORKFLOW_REF}" == "${EXPECTED_WORKFLOW_REF}"' in body


def test_graph_verifies_both_images_before_qualification() -> None:
    jobs = _load()["jobs"]
    assert _needs(jobs["verify-agentcore"]) == {"authorize"}
    assert _needs(jobs["verify-control-plane"]) == {"authorize"}
    verified = {"verify-agentcore", "verify-control-plane"}
    assert _needs(jobs["external-oidc"]) == verified
    assert _needs(jobs["stage-managed"]) == verified
    assert _needs(jobs["launch-gates"]) == {
        "external-oidc",
        "stage-managed",
    }
    assert _needs(jobs["rehearsal-evidence"]) == {
        "launch-gates"
    }
    assert _needs(jobs["teardown"]) == {
        "authorize",
        "external-oidc",
        "stage-managed",
        "launch-gates",
        "rehearsal-evidence",
    }
    assert _needs(jobs["teardown-evidence"]) == {"teardown"}
    assert _needs(jobs["production"]) == {
        "external-oidc",
        "launch-gates",
        "rehearsal-evidence",
        "teardown",
        "teardown-evidence",
    }

    assert jobs["verify-agentcore"]["with"] == {
        "evidence_run_id": "${{ inputs.evidence_run_id }}",
        "commit_sha": "${{ inputs.release_commit_sha }}",
        "image_reference": (
            "${{ inputs.agentcore_image_reference }}"
        ),
        "target": "agentcore",
        "aws_region": "us-east-1",
    }
    assert jobs["verify-control-plane"]["with"] == {
        "evidence_run_id": "${{ inputs.evidence_run_id }}",
        "commit_sha": "${{ inputs.release_commit_sha }}",
        "image_reference": (
            "${{ inputs.control_plane_image_reference }}"
        ),
        "target": "fargate",
        "aws_region": "us-east-1",
    }


def test_every_reusable_call_matches_its_exact_local_interface() -> None:
    jobs = _load()["jobs"]
    for job_name, filename in REUSABLES.items():
        job = jobs[job_name]
        assert job["uses"] == f"./.github/workflows/{filename}"
        called = _load(
            ROOT / ".github" / "workflows" / filename
        )
        call_inputs = called["on"]["workflow_call"]["inputs"]
        assert set(job["with"]) == set(call_inputs)
        assert "secrets" not in job

    external = jobs["external-oidc"]["with"]
    assert external["setup_config_s3_version_id"] == (
        "${{ inputs.external_oidc_setup_config_s3_version_id }}"
    )
    assert external["setup_config_sha256"] == (
        "${{ inputs.external_oidc_setup_config_sha256 }}"
    )
    assert external["certification_config_s3_version_id"] == (
        "${{ inputs.certification_config_s3_version_id }}"
    )
    assert external["certification_config_sha256"] == (
        "${{ inputs.certification_config_sha256 }}"
    )


def test_managed_stage_uses_only_protected_qualification_role() -> None:
    workflow = _load()
    stage = workflow["jobs"]["stage-managed"]
    assert stage["environment"] == "agentcore-qualification"
    assert stage["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }
    credential_steps = [
        step
        for step in stage["steps"]
        if str(step.get("uses", "")).startswith(
            "aws-actions/configure-aws-credentials@"
        )
    ]
    assert len(credential_steps) == 1
    assert (
        credential_steps[0]["with"]["role-to-assume"]
        == QUALIFICATION_ROLE
    )
    stage_text = str(stage)
    secret_expressions = set(
        re.findall(r"\$\{\{\s*secrets\.([A-Z0-9_]+)\s*\}\}", stage_text)
    )
    assert secret_expressions == {
        "AXON_AGENTCORE_QUALIFICATION_ROLE_ARN"
    }

    validate = _step(
        "stage-managed",
        "Validate protected immutable inputs",
    )["run"]
    assert '"${QUALIFICATION_NAMESPACE}" == "managed"' in validate
    assert "VERIFIED_AGENTCORE_IMAGE" in validate
    assert "VERIFIED_CONTROL_PLANE_IMAGE" in validate
    assert (
        "${{ needs.verify-agentcore.outputs.verified_image }}"
        in stage_text
    )
    assert (
        "${{ needs.verify-control-plane.outputs.verified_image }}"
        in stage_text
    )
    install = _step(
        "stage-managed",
        "Install locked qualification dependencies",
    )["run"]
    assert "--extra dev" in install
    assert "playwright install chromium" in install


def test_stage_fetches_every_reviewed_contract_by_version_and_hash() -> None:
    fetch = _step(
        "stage-managed",
        "Fetch exact reviewed qualification contracts",
    )
    environment = fetch["env"]
    expected = {
        "SETUP_CONFIG_VERSION_ID": (
            "${{ inputs.setup_config_s3_version_id }}"
        ),
        "SETUP_CONFIG_SHA256": (
            "${{ inputs.setup_config_sha256 }}"
        ),
        "CERTIFICATION_CONFIG_VERSION_ID": (
            "${{ inputs.certification_config_s3_version_id }}"
        ),
        "CERTIFICATION_CONFIG_SHA256": (
            "${{ inputs.certification_config_sha256 }}"
        ),
        "PRODUCTION_VALIDATION_CONFIG_VERSION_ID": (
            "${{ inputs.production_validation_config_s3_version_id }}"
        ),
        "PRODUCTION_VALIDATION_CONFIG_SHA256": (
            "${{ inputs.production_validation_config_sha256 }}"
        ),
        "GATE_CONFIG_VERSION_ID": (
            "${{ inputs.gate_config_s3_version_id }}"
        ),
        "GATE_CONFIG_SHA256": (
            "${{ inputs.gate_config_sha256 }}"
        ),
    }
    for name, value in expected.items():
        assert environment[name] == value
    body = fetch["run"]
    assert "--version-id \"${version_id}\"" in body
    assert "--expected-bucket-owner \"${AWS_ACCOUNT_ID}\"" in body
    assert "--checksum-mode ENABLED" in body
    assert "sha256sum --check --status" in body
    assert body.count("fetch_exact \\") == 4


def test_stage_deploys_certifies_and_promotes_only_managed_stacks() -> None:
    body = _job_body("stage-managed")
    assert (
        '--deployment-namespace "${QUALIFICATION_NAMESPACE}"'
        in body
    )
    assert (
        '--rehearsal-control-table-arn "${ledger}"'
        in body
    )
    for stack in (
        "AxonLLMIdentityStack-${QUALIFICATION_NAMESPACE}",
        "AxonLLMAgentCoreStack-${QUALIFICATION_NAMESPACE}",
        "AxonLLMControlPlaneStack-${QUALIFICATION_NAMESPACE}",
        "AxonLLMLaunchWorkersStack-${QUALIFICATION_NAMESPACE}",
    ):
        assert stack in body
    assert "prepare_agentcore_certification.py prepare" in body
    assert "scripts/operations/certify_agentcore.py" in body
    certification = _step(
        "stage-managed",
        "Prepare and certify managed qualification candidate",
    )["run"]
    assert 'overallStatus == "PASS"' in certification
    assert 'overallStatus == "passed"' not in certification
    assert body.index(
        "--prepare-candidate-promotion-version"
    ) < body.index("--promote-candidate-version")
    assert body.index(
        "--promote-candidate-version"
    ) < body.index("--deploy-control-plane")
    assert "RuntimeEndpointName == \"production\"" in body
    assert "AxonLLMAgentCoreStack-managed" not in body
    assert "AxonLLMControlPlaneStack-managed" not in body


def test_launch_workers_use_control_plane_and_foundation_outputs() -> None:
    step = _step(
        "stage-managed",
        "Deploy namespaced launch workers on control-plane networking",
    )
    body = step["run"]
    for output in (
        "ClusterArn",
        "SubnetIds",
        "TaskSecurityGroupId",
    ):
        assert output in body
    for output in (
        "LaunchCoordinatorActionActivityArn",
        "LaunchCoordinatorCleanupActivityArn",
        "LaunchCoordinatorActionWorkerRoleArn",
        "LaunchCoordinatorCleanupWorkerRoleArn",
        "LaunchCoordinatorLeaseTableArn",
        "RehearsalControlLedgerTableArn",
        "LaunchRuntimeIdentitySecretArn",
    ):
        assert output in body
    for parameter in (
        "ClusterArn",
        "PrivateSubnetIds",
        "SecurityGroupIds",
        "ActionActivityArn",
        "CleanupActivityArn",
        "LeaseTableArn",
        "RehearsalControlTableArn",
        "RuntimeIdentitySecretArn",
        "ActionTaskRoleArn",
        "CleanupTaskRoleArn",
        "WorkerImageRepositoryArn",
        "WorkerImageUri",
    ):
        assert f":{parameter}=" in body
    assert (
        "WorkerImageUri=${CONTROL_PLANE_IMAGE}"
        in body
    )
    assert "aws ecs wait services-stable" in body


def test_managed_stage_runs_reviewed_control_plane_validation() -> None:
    names = [
        step["name"]
        for step in _steps("stage-managed")
        if "name" in step
    ]
    prepare = names.index(
        "Prepare managed control-plane canary sessions"
    )
    validate = names.index(
        "Run managed control-plane RBAC and load validation"
    )
    cleanup = names.index(
        "Remove managed control-plane canary sessions"
    )
    workers = names.index(
        "Deploy namespaced launch workers on control-plane networking"
    )
    assert prepare < validate < cleanup < workers

    prepare_body = _step(
        "stage-managed",
        "Prepare managed control-plane canary sessions",
    )["run"]
    assert "parse_config" in prepare_body
    assert "prepare_sessions" in prepare_body
    assert "tenant_admin_mutation_round_trip" in prepare_body
    assert "viewer_mutation_denied" in prepare_body
    assert "control-plane-canary-sessions.json" in prepare_body
    for binding in (
        "EndpointMode",
        "ControlPlaneUrl",
        "ControlPlaneDomainName",
        "ControlPlaneAuthMode",
        "alb-cognito",
        "application-oidc",
        "alb-session-cookie",
        "browser-session-cookie",
    ):
        assert binding in prepare_body
    assert "deployment_control_plane_domain" in prepare_body
    assert 'os.environ["QUALIFICATION_NAMESPACE"]' in prepare_body

    validation_body = _step(
        "stage-managed",
        "Run managed control-plane RBAC and load validation",
    )["run"]
    assert "run_production_validation.py" in validation_body
    assert "--target-group-arn" in validation_body
    assert 'overallStatus == "PASS"' in validation_body
    assert "setup_mode = control.get(" in validation_body
    assert '"endpoint_mode",' in validation_body
    assert '"custom-domain"' in validation_body
    assert '"cloudfront"' in validation_body
    assert 'deployed_control.get("ControlPlaneUrl")' in validation_body
    assert "deployment_control_plane_domain" in validation_body
    assert 'os.environ["QUALIFICATION_NAMESPACE"]' in validation_body
    assert '"--base-url",' in validation_body
    assert "control_url," in validation_body
    assert 'control_url = f"https://' not in validation_body

    cleanup_step = _step(
        "stage-managed",
        "Remove managed control-plane canary sessions",
    )
    assert "always()" in cleanup_step["if"]
    assert (
        "prepare_control_plane_canary_sessions.py"
        in cleanup_step["run"]
    )


def test_reviewed_gate_config_is_bound_to_every_live_resource() -> None:
    step = _step(
        "stage-managed",
        "Require reviewed gate bindings match staged resources",
    )
    assert step["env"]["AWS_ACCOUNT_ID"] == (
        "${{ vars.AXON_AWS_ACCOUNT_ID }}"
    )
    body = step["run"]
    for output in (
        "SecurityEventOutboxQueueArn",
        "SecurityEventOutboxQueueUrl",
        "SecurityEventDeadLetterQueueArn",
        "SecurityEventDeadLetterQueueUrl",
        "SecurityEventDeadLettersAlarmArn",
        "SecurityEventLogGroupArn",
        "LaunchCoordinatorExecutionRoleArn",
        "AgentCoreLaunchGatesRoleArn",
        "LaunchCoordinatorWatchdogAlarmArn",
        "LaunchCoordinatorKeyArn",
    ):
        assert output in body
    for binding in (
        ".accountId == $account_id",
        ".resources.restoredStateTableArn",
        ".resources.outboxQueueArn == $outbox_arn",
        ".resources.deadLetterQueueArn == $dead_letter_arn",
        ".resources.deadLetterAlarmArn == $dead_letter_alarm",
        ".resources.securityEventLogGroupArn",
        ".coordinator.executionRoleArn",
        ".coordinator.launchRoleArn",
        ".coordinator.watchdogAlarmArn",
        ".coordinator.kmsKeyArn",
        ".scenario.tenantId == $setup[0].tenant.tenant_id",
        ".scenario.projectId == $setup[0].tenant.project_id",
        ".scenario.datasourceId",
        ".scenario.selectSql",
        "$gate.scenario.primaryProvider",
        "$gate.scenario.fallbackProvider",
    ):
        assert binding in body
    assert "--slurpfile setup" in body
    assert "--slurpfile certification" in body
    assert "AGENTCORE_IMAGE" not in body
    assert "CONTROL_PLANE_IMAGE" not in body


def test_stage_installs_only_a_live_bounded_jwt() -> None:
    step = _step(
        "stage-managed",
        "Install live qualification identity",
    )
    body = step["run"]
    assert '"token": token' in body
    assert '"expiresAtEpoch": expires_at' in body
    assert "(4 * 60 * 60) + (15 * 60)" in body
    assert "qualification JWT does not cover the 240-minute gate" in body
    assert "--secret-string \"file://${work}/runtime-identity.json\"" in body
    assert "LaunchRuntimeIdentitySecretArn" in body
    assert "rm -f \"${work}/runtime-identity.json\"" in body


def test_gate_outputs_are_immutably_bound_into_evidence() -> None:
    jobs = _load()["jobs"]
    gates = jobs["launch-gates"]["with"]
    assert gates == {
        "release_commit_sha": (
            "${{ inputs.release_commit_sha }}"
        ),
        "agentcore_image_reference": (
            "${{ inputs.agentcore_image_reference }}"
        ),
        "control_plane_image_reference": (
            "${{ inputs.control_plane_image_reference }}"
        ),
        "gate_config_s3_version_id": (
            "${{ inputs.gate_config_s3_version_id }}"
        ),
        "gate_config_sha256": (
            "${{ inputs.gate_config_sha256 }}"
        ),
        "aws_region": "us-east-1",
    }
    evidence = jobs["rehearsal-evidence"]["with"]
    output_bindings = {
        "gate_manifest_s3_version_id": (
            "gate_manifest_version_id"
        ),
        "gate_manifest_sha256": "gate_manifest_sha256",
        "gate_manifest_signature_s3_version_id": (
            "gate_manifest_signature_version_id"
        ),
        "gate_manifest_signature_sha256": (
            "gate_manifest_signature_sha256"
        ),
    }
    for input_name, output_name in output_bindings.items():
        assert evidence[input_name] == (
            "${{ needs.launch-gates.outputs."
            + output_name
            + " }}"
        )


def test_teardown_is_always_protected_ordered_and_fail_closed() -> None:
    workflow = _load()
    teardown = workflow["jobs"]["teardown"]
    assert "always()" in teardown["if"]
    assert "needs.authorize.result == 'success'" in teardown["if"]
    assert (
        "needs.stage-managed.result != 'skipped'"
        in teardown["if"]
    )
    assert teardown["environment"] == "agentcore-qualification"
    assert teardown["outputs"]["succeeded"] == (
        "${{ steps.cleanup.outputs.succeeded }}"
    )
    credential_steps = [
        step
        for step in teardown["steps"]
        if str(step.get("uses", "")).startswith(
            "aws-actions/configure-aws-credentials@"
        )
    ]
    assert len(credential_steps) == 1
    assert (
        credential_steps[0]["with"]["role-to-assume"]
        == QUALIFICATION_ROLE
    )

    body = _step(
        "teardown",
        "Scale down, revoke, remove fixtures, and destroy stacks",
    )["run"]
    scale = body.index("--desired-count 0")
    revoke = body.index('{"token":"revoked","expiresAtEpoch":0}')
    fixtures = body.index(
        "prepare_agentcore_certification.py"
    )
    workers = body.rindex('delete_stack "${worker_stack}"')
    control = body.rindex('delete_stack "${control_stack}"')
    runtime = body.rindex('delete_stack "${runtime_stack}"')
    identity = body.rindex('delete_stack "${identity_stack}"')
    assert scale < revoke < fixtures
    assert fixtures < workers < control < runtime < identity
    assert "control-plane-canary-sessions.json" in body
    assert "prepare_control_plane_canary_sessions.py" in body
    assert "failures=0" in body
    assert "succeeded=false" in body
    assert "exit 1" in body


def test_teardown_is_verified_and_published_as_immutable_evidence() -> None:
    workflow = _load()
    teardown_body = _step(
        "teardown",
        "Scale down, revoke, remove fixtures, and destroy stacks",
    )["run"]
    assert "secretsmanager describe-secret" in teardown_body
    assert "secretsmanager get-secret-value" in teardown_body
    assert "AWSCURRENT" in teardown_body
    assert "aws ecs describe-services" in teardown_body
    assert "stateAbsentAfterCleanup" in teardown_body
    assert (
        "axonllm.agentcore-qualification-teardown/v1"
        in teardown_body
    )
    upload = _step(
        "teardown",
        "Preserve verified qualification teardown receipt",
    )
    assert "success()" in upload["if"]
    assert upload["with"]["if-no-files-found"] == "error"

    evidence = workflow["jobs"]["teardown-evidence"]
    assert evidence["environment"] == "agentcore-production-evidence"
    assert "needs.teardown.result == 'success'" in evidence["if"]
    validate = _step(
        "teardown-evidence",
        "Validate exact teardown receipt",
    )["run"]
    assert "expected_revoked_hash" in validate
    assert "AxonLLMAgentCoreStack-managed" in validate
    persist = _step(
        "teardown-evidence",
        "Sign, persist, and reverify teardown evidence",
    )["run"]
    assert "kms_evidence.py sign" in persist
    assert "kms_evidence.py verify" in persist
    assert "--object-lock-mode COMPLIANCE" in persist
    assert "--if-none-match '*'" in persist
    assert "--version-id" in persist
    assert persist.count("aws s3api head-object") == 2
    assert "--checksum-mode ENABLED" in persist
    assert 'metadata.get("ObjectLockMode") == "COMPLIANCE"' in persist
    assert 'metadata.get("BucketKeyEnabled") is True' in persist


def test_production_requires_successful_teardown_and_evidence() -> None:
    production = _load()["jobs"]["production"]
    condition = production["if"]
    assert "needs.teardown.result == 'success'" in condition
    assert "needs.teardown.outputs.succeeded == 'true'" in condition
    assert "needs.teardown-evidence.result == 'success'" in condition
    assert (
        "needs.teardown-evidence.outputs.receipt_version_id != ''"
        in condition
    )
    assert (
        "needs.teardown-evidence.outputs.receipt_sha256 != ''"
        in condition
    )
    for output_name in (
        "receipt_uri",
        "signature_uri",
        "signature_version_id",
        "signature_sha256",
    ):
        assert (
            f"needs.teardown-evidence.outputs.{output_name} != ''"
            in condition
        )
    assert "needs.external-oidc.result == 'success'" in condition
    assert "needs.launch-gates.result == 'success'" in condition
    assert "needs.rehearsal-evidence.result == 'success'" in condition
    assert (
        "needs.launch-gates.outputs.gate_manifest_version_id != ''"
        in condition
    )

    bindings = production["with"]
    assert bindings["operation"] == "deploy"
    assert bindings["rollback_provider_secret_version"] == ""
    assert all(
        "rehearsal" not in name
        or name.startswith("launch_rehearsal_detailed_")
        for name in bindings
    )
    assert not any(
        "rehearsal_control" in name for name in bindings
    )
    assert bindings["setup_config_s3_version_id"] == (
        "${{ inputs.setup_config_s3_version_id }}"
    )
    assert bindings["external_oidc_report_s3_uri"] == (
        "${{ needs.external-oidc.outputs.report_s3_uri }}"
    )
    assert bindings[
        "launch_rehearsal_detailed_report_s3_uri"
    ] == (
        "${{ needs.rehearsal-evidence.outputs."
        "detailed_report_uri }}"
    )
    teardown_bindings = {
        "qualification_teardown_receipt_s3_uri": "receipt_uri",
        "qualification_teardown_receipt_s3_version_id": (
            "receipt_version_id"
        ),
        "qualification_teardown_receipt_sha256": "receipt_sha256",
        "qualification_teardown_signature_s3_uri": "signature_uri",
        "qualification_teardown_signature_s3_version_id": (
            "signature_version_id"
        ),
        "qualification_teardown_signature_sha256": "signature_sha256",
    }
    for input_name, output_name in teardown_bindings.items():
        assert bindings[input_name] == (
            "${{ needs.teardown-evidence.outputs."
            + output_name
            + " }}"
        )


def test_all_actions_are_pinned_and_shells_start_fail_closed() -> None:
    workflow = _load()
    for job_name, job in workflow["jobs"].items():
        if "uses" in job:
            assert job["uses"].startswith("./.github/workflows/")
            continue
        for step in job.get("steps", []):
            action = step.get("uses")
            if action:
                assert re.fullmatch(
                    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
                    r"@[0-9a-f]{40}",
                    action,
                )
                if action.startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] in (
                        False,
                        "false",
                    )
            script = step.get("run")
            if script:
                first = next(
                    line.strip()
                    for line in script.splitlines()
                    if line.strip()
                )
                assert first == "set -euo pipefail", (
                    job_name,
                    step.get("name"),
                )


def test_workflow_contains_no_pasted_provider_credentials() -> None:
    body = WORKFLOW.read_text(encoding="utf-8")
    forbidden = (
        r"\bgsk_[A-Za-z0-9]{20,}",
        r"\bxai-[A-Za-z0-9]{20,}",
        r"\bfw_[A-Za-z0-9]{12,}",
        r"\bsk-[A-Za-z0-9]{20,}",
    )
    assert all(re.search(pattern, body) is None for pattern in forbidden)
    assert (
        "AXON_AGENTCORE_QUALIFICATION_PROVIDER_SOURCE_SECRET_ARN"
        in body
    )
    assert "AXON_AGENTCORE_PROVIDER_SOURCE_SECRET_ARN" not in body
    assert "secretsmanager get-secret-value" in body


def test_reusable_workflows_use_supported_commit_context() -> None:
    workflow_dir = ROOT / ".github" / "workflows"
    for filename in {
        "agentcore-launch-gates.yml",
        "agentcore-launch-rehearsal-evidence.yml",
        "certify-agentcore-external-oidc.yml",
        "deploy-agentcore-production.yml",
    }:
        body = (workflow_dir / filename).read_text(encoding="utf-8")
        assert "github.job_workflow_sha" not in body
        assert "CALLED_WORKFLOW_COMMIT: ${{ github.workflow_sha }}" in body
