from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from scripts.ci.validate_workflows import WorkflowLoader, validate_workflow


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-agentcore-production.yml"


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

    assert workflow["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }
    assert deploy["environment"] == "agentcore-production-deploy"
    assert deploy["runs-on"] == {
        "group": "axonllm-production",
        "labels": "axonllm-production-allowlisted",
    }
    assert deploy["permissions"]["id-token"] == "write"
    assert deploy["needs"] == [
        "verify-agentcore",
        "verify-control-plane",
    ]
    assert workflow["jobs"]["verify-agentcore"]["with"]["target"] == "agentcore"
    assert workflow["jobs"]["verify-control-plane"]["with"]["target"] == "fargate"
    assert "secrets" not in workflow["jobs"]["verify-agentcore"]
    assert "secrets" not in workflow["jobs"]["verify-control-plane"]
    credential_step = next(
        step for step in deploy["steps"] if step.get("name") == "Configure production AWS credentials through OIDC"
    )
    assert credential_step["with"]["role-to-assume"] == ("${{ secrets.AXON_AGENTCORE_DEPLOY_ROLE_ARN }}")
    assert credential_step["with"]["allowed-account-ids"] == ("${{ vars.AXON_AWS_ACCOUNT_ID }}")
    assert "aws-access-key-id" not in credential_step["with"]


def test_workflow_gates_deployment_before_signed_promotion() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["deploy"]["steps"]
    names = [step["name"] for step in steps]

    assert names.index("Synthesize and validate production infrastructure") < names.index(
        "Verify immutable transition journal storage"
    )
    assert names.index("Require reviewed templates before transactional promotion") < names.index(
        "Verify immutable transition journal storage"
    )
    assert names.index("Verify immutable transition journal storage") < names.index(
        "Reconcile any prior signed production transition"
    )
    assert names.index("Fetch and verify external OIDC certification evidence") < names.index(
        "Reconcile any prior signed production transition"
    )
    assert names.index("Fetch and verify qualification teardown evidence") < names.index(
        "Fetch and verify external OIDC certification evidence"
    )
    assert names.index("Reconcile any prior signed production transition") < names.index(
        "Stage identity and AgentCore candidate only"
    )
    assert names.index("Stage identity and AgentCore candidate only") < names.index(
        "Exercise PITR and validate state protection"
    )
    assert names.index("Exercise PITR and validate state protection") < names.index(
        "Prepare fresh certification identities"
    )
    assert names.index("Prepare fresh certification identities") < names.index(
        "Run direct AgentCore candidate certification"
    )
    assert names.index("Run direct AgentCore candidate certification") < names.index(
        "Prepare exact promotion and rollback metadata"
    )
    assert names.index("Prepare exact promotion and rollback metadata") < names.index(
        "Sign and persist promotion intent before changing production"
    )
    assert names.index("Sign and persist promotion intent before changing production") < names.index(
        "Promote certified AgentCore candidate"
    )
    assert names.index("Promote certified AgentCore candidate") < names.index(
        "Deploy reviewed Fargate control plane after promotion"
    )
    assert names.index("Deploy reviewed Fargate control plane after promotion") < names.index(
        "Run direct AgentCore production certification"
    )
    assert names.index("Run direct AgentCore production certification") < names.index(
        "Prepare fresh control-plane canary sessions"
    )
    assert names.index("Prepare fresh control-plane canary sessions") < names.index(
        "Run production control-plane RBAC and load validation"
    )
    assert names.index("Run production control-plane RBAC and load validation") < names.index(
        "Reconcile production validation rollback journal"
    )
    assert names.index("Reconcile production validation rollback journal") < names.index(
        "Remove control-plane canary sessions"
    )
    assert names.index("Remove control-plane canary sessions") < names.index(
        "Remove certification fixtures after both certifications"
    )
    assert names.index("Remove certification fixtures after both certifications") < names.index(
        "Require stable production recovery posture"
    )
    assert names.index("Require stable production recovery posture") < names.index(
        "Create and KMS-sign deployment evidence"
    )
    assert names.index("Create and KMS-sign deployment evidence") < names.index("Persist immutable deployment evidence")
    assert names.index("Persist immutable deployment evidence") < names.index("Finalize production promotion")
    assert names.index("Finalize production promotion") < names.index("Append signed committed transition record")
    assert names.index("Append signed committed transition record") < names.index(
        "Roll back the full persisted transition after failure"
    )
    assert names.index("Roll back the full persisted transition after failure") < names.index(
        "Append signed rolled-back transition record"
    )
    assert names.index("Append signed rolled-back transition record") < names.index(
        "Retain signed evidence with the workflow run"
    )

    body = WORKFLOW.read_text(encoding="utf-8")
    assert "synthesize_and_verify_cdk.sh" in body
    assert "cloudformation get-template" in body
    assert "separate approved infrastructure migration" in body
    assert "trivy config" in body
    assert "--exercise-restore" in body
    assert "--require-vault-lock" not in body
    assert "--start-backup" not in body
    assert "agentcore_recovery.py" in body
    assert "prepare_agentcore_certification.py prepare" in body
    assert "prepare_agentcore_certification.py cleanup" in body
    assert "certify_agentcore.py" in body
    assert "--candidate-endpoint-name" in body
    assert "--prepare-candidate-promotion-version" in body
    assert "--promote-candidate-version" in body
    assert "--deploy-control-plane" in body
    assert "--finalize-promotion" in body
    assert "--rollback-promotion" in body
    assert "--discard-candidate-version" in body
    assert "steps.persist_intent.outcome == 'success'" in body
    assert "steps.persist_evidence.outputs.verified != 'true'" in body
    assert "steps.committed.outcome != 'success'" in body
    assert "steps.rollback.outputs.rolled_back == 'true'" in body
    assert "CandidateRuntimeEndpointName" in body
    assert "deployment_evidence.py create" in body
    assert "deployment_transition.py create" in body
    assert "deployment_transition.py create-recovery-binding" in body
    assert "deployment_transition.py verify-recovery-binding" in body
    assert "transition-recovery-setup.json" in body
    assert "transition-recovery-binding.json" in body
    assert "run_production_validation.py" in body
    assert "--rollback-journal" in body
    assert "--reconcile-rollback-journal" in body
    assert "prepare_control_plane_canary_sessions.py cleanup" in body
    assert "playwright install chromium" in body
    assert "--target-group-arn" in body
    assert "CONTROL_PLANE_CANARY_SECRET_ARN" not in body
    assert "--production-certification-report" in body
    assert "--production-validation-report" in body
    assert "--launch-rehearsal-report" in body
    assert "--external-oidc-certification-report" in body
    assert "--external-oidc-report-version-id" in body
    assert "--qualification-teardown-receipt-version-id" in body
    assert "--qualification-teardown-signature-version-id" in body
    assert "verify-qualification-teardown" in body
    assert "kms_evidence.py sign" in body
    assert body.count("kms_evidence.py verify") >= 3
    assert "AXON_AGENTCORE_CERTIFICATION_SECRET_ARN" not in body

    canary_cleanup = next(step for step in steps if step.get("name") == "Remove control-plane canary sessions")
    assert canary_cleanup["if"] == "${{ always() }}"
    assert "control-plane-canary-sessions.json" in canary_cleanup["run"]
    assert "control-plane-canary-credentials.json" in canary_cleanup["run"]
    rollback_reconciliation = next(
        step for step in steps if step.get("name") == "Reconcile production validation rollback journal"
    )
    assert rollback_reconciliation["if"] == "${{ always() }}"
    assert "production-validation-rollback-journal.json" in rollback_reconciliation["run"]


def test_control_plane_validation_is_endpoint_mode_bound() -> None:
    steps = _workflow()["jobs"]["deploy"]["steps"]
    prepare = next(step for step in steps if step.get("name") == "Prepare fresh control-plane canary sessions")["run"]
    validation = next(
        step for step in steps if step.get("name") == "Run production control-plane RBAC and load validation"
    )["run"]

    for body in (prepare, validation):
        for binding in (
            "EndpointMode",
            "ControlPlaneUrl",
            "ControlPlaneDomainName",
            "ControlPlaneAuthMode",
            "custom-domain",
            "cloudfront",
            "alb-cognito",
            "application-oidc",
            "alb-session-cookie",
            "browser-session-cookie",
        ):
            assert binding in body
    assert "setup_mode = control.get(" in validation
    assert '"endpoint_mode",' in validation
    assert 'deployed_control.get("ControlPlaneUrl")' in validation
    assert '"--base-url",' in validation
    assert "control_url," in validation
    assert 'control_url = f"https://' not in validation


def test_transition_journal_is_replay_bound_and_crash_recoverable() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["deploy"]["steps"]
    body = WORKFLOW.read_text(encoding="utf-8")
    committed = next(step for step in steps if step.get("name") == "Append signed committed transition record")["run"]
    reconciliation = next(
        step for step in steps if step.get("name") == "Reconcile any prior signed production transition"
    )["run"]
    rolled_back = next(step for step in steps if step.get("name") == "Append signed rolled-back transition record")[
        "run"
    ]

    assert "--transition-context" in body
    assert "deployment_transition.py verify-intent" in body
    assert "deployment_transition.py fetch-s3-object" in body
    assert "list-object-versions" in body
    assert ".DeleteMarkers" in body
    assert "s3:DeleteObject" in body
    for publisher in (committed, rolled_back):
        assert 'case "${terminal_state}:${bundle_state}"' in publisher
        assert "present:present)" in publisher
        assert "present:absent)" in publisher
        assert "absent:absent)" in publisher
        assert "partial or indeterminate" in publisher
        assert publisher.index('put_locked "${terminal}"') < publisher.index("kms_evidence.py sign")
        assert publisher.index("kms_evidence.py sign") < publisher.index('put_locked "${bundle}"')

    assert 'case "${terminal_state}:${terminal_bundle_state}"' in reconciliation
    assert "present:absent)" in reconciliation
    assert "absent:absent)" in reconciliation
    assert "terminal_needs_persist=true" in reconciliation
    assert reconciliation.index('put_locked "${terminal}" "${terminal_key}"') < reconciliation.index(
        "kms_evidence.py sign"
    )
    assert reconciliation.index("kms_evidence.py sign") < reconciliation.index(
        'put_locked "${terminal_bundle}" "${terminal_bundle_key}"'
    )
    assert '--record "${terminal}"' in reconciliation


def test_evidence_resolution_is_tristate_and_commit_wins() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["deploy"]["steps"]
    persist = next(step for step in steps if step.get("name") == "Persist immutable deployment evidence")
    summary = next(step for step in steps if step.get("name") == "Summarize immutable deployment evidence")
    rollback = next(
        step for step in steps if step.get("name") == "Roll back the full persisted transition after failure"
    )
    reconciliation = next(
        step for step in steps if step.get("name") == "Reconcile any prior signed production transition"
    )["run"]

    persist_run = persist["run"]
    assert "present|absent|indeterminate)" in persist_run
    assert "create-commit" in persist_run
    assert "verify-commit" in persist_run
    assert "The final signature is the sole atomic completion signal" in persist_run
    assert "Persisted deployment evidence commit is incomplete or indeterminate" in persist_run
    assert persist_run.index('"${evidence}:${evidence_key}"') < persist_run.index(
        '"${commit_bundle}:${commit_bundle_key}"'
    )
    assert persist_run.rstrip().endswith("printf 'verified=true\\n' >>\"${GITHUB_OUTPUT}\"")
    assert summary["continue-on-error"] == "true"
    assert "steps.persist_evidence.outputs.verified == 'true'" in summary["if"]

    rollback_run = rollback["run"]
    assert "steps.persist_evidence.outputs.verified != 'true'" in rollback["if"]
    assert 'case "${commit_bundle_state}"' in rollback_run
    assert "Committed deployment evidence is incomplete" in rollback_run
    assert "Deployment evidence commit state is indeterminate" in rollback_run
    assert "present)" in rollback_run
    assert "absent)" in rollback_run
    assert rollback_run.index("absent)") < rollback_run.index("--rollback-promotion")

    assert 'case "${evidence_commit_bundle_state}"' in reconciliation
    assert "Committed deployment evidence is incomplete" in reconciliation
    assert "Deployment evidence commit state is indeterminate" in reconciliation
    assert reconciliation.index("present)") < reconciliation.index("--finalize-promotion")
    assert reconciliation.index("absent)") < reconciliation.index("--rollback-promotion")


def test_workflow_supports_immutable_rollback_without_rebuild() -> None:
    workflow = _workflow()
    inputs = workflow["on"]["workflow_call"]["inputs"]
    body = WORKFLOW.read_text(encoding="utf-8")

    assert inputs["operation"]["type"] == "string"
    assert "rollback_provider_secret_version" in inputs
    assert "--rollback-provider-secret-version" in body
    assert "verified_image_uri" in body
    assert "docker build" not in body
    assert "buildx" not in body
    assert ":latest" not in body
    assert '"latest"' not in body
    assert "npm ci" in body
    assert "--package-lock=false" not in body
    assert "AXON_CDK_CLI_VERSION" not in body


def test_workflow_call_is_the_only_production_entry_point() -> None:
    workflow = _workflow()
    call_inputs = workflow["on"]["workflow_call"]["inputs"]
    body = WORKFLOW.read_text(encoding="utf-8")

    assert set(workflow["on"]) == {"workflow_call"}
    assert call_inputs["operation"]["type"] == "string"
    assert "aws_region" not in call_inputs
    assert "${{ inputs.aws_region }}" not in body
    assert workflow["env"]["AWS_REGION"] == "us-east-1"
    assert workflow["jobs"]["verify-agentcore"]["with"]["aws_region"] == ("us-east-1")
    assert workflow["jobs"]["verify-control-plane"]["with"]["aws_region"] == ("us-east-1")


def test_workflow_binds_reviewed_configs_to_s3_versions_and_hashes() -> None:
    workflow = _workflow()
    inputs = workflow["on"]["workflow_call"]["inputs"]
    steps = workflow["jobs"]["deploy"]["steps"]
    validation = next(step for step in steps if step.get("name") == "Validate immutable production inputs")
    fetch = next(step for step in steps if step.get("name") == "Fetch and bind reviewed launch configuration")
    external_oidc = next(
        step for step in steps if step.get("name") == "Fetch and verify external OIDC certification evidence"
    )
    teardown = next(step for step in steps if step.get("name") == "Fetch and verify qualification teardown evidence")

    required_inputs = {
        "setup_config_s3_version_id",
        "setup_config_sha256",
        "certification_config_s3_version_id",
        "certification_config_sha256",
        "production_validation_config_s3_version_id",
        "production_validation_config_sha256",
        "launch_rehearsal_detailed_report_s3_uri",
        "launch_rehearsal_detailed_report_s3_version_id",
        "launch_rehearsal_detailed_report_sha256",
        "launch_rehearsal_detailed_signature_s3_uri",
        "launch_rehearsal_detailed_signature_s3_version_id",
        "launch_rehearsal_detailed_signature_sha256",
        "external_oidc_report_s3_uri",
        "external_oidc_report_s3_version_id",
        "external_oidc_report_sha256",
        "external_oidc_signature_s3_uri",
        "external_oidc_signature_s3_version_id",
        "external_oidc_signature_sha256",
        "qualification_teardown_receipt_s3_uri",
        "qualification_teardown_receipt_s3_version_id",
        "qualification_teardown_receipt_sha256",
        "qualification_teardown_signature_s3_uri",
        "qualification_teardown_signature_s3_version_id",
        "qualification_teardown_signature_sha256",
    }
    assert all(inputs[name]["required"] == "true" for name in required_inputs)
    assert validation["env"]["SETUP_CONFIG_VERSION_ID"] == ("${{ inputs.setup_config_s3_version_id }}")
    assert validation["env"]["CERTIFICATION_CONFIG_VERSION_ID"] == ("${{ inputs.certification_config_s3_version_id }}")
    assert validation["env"]["SETUP_CONFIG_SHA256"] == ("${{ inputs.setup_config_sha256 }}")
    assert validation["env"]["CERTIFICATION_CONFIG_SHA256"] == ("${{ inputs.certification_config_sha256 }}")
    assert validation["env"]["PRODUCTION_VALIDATION_CONFIG_VERSION_ID"] == (
        "${{ inputs.production_validation_config_s3_version_id }}"
    )
    assert validation["env"]["LAUNCH_REHEARSAL_REPORT_URI"] == ("${{ inputs.launch_rehearsal_detailed_report_s3_uri }}")
    assert validation["env"]["LAUNCH_REHEARSAL_REPORT_VERSION_ID"] == (
        "${{ inputs.launch_rehearsal_detailed_report_s3_version_id }}"
    )
    assert validation["env"]["LAUNCH_REHEARSAL_SIGNATURE_URI"] == (
        "${{ inputs.launch_rehearsal_detailed_signature_s3_uri }}"
    )
    assert validation["env"]["LAUNCH_REHEARSAL_SIGNATURE_VERSION_ID"] == (
        "${{ inputs.launch_rehearsal_detailed_signature_s3_version_id }}"
    )
    assert validation["env"]["EXTERNAL_OIDC_REPORT_URI"] == ("${{ inputs.external_oidc_report_s3_uri }}")
    assert validation["env"]["EXTERNAL_OIDC_SIGNATURE_URI"] == ("${{ inputs.external_oidc_signature_s3_uri }}")
    assert validation["env"]["TEARDOWN_RECEIPT_URI"] == ("${{ inputs.qualification_teardown_receipt_s3_uri }}")
    assert validation["env"]["TEARDOWN_SIGNATURE_URI"] == ("${{ inputs.qualification_teardown_signature_s3_uri }}")
    assert validation["run"].count("^[0-9a-f]{64}$") == 9
    assert validation["run"].count('!= "null"') == 9
    assert fetch["run"].count("aws s3api get-object") == 5
    assert fetch["run"].count("--version-id") == 5
    assert fetch["run"].count("sha256sum --check --status") == 5
    assert "kms_evidence.py verify" in fetch["run"]
    assert "launch_rehearsal_evidence.py verify-detailed" in fetch["run"]
    assert 'metadata.get("ObjectLockMode") == "COMPLIANCE"' in fetch["run"]
    assert 'metadata.get("ChecksumSHA256") == checksum' in fetch["run"]
    assert "git merge-base --is-ancestor" in fetch["run"]
    assert external_oidc["run"].count("aws s3api get-object") == 2
    assert external_oidc["run"].count("--version-id") == 2
    assert external_oidc["run"].count("sha256sum --check --status") == 2
    assert "kms_evidence.py verify" in external_oidc["run"]
    assert "verify-published-report" in external_oidc["run"]
    assert 'metadata.get("ObjectLockMode") == "COMPLIANCE"' in (external_oidc["run"])
    assert 'metadata.get("BucketKeyEnabled") is True' in (external_oidc["run"])
    assert teardown["run"].count("aws s3api head-object") == 2
    assert teardown["run"].count("aws s3api get-object") == 2
    assert teardown["run"].count("--version-id") == 4
    assert teardown["run"].count("sha256sum --check --status") == 2
    assert "kms_evidence.py verify" in teardown["run"]
    assert "verify-qualification-teardown" in teardown["run"]
    assert 'metadata.get("ObjectLockMode") == "COMPLIANCE"' in (teardown["run"])
    assert 'metadata.get("BucketKeyEnabled") is True' in (teardown["run"])
    assert "AXON_AGENTCORE_LAUNCH_REHEARSAL_REPORT_S3_URI" not in (WORKFLOW.read_text(encoding="utf-8"))
    assert "AXON_AGENTCORE_LAUNCH_REHEARSAL_SIGNATURE_S3_URI" not in (WORKFLOW.read_text(encoding="utf-8"))
    assert "aws s3 cp" not in fetch["run"]


def test_workflow_rejects_incomplete_production_provider_contract_early() -> None:
    workflow = _workflow()
    fetch = next(
        step
        for step in workflow["jobs"]["deploy"]["steps"]
        if step.get("name") == "Fetch and bind reviewed launch configuration"
    )
    run = fetch["run"]

    assert ('uv run --frozen --no-sync python - \\\n  "${setup}" \\\n  "${certification}"') in run
    assert "PRODUCTION_ALLOWED_PROVIDERS" in run
    assert "PRODUCTION_LAUNCH_PROFILE" in run
    assert "PRODUCTION_LAUNCH_PROVIDERS" in run
    assert "PRODUCTION_PROVIDER_FEATURES_BY_PROVIDER" in run
    assert "parse_certification_config" in run
    assert 'runtime.get("enabled_providers")' in run
    assert "not PRODUCTION_LAUNCH_PROVIDERS.issubset(" in run
    assert "not enabled_provider_set.issubset(" in run
    assert "certification.profile != PRODUCTION_LAUNCH_PROFILE" in run
    assert "PRODUCTION_PROVIDER_FEATURES_BY_PROVIDER[" in run
    assert run.index("parse_certification_config") < run.index("./deploy-agentcore.sh")


def test_workflow_persists_only_signed_redacted_evidence_with_object_lock() -> None:
    workflow = _workflow()
    deploy = workflow["jobs"]["deploy"]
    body = WORKFLOW.read_text(encoding="utf-8")
    upload = next(
        step for step in deploy["steps"] if step.get("name") == "Retain signed evidence with the workflow run"
    )

    assert "--object-lock-mode COMPLIANCE" in body
    assert "--if-none-match '*'" in body
    assert "get-object-lock-configuration" in body
    assert "get-bucket-versioning" in body
    assert "get-bucket-encryption" in body
    assert "agentcore-deployment-kms-signature.json" in body
    assert "agentcore-deployment-commit.json" in body
    assert "agentcore-deployment-commit-kms-signature.json" in body
    assert "provider-secret-version.json" in body
    assert "--runtime-outputs" in body
    assert "--certification-report" in body
    assert "--production-validation-config" in body
    assert "--launch-rehearsal-signature" in body
    assert "promotion.json" in body
    assert "promotion-finalization.json" in upload["with"]["path"]
    assert "transition-terminal.json" in body
    assert "steps.persist_intent.outcome" in body
    assert upload["with"]["retention-days"] == 90
    assert "provider.env" not in upload["with"]["path"]
    assert "certification-credentials" not in upload["with"]["path"]
    assert "certification-fixtures" not in upload["with"]["path"]
    assert "AWS_ACCESS_KEY_ID" not in body
    assert "AWS_SECRET_ACCESS_KEY" not in body


def test_production_workflow_passes_repository_policy() -> None:
    assert validate_workflow(WORKFLOW) == 8
