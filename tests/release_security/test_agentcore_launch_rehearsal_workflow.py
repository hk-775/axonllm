from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (
    ROOT
    / ".github"
    / "workflows"
    / "agentcore-launch-rehearsal-evidence.yml"
)
sys.path.insert(0, str(ROOT / "scripts" / "ci"))

import validate_workflows  # noqa: E402


def _workflow() -> dict[str, Any]:
    value = yaml.load(
        WORKFLOW.read_text(encoding="utf-8"),
        Loader=validate_workflows.WorkflowLoader,
    )
    assert isinstance(value, dict)
    return value


def _body() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_is_manual_protected_and_uses_allowlisted_runner() -> None:
    workflow = _workflow()

    assert set(workflow["on"]) == {"workflow_call"}
    job = workflow["jobs"]["produce"]
    assert job["environment"] == "agentcore-production-evidence"
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
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    checkout = job["steps"][0]
    assert checkout["with"]["clean"] == "true"
    assert checkout["with"]["fetch-depth"] == 0
    assert validate_workflows.validate_workflow(WORKFLOW) == 3


def test_workflow_has_no_hand_authored_gate_outcome_inputs() -> None:
    call = _workflow()["on"]["workflow_call"]
    inputs = call["inputs"]

    assert "release_commit_sha" in inputs
    assert "agentcore_image_reference" in inputs
    assert "control_plane_image_reference" in inputs
    assert "gate_manifest_s3_version_id" in inputs
    assert "gate_manifest_signature_s3_version_id" in inputs
    assert not any(
        "pass" in name.lower()
        or "status" in name.lower()
        or "outcome" in name.lower()
        for name in inputs
    )
    assert set(call["inputs"]) == set(inputs)
    assert {
        "detailed_report_uri",
        "detailed_report_version_id",
        "detailed_report_sha256",
        "detailed_signature_uri",
        "detailed_signature_version_id",
        "detailed_signature_sha256",
    }.issubset(call["outputs"])


def test_workflow_verifies_signed_receipts_and_kms_signs_derived_reports() -> None:
    body = _body()

    assert "launch_rehearsal_evidence.py produce" in body
    assert "launch_rehearsal_evidence.py verify" in body
    assert body.count("kms_evidence.py sign") == 2
    assert body.count("kms_evidence.py verify") == 4
    assert "gate_manifest_s3_version_id" in body
    assert "gate_manifest_signature_s3_version_id" in body
    assert "AXON_AGENTCORE_PREREQUISITE_SIGNING_KEY_ARN" in body
    assert "AXON_DEPLOYMENT_SIGNING_KEY_ARN" not in body
    assert "AXON_DEPLOYMENT_EVIDENCE_KMS_KEY_ARN" in body
    assert "role-to-assume:" in body
    assert "id-token: write" in body
    assert 'CALLED_WORKFLOW_COMMIT}" == "${RELEASE_COMMIT}"' in body
    assert 'PARENT_WORKFLOW_COMMIT}" == "${RELEASE_COMMIT}"' in body
    assert "--parent-workflow-ref" in body
    assert "scripts/release/launch_rehearsal_evidence.py produce" in body
    assert "s3:GetObjectVersion" in body


def test_workflow_requires_compliance_lock_and_exact_version_readback() -> None:
    body = _body()

    assert "get-bucket-versioning" in body
    assert "get-object-lock-configuration" in body
    assert "--object-lock-mode COMPLIANCE" in body
    assert "--object-lock-retain-until-date" in body
    assert "--version-id" in body
    assert 'metadata.get("ObjectLockMode") == "COMPLIANCE"' in body
    assert "cmp --silent" in body
    assert "sha256sum" in body
    assert "report_version_id=" in body
    assert "signature_version_id=" in body


def test_workflow_publishes_current_projection_separately_from_detail() -> None:
    body = _body()

    assert "agentcore-launch-rehearsal-evidence.json" in body
    assert "agentcore-launch-rehearsal.json" in body
    assert "AXON_AGENTCORE_LAUNCH_REHEARSAL_REPORT_S3_URI" in body
    assert "AXON_AGENTCORE_LAUNCH_REHEARSAL_SIGNATURE_S3_URI" in body
    assert "detailed_report_version_id" in body
    assert "report_version_id" in body
