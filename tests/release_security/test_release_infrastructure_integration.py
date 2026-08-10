from __future__ import annotations

import json
import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = REPO_ROOT / "deploy-fargate.sh"
SYNTHESIS_SCRIPT = REPO_ROOT / "scripts" / "ci" / "synthesize_and_verify_cdk.sh"
ROOT_DOCKERFILE = REPO_ROOT / "Dockerfile"
AGENTCORE_DOCKERFILE = REPO_ROOT / "infra" / "agentcore-image" / "Dockerfile"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-security.yml"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-verification.yml"
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-release.yml"
REGISTRY_INSTALLER = REPO_ROOT / "scripts" / "ci" / "install_registry_tools.sh"


def _inline_policy_statements(workflow_path: Path) -> list[dict]:
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    statements = []
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            policy = step.get("with", {}).get("inline-session-policy")
            if policy is not None:
                statements.extend(json.loads(policy)["Statement"])
    return statements


def _actions(statement: dict) -> set[str]:
    actions = statement["Action"]
    if isinstance(actions, str):
        return {actions}
    return set(actions)


def _kms_statements(workflow_path: Path) -> list[dict]:
    return [
        statement
        for statement in _inline_policy_statements(workflow_path)
        if any(action.startswith("kms:") for action in _actions(statement))
    ]


def _assert_rotation_safe_verify_policy(workflow_path: Path) -> None:
    statements = _kms_statements(workflow_path)
    assert len(statements) == 1
    assert _actions(statements[0]) == {"kms:Verify"}
    assert statements[0]["Resource"] == (
        "arn:aws:kms:us-east-1:"
        "${{ vars.AXON_AWS_ACCOUNT_ID }}:key/*"
    )
    assert statements[0]["Condition"] == {
        "ForAnyValue:StringLike": {
            "kms:ResourceAliases": (
                "alias/axonllm/release-signing-v*"
            ),
        },
    }


def test_fargate_deploy_requires_and_passes_verified_image() -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "require_env AXON_VERIFIED_IMAGE_URI" in script
    parameter = '--parameters "AxonLLMStack:VerifiedImageUri=${AXON_VERIFIED_IMAGE_URI}"'
    assert parameter in script
    assert "immutable private ECR URI in us-east-1" in script


def test_synthesis_requires_zero_cdk_docker_assets() -> None:
    script = SYNTHESIS_SCRIPT.read_text(encoding="utf-8")

    assert 'manifest.get("dockerImages")' in script
    assert "docker_images != {}" in script
    assert "expected zero CDK Docker assets" in script
    assert "verify_cdk_asset.py" not in script
    assert 'verify_target "fargate" "AxonLLMStack"' in script
    assert (
        'verify_target "agentcore" "AxonLLMAgentCoreStack"'
        in script
    )
    assert (
        'verify_target "release-foundation" '
        '"AxonLLMReleaseFoundationStack"'
    ) in script
    assert '"${venv_dir}/bin/cfn-lint" -i W3005' in script


def test_runtime_dockerfiles_use_architecture_specific_digest_pins() -> None:
    fargate = ROOT_DOCKERFILE.read_text(encoding="utf-8")
    agentcore = AGENTCORE_DOCKERFILE.read_text(encoding="utf-8")

    assert (
        "FROM docker.io/library/python:3.12-slim@sha256:"
        "d657ab0ade19f404a6ccc883ab399540de667aff751748ce23c07330c5a89e64"
    ) in fargate
    assert (
        "COPY --from=ghcr.io/astral-sh/uv:0.10.7@sha256:"
        "0ca776d5bd774b0f8a9092100166ac46bf93386da17b8bf626f8e60b1f2d1c77"
    ) in fargate
    assert (
        "FROM docker.io/library/python:3.12-slim@sha256:"
        "adbc7c33e0abc183557d1d14ce5eb5d261aaadff5451c81a8db636b3ebefcdf6"
    ) in agentcore
    assert (
        "COPY --from=ghcr.io/astral-sh/uv:0.10.7@sha256:"
        "5fe7b2b0499a485ee86e1e0d2154e1556a08dffeb5f72b204895af5212a2069c"
    ) in agentcore

    for dockerfile in (fargate, agentcore):
        assert "UV_NO_CACHE=1" in dockerfile
        image_sources = re.findall(
            r"^(?:FROM|COPY --from=)(\S+)",
            dockerfile,
            flags=re.MULTILINE,
        )
        external_sources = [source for source in image_sources if "/" in source]
        assert external_sources
        assert all("@sha256:" in source for source in external_sources)


def test_release_builds_scans_kms_signs_and_stores_both_images() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "--platform linux/amd64" in workflow
    assert "--platform linux/arm64" in workflow
    assert "--file infra/agentcore-image/Dockerfile" in workflow
    assert "--build-context project=." in workflow
    assert "agentcore-image-security.json" in workflow
    assert "agentcore-image.cyclonedx.json" in workflow
    assert "--target fargate" in workflow
    assert "--target agentcore" in workflow
    assert ".targets.fargate.digest" in workflow
    assert ".targets.agentcore.digest" in workflow
    identity_step = workflow.split(
        "- name: Read immutable evidence identities",
        maxsplit=1,
    )[1].split("- name: Configure release-signing", maxsplit=1)[0]
    assert "build-metadata.json" not in identity_step
    assert "AXON_RELEASE_SIGNING_KEY_ARN" in workflow
    assert "AXON_RELEASE_SIGN_ROLE_ARN" in workflow
    assert "AXON_RELEASE_PUBLISH_ROLE_ARN" not in workflow
    assert '"kms:Sign"' in workflow
    assert '"kms:Verify"' in workflow
    assert workflow.count("kms_evidence.py sign") == 2
    assert workflow.count("kms_evidence.py verify") == 2
    assert "provenance-kms-signature.json" in workflow
    assert "manifest-kms-signature.json" in workflow
    assert workflow.count("--signing-key-arn") == 3
    assert "--signing-account-id" not in workflow
    assert "actions/attest-build-provenance@" not in workflow
    assert "attestations: write" not in workflow
    signing_statements = _kms_statements(RELEASE_WORKFLOW)
    assert len(signing_statements) == 1
    assert _actions(signing_statements[0]) == {
        "kms:Sign",
        "kms:Verify",
    }
    assert signing_statements[0]["Resource"] == (
        "${{ vars.AXON_RELEASE_SIGNING_KEY_ARN }}"
    )
    assert "Condition" not in signing_statements[0]

    assert "axonllm-release-evidence-${{ github.sha }}" in workflow
    assert "actions/upload-artifact@" in workflow
    assert "--push" not in workflow
    assert "ghcr.io/" not in workflow
    assert "public.ecr.aws" not in workflow
    assert "packages: write" not in workflow


def test_deployment_gate_selects_only_signed_target_identity() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert workflow.count("default: fargate") == 2
    assert "type: choice" in workflow
    assert "- fargate" in workflow
    assert "- agentcore" in workflow
    assert '--target "${DEPLOY_TARGET}"' in workflow
    assert '--run-id "${EVIDENCE_RUN_ID}"' in workflow
    assert '--github-output "${GITHUB_OUTPUT}"' in workflow

    assert "EXPECTED_DIGEST: ${{ steps.evidence.outputs.digest }}" in workflow
    assert "--expected-digest \"${EXPECTED_DIGEST}\"" in workflow
    assert "AXON_RELEASE_SIGNING_KEY_ARN" not in workflow
    assert workflow.count("--signing-account-id") == 1
    assert "--signing-key-arn" not in workflow
    assert (
        "SIGNING_KEY_ARN: "
        "${{ steps.evidence.outputs.signing_key_arn }}"
    ) in workflow
    assert workflow.count("kms_evidence.py verify") == 2
    assert "kms:Sign" not in workflow
    assert "inputs.digest" not in workflow
    _assert_rotation_safe_verify_policy(DEPLOY_WORKFLOW)

    assert "--require-release-tag" in workflow
    assert "git/ref/tags/${release_tag}" in workflow
    assert "git/tags/${tag_sha}" in workflow
    assert "actions/runs/${EVIDENCE_RUN_ID}" in workflow
    assert '.path == ".github/workflows/release-security.yml"' in workflow
    assert '.conclusion == "success"' in workflow
    assert '.event == "push"' in workflow
    assert '[[ "${tag_type}" == "commit" ]]' in workflow
    assert '[[ "${tag_sha}" == "${EXPECTED_COMMIT}" ]]' in workflow
    assert "gh attestation verify" not in workflow
    assert "--verify-remote" in workflow
    assert "VERIFIED_TARGET: ${{ steps.evidence.outputs.target }}" in workflow


def test_publication_preserves_signed_digests_in_private_ecr() -> None:
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert "environment: release" in workflow
    assert "id-token: write" in workflow
    assert "AXON_RELEASE_PUBLISH_ROLE_ARN" in workflow
    assert "AXON_RELEASE_SIGNING_KEY_ARN" not in workflow
    assert "allowed-account-ids: ${{ vars.AXON_AWS_ACCOUNT_ID }}" in workflow
    assert "--require-release-tag" in workflow
    assert workflow.count("--run-id \"${EVIDENCE_RUN_ID}\"") == 2
    assert workflow.count("--signing-account-id") == 2
    assert "--signing-key-arn" not in workflow
    assert "signing_key_arn=$(sed -n" in workflow
    assert (
        "SIGNING_KEY_ARN: "
        "${{ steps.evidence.outputs.signing_key_arn }}"
    ) in workflow
    _assert_rotation_safe_verify_policy(PUBLISH_WORKFLOW)
    assert '[[ "${release_ref}" == "refs/tags/${EXPECTED_TAG}" ]]' in workflow
    assert '.path == ".github/workflows/release-security.yml"' in workflow
    assert '.conclusion == "success"' in workflow
    assert "(.run_attempt | tostring) == $run_attempt" in workflow
    assert '[[ "${tag_sha}" == "${EXPECTED_COMMIT}" ]]' in workflow
    assert "actions/workflows/ci.yml/runs" in workflow
    assert "--from-oci-layout" in workflow
    assert '"${archive}@${digest}"' in workflow
    assert '"${repository}:${RELEASE_TAG}"' in workflow
    assert "--verify-remote" in workflow
    assert workflow.count("kms_evidence.py verify") == 2
    assert "gh attestation verify" not in workflow
    assert "ecr:BatchDeleteImage" not in workflow
    assert "ImageNotFoundException" in workflow
    assert "2>/dev/null || true" not in workflow
    assert "latest" not in workflow
    assert "packages: write" not in workflow


def test_registry_client_is_version_and_checksum_pinned() -> None:
    installer = REGISTRY_INSTALLER.read_text(encoding="utf-8")

    assert REGISTRY_INSTALLER.stat().st_mode & 0o111
    assert 'ORAS_VERSION="1.3.3"' in installer
    assert installer.count('sha256="') == 4
    assert "sha256sum --check" in installer
    assert "shasum -a 256 --check" in installer
    assert "curl --fail --silent --show-error --location" in installer
