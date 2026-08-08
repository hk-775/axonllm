from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = REPO_ROOT / "deploy-fargate.sh"
SYNTHESIS_SCRIPT = REPO_ROOT / "scripts" / "ci" / "synthesize_and_verify_cdk.sh"
ROOT_DOCKERFILE = REPO_ROOT / "Dockerfile"
AGENTCORE_DOCKERFILE = REPO_ROOT / "infra" / "agentcore-image" / "Dockerfile"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-security.yml"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-verification.yml"


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


def test_release_builds_scans_attests_and_privately_stores_both_images() -> None:
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
    )[1].split("- name: Keylessly attest Fargate", maxsplit=1)[0]
    assert "build-metadata.json" not in identity_step
    assert "subject-name: axonllm-agentcore-linux-arm64" in workflow
    assert workflow.count("push-to-registry: false") == 3
    assert "agentcore-image-provenance.sigstore.jsonl" in workflow
    assert "image-provenance.sigstore.jsonl" in workflow

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
    assert (
        "IMAGE_BUNDLE: ${{ runner.temp }}/release-evidence/"
        "${{ steps.evidence.outputs.bundle }}"
    ) in workflow
    assert "--expected-digest \"${EXPECTED_DIGEST}\"" in workflow
    assert '--bundle "${IMAGE_BUNDLE}"' in workflow
    assert "inputs.bundle" not in workflow
    assert "inputs.digest" not in workflow

    assert "--require-release-tag" in workflow
    assert "git/ref/tags/${release_tag}" in workflow
    assert "git/tags/${tag_sha}" in workflow
    assert "actions/runs/${EVIDENCE_RUN_ID}" in workflow
    assert '.path == ".github/workflows/release-security.yml"' in workflow
    assert '.conclusion == "success"' in workflow
    assert '[[ "${tag_type}" == "commit" ]]' in workflow
    assert '[[ "${tag_sha}" == "${EXPECTED_COMMIT}" ]]' in workflow
    assert "--signer-workflow" in workflow
    assert '--signer-digest "${EXPECTED_COMMIT}"' in workflow
    assert '--source-digest "${EXPECTED_COMMIT}"' in workflow
    assert '--source-ref "${RELEASE_REF}"' in workflow
    assert "--verify-remote" in workflow
    assert "VERIFIED_TARGET: ${{ steps.evidence.outputs.target }}" in workflow
