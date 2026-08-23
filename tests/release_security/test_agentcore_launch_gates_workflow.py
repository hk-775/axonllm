from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "agentcore-launch-gates.yml"
sys.path.insert(0, str(ROOT / "scripts" / "ci"))

import validate_workflows  # noqa: E402


def _workflow() -> dict[str, Any]:
    value = yaml.load(
        WORKFLOW.read_text(encoding="utf-8"),
        Loader=validate_workflows.WorkflowLoader,
    )
    assert isinstance(value, dict)
    return value


def _step(name: str) -> dict[str, Any]:
    steps = _workflow()["jobs"]["launch-gates"]["steps"]
    return next(step for step in steps if step.get("name") == name)


def test_workflow_is_manual_protected_and_policy_compliant() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["launch-gates"]

    assert set(workflow["on"]) == {
        "workflow_call",
        "workflow_dispatch",
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "agentcore-production-launch-gates",
        "cancel-in-progress": "false",
    }
    assert job["environment"] == "agentcore-production-launch-gates"
    assert job["timeout-minutes"] == 170
    assert job["runs-on"] == [
        "self-hosted",
        "linux",
        "x64",
        "axonllm-production-allowlisted",
    ]
    assert job["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }
    assert validate_workflows.validate_workflow(WORKFLOW) == 5


def test_workflow_has_no_hand_authored_gate_outcomes() -> None:
    inputs = _workflow()["on"]["workflow_dispatch"]["inputs"]
    call = _workflow()["on"]["workflow_call"]

    assert set(inputs) == {
        "release_commit_sha",
        "agentcore_image_reference",
        "control_plane_image_reference",
        "gate_config_s3_version_id",
        "gate_config_sha256",
        "aws_region",
    }
    assert all(value["required"] == "true" for value in inputs.values())
    assert set(call["inputs"]) == set(inputs)
    assert set(call["outputs"]) == {
        "gate_manifest_uri",
        "gate_manifest_version_id",
        "gate_manifest_sha256",
        "gate_manifest_signature_uri",
        "gate_manifest_signature_version_id",
        "gate_manifest_signature_sha256",
    }
    assert not any(token in name.lower() for name in inputs for token in ("pass", "status", "outcome", "observation"))


def test_workflow_uses_only_protected_producer_checkout() -> None:
    steps = _workflow()["jobs"]["launch-gates"]["steps"]
    checkouts = [step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@")]

    assert len(checkouts) == 1
    producer = checkouts[0]
    assert producer["with"] == {
        "clean": "true",
        "fetch-depth": 0,
        "path": "producer",
        "persist-credentials": "false",
        "ref": "${{ inputs.release_commit_sha }}",
    }
    validation = _step("Validate protected dispatch and immutable bindings")["run"]
    assert 'GITHUB_REF}" == "refs/heads/main"' in validation
    assert "agentcore-launch-gates.yml@refs/heads/main" in validation
    assert 'CALLED_WORKFLOW_COMMIT}" == "${RELEASE_COMMIT}"' in validation
    assert 'PARENT_WORKFLOW_COMMIT}" == "${RELEASE_COMMIT}"' in validation
    assert "git -C release" not in validation
    assert "producer/scripts/operations/rehearse_agentcore_launch.py" in validation


def test_workflow_uses_separate_oidc_roles_and_locked_config() -> None:
    prepare_credentials = _step("Configure evidence credentials for immutable config")
    launch_credentials = _step("Configure launch coordinator credentials")
    publish_credentials = _step("Configure evidence credentials for publication")
    fetch = _step("Fetch and bind immutable reviewed gate config")
    body = WORKFLOW.read_text(encoding="utf-8")

    assert prepare_credentials["with"]["role-to-assume"] == (
        "${{ secrets.AXON_AGENTCORE_LAUNCH_GATES_ROLE_ARN }}"
    )
    assert publish_credentials["with"]["role-to-assume"] == (
        "${{ secrets.AXON_AGENTCORE_LAUNCH_GATES_ROLE_ARN }}"
    )
    assert launch_credentials["with"]["role-to-assume"] == ("${{ secrets.AXON_AGENTCORE_LAUNCH_GATES_ROLE_ARN }}")
    assert launch_credentials["with"]["role-session-name"] == (
        "AxonLLMLaunchGates-${{ github.run_id }}-${{ github.run_attempt }}"
    )
    for credentials in (
        prepare_credentials,
        launch_credentials,
        publish_credentials,
    ):
        assert credentials["with"]["output-credentials"] == "false"
        assert credentials["with"]["output-env-credentials"] == "true"
        assert credentials["with"]["unset-current-credentials"] == "true"
    assert publish_credentials["if"] == "${{ always() }}"
    assert "id-token: write" in body
    assert fetch["env"]["AWS_ACCOUNT_ID"] == ("${{ vars.AXON_AWS_ACCOUNT_ID }}")
    assert fetch["env"]["CONFIG_URI"] == ("${{ vars.AXON_AGENTCORE_LAUNCH_GATE_CONFIG_S3_URI }}")
    assert fetch["env"]["CONFIG_VERSION_ID"] == ("${{ inputs.gate_config_s3_version_id }}")
    run = fetch["run"]
    assert "get-bucket-versioning" in run
    assert "get-object-lock-configuration" in run
    assert run.count("aws s3api get-object \\") == 1
    assert run.count("aws s3api get-object-retention") == 1
    assert "--version-id" in run
    assert run.count('--expected-bucket-owner "${AWS_ACCOUNT_ID}"') == 4
    assert "--checksum-mode ENABLED" in run
    assert "sha256sum --check --status" in run
    assert 'metadata.get("ChecksumSHA256") == expected_checksum' in run
    assert 'metadata.get("ObjectLockMode") == "COMPLIANCE"' in run
    assert 'retention.get("Retention", {}).get("Mode")' in run
    assert run.count("timedelta(days=2555)") == 2
    assert 'install -d -m 0700 "${state_dir}"' in run
    assert 'chmod 0400 "${config}"' in run
    assert '"${state_dir}/execution-bundle.json"' in run


def test_workflow_enforces_two_phase_capability_boundary() -> None:
    steps = _workflow()["jobs"]["launch-gates"]["steps"]
    execute = _step("Execute launch gates through coordinator")
    publish = _step("Publish signed immutable receipts")
    execute_run = execute["run"]
    publish_run = publish["run"]

    assert execute["continue-on-error"] == "true"
    assert publish["if"] == ("${{ always() && steps.publication-credentials.outcome == 'success' }}")
    assert "launch_gate_receipts.py \\\n  execute \\" in execute_run
    assert "launch_gate_receipts.py \\\n  publish \\" in publish_run
    common_arguments = [
        "--release-commit",
        "--region",
        "--agentcore-image",
        "--control-plane-image",
        "--repository",
        "--workflow-ref",
        "--workflow-commit",
        "--parent-workflow-ref",
        "--parent-workflow-commit",
        "--run-id",
        "--run-attempt",
        "--reviewed-config",
        "--reviewed-config-uri",
        "--reviewed-config-version-id",
        "--reviewed-config-sha256",
        "--state-dir",
        "--execution-bundle",
    ]
    execute_arguments = [
        *common_arguments,
        "--operation-root",
        "--github-output",
    ]
    publish_arguments = [
        *common_arguments,
        "--execution-bundle-sha256",
        "--evidence-bucket",
        "--evidence-prefix",
        "--manifest-uri",
        "--manifest-signature-uri",
        "--storage-kms-key-arn",
        "--signing-key-arn",
        "--github-output",
    ]
    actual_execute = [
        line.strip().split(maxsplit=1)[0] for line in execute_run.splitlines() if line.strip().startswith("--")
    ]
    actual_publish = [
        line.strip().split(maxsplit=1)[0] for line in publish_run.splitlines() if line.strip().startswith("--")
    ]
    assert actual_execute == execute_arguments
    assert actual_publish == publish_arguments
    assert not {
        "EVIDENCE_BUCKET",
        "EVIDENCE_PREFIX",
        "MANIFEST_URI",
        "MANIFEST_SIGNATURE_URI",
        "SIGNING_KEY_ARN",
        "STORAGE_KEY_ARN",
    } & set(execute["env"])
    assert "--evidence-" not in execute_run
    assert "--manifest-" not in execute_run
    assert "--signing-key-arn" not in execute_run
    assert "--storage-kms-key-arn" not in execute_run
    assert "--operation-root" not in publish_run
    assert publish["env"]["EXECUTION_BUNDLE_SHA256"] == ("${{ steps.execute.outputs.execution_bundle_sha256 }}")
    assert publish["env"]["REVIEWED_CONFIG_URI"] == ("${{ vars.AXON_AGENTCORE_LAUNCH_GATE_CONFIG_S3_URI }}")
    assert publish["env"]["REVIEWED_CONFIG_VERSION_ID"] == ("${{ inputs.gate_config_s3_version_id }}")
    assert publish["env"]["REVIEWED_CONFIG_SHA256"] == ("${{ inputs.gate_config_sha256 }}")
    assert "${{ secrets." not in execute_run + publish_run
    assert "AWS_ACCESS_KEY_ID" not in execute_run + publish_run
    assert "AWS_SECRET_ACCESS_KEY" not in execute_run + publish_run
    assert "AWS_SESSION_TOKEN" not in execute_run + publish_run

    names = [step["name"] for step in steps]
    assert names.index("Configure evidence credentials for immutable config") < names.index(
        "Fetch and bind immutable reviewed gate config"
    )
    assert names.index("Configure launch coordinator credentials") < names.index(
        "Execute launch gates through coordinator"
    )
    assert names.index("Configure evidence credentials for publication") < names.index(
        "Publish signed immutable receipts"
    )


def test_workflow_exports_exact_manifest_reference_triples() -> None:
    outputs = _workflow()["jobs"]["launch-gates"]["outputs"]

    assert outputs == {
        "gate_manifest_uri": "${{ steps.publish.outputs.gate_manifest_uri }}",
        "gate_manifest_version_id": ("${{ steps.publish.outputs.gate_manifest_version_id }}"),
        "gate_manifest_sha256": ("${{ steps.publish.outputs.gate_manifest_sha256 }}"),
        "gate_manifest_signature_uri": ("${{ steps.publish.outputs.gate_manifest_signature_uri }}"),
        "gate_manifest_signature_version_id": ("${{ steps.publish.outputs.gate_manifest_signature_version_id }}"),
        "gate_manifest_signature_sha256": ("${{ steps.publish.outputs.gate_manifest_signature_sha256 }}"),
    }
