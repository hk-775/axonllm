"""Regression tests for the release and supply-chain gates."""

from __future__ import annotations

import ast
import datetime as dt
import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci.verify_cdk_asset import (
    AssetVerificationError,
    REQUIRED_PATHS,
    verify_cdk_asset,
)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"


def _workflow_step(job_name: str, step_name: str) -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    steps = workflow["jobs"][job_name]["steps"]
    return next(step for step in steps if step.get("name") == step_name)


def test_codeowners_resolves_after_personal_account_transfer() -> None:
    codeowners = (ROOT / "CODEOWNERS").read_text(encoding="utf-8")

    assert codeowners.count("@hk-775") == 4
    assert "@axonllm/" not in codeowners.lower()


def test_workflow_actions_are_commit_pinned_and_read_only() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    action_refs = re.findall(r"^\s*uses:\s*([^#\s]+)", workflow, flags=re.MULTILINE)

    assert action_refs
    assert all(re.search(r"@[0-9a-f]{40}$", ref) for ref in action_refs)
    assert re.search(r"(?m)^permissions:\n\s+contents: read$", workflow)
    assert 'tags: ["v*"]' in workflow
    assert "pull_request_target:" not in workflow
    assert "continue-on-error:" not in workflow
    assert "uvx " not in workflow


def test_workflow_enforces_each_release_gate() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    required_commands = (
        "python -m py_compile",
        "ruff check",
        "pytest tests",
        "git diff --check",
        "gitleaks git",
        "audit_python_dependencies.sh",
        "build_serverless_control_artifacts.sh",
        "trivy config",
        "synthesize_and_verify_cdk.sh",
        "docker build",
        "verify_image.py",
        "trivy image",
    )

    missing = [command for command in required_commands if command not in workflow]
    assert missing == []
    assert "--verify-startup" in workflow
    verifier = (ROOT / "scripts/ci/verify_image.py").read_text(
        encoding="utf-8"
    )
    assert "_verify_two_replica_startup(image)" in verifier
    assert '"docker", "network", "create", "--internal"' in verifier
    assert "--extra agentcore" in workflow
    assert '"${CDK_CI_OUTDIR}/fargate/AxonLLMStack.template.json"' in workflow
    assert '"${CDK_CI_OUTDIR}/agentcore/AxonLLMAgentCoreStack.template.json"' in workflow
    assert '"${CDK_CI_OUTDIR}/application-state/AxonLLMApplicationStateStack.template.json"' in workflow
    assert '"${CDK_CI_OUTDIR}/managed-network/AxonLLMManagedNetworkStack.template.json"' in workflow
    assert '"${CDK_CI_OUTDIR}/managed-network-parked/AxonLLMManagedNetworkStack.template.json"' in workflow
    assert '"${CDK_CI_OUTDIR}/agentcore-parked/AxonLLMAgentCoreStack.template.json"' in workflow
    assert '"${CDK_CI_OUTDIR}/serverless-control-plane/AxonLLMServerlessControlPlaneStack.template.json"' in workflow
    assert '"${CDK_CI_OUTDIR}/control-plane-edge/AxonLLMControlPlaneStack.template.json"' in workflow
    assert (
        '"${CDK_CI_OUTDIR}/serverless-control-plane-edge/AxonLLMServerlessControlPlaneStack.template.json"' in workflow
    )
    assert '"${CDK_CI_OUTDIR}/serverless-workers/AxonLLMServerlessWorkersStack.template.json"' in workflow
    assert '"${CDK_CI_OUTDIR}/release-foundation/AxonLLMReleaseFoundationStack.template.json"' in workflow
    assert '"${CDK_CI_OUTDIR}/launch-workers/AxonLLMLaunchWorkersStack-managed.template.json"' in workflow
    synthesis = (ROOT / "scripts/ci/synthesize_and_verify_cdk.sh").read_text(encoding="utf-8")
    assert 'verify_target "application-state" "AxonLLMApplicationStateStack"' in synthesis
    assert 'verify_target "managed-network" "AxonLLMManagedNetworkStack"' in synthesis
    assert 'verify_target "managed-network-parked" "AxonLLMManagedNetworkStack"' in synthesis
    assert 'verify_target "agentcore-parked" "AxonLLMAgentCoreStack"' in synthesis
    assert 'verify_target "serverless-control-plane" "AxonLLMServerlessControlPlaneStack"' in synthesis
    assert '"control-plane-edge"' in synthesis
    assert '"edge_cutover_enabled":true' in synthesis
    assert '"serverless-control-plane-edge"' in synthesis
    assert '"edge_attachment_enabled":true' in synthesis
    assert 'verify_target "serverless-workers" "AxonLLMServerlessWorkersStack"' in synthesis
    assert 'verify_target "launch-workers" "AxonLLMLaunchWorkersStack-managed" "managed"' in synthesis


def test_ci_compiles_production_and_release_python_syntax() -> None:
    step = _workflow_step("supply-chain", "Compile production and release Python")

    assert step["env"] == {"PYTHONPYCACHEPREFIX": "${{ runner.temp }}/python-syntax-cache"}
    command = step["run"]
    assert 'python -m py_compile "${source}"' in command
    assert "git ls-files -z -- '*.py' ':!tests/**'" in command
    assert "compileall" not in command


def test_supply_chain_installs_and_runs_release_security_tests() -> None:
    install = _workflow_step("supply-chain", "Install locked audit dependencies")
    validate = _workflow_step(
        "supply-chain",
        "Validate workflow and operational tooling",
    )

    assert install["run"] == "uv sync --frozen --extra security --extra dev"
    assert "uv run --frozen --no-sync pytest tests/release_security -q --tb=short" in validate["run"]
    assert "unittest discover" not in validate["run"]


def test_ci_validates_every_tracked_shell_script() -> None:
    step = _workflow_step("supply-chain", "Validate tracked shell syntax")
    command = step["run"]

    assert 'bash -n "${script}"' in command
    assert "git ls-files -z -- '*.sh'" in command
    assert "find scripts" not in command
    for path in (
        "deploy-agentcore.sh",
        "deploy-fargate.sh",
        "deploy.sh",
        "site/deploy.sh",
    ):
        assert (ROOT / path).is_file()


@pytest.mark.parametrize(
    "installer",
    (
        "scripts/ci/install_registry_tools.sh",
        "scripts/ci/install_security_tools.sh",
    ),
)
def test_ci_tool_downloads_tolerate_transient_release_outages(
    installer: str,
) -> None:
    script = (ROOT / installer).read_text(encoding="utf-8")

    assert "--connect-timeout 15" in script
    assert "--max-time 180" in script
    assert "--retry 8 --retry-all-errors --retry-max-time 120" in script
    assert "sha256sum --check" in script
    assert "shasum -a 256 --check" in script


def test_ci_checks_worktree_and_event_patch_whitespace() -> None:
    step = _workflow_step("supply-chain", "Check patch whitespace")
    command = step["run"]

    assert "\ngit diff --check\n" in f"\n{command}"
    assert 'git diff --check "${PR_BASE_SHA}...${PR_HEAD_SHA}"' in command
    assert 'git diff --check "${BEFORE_SHA}..${GITHUB_SHA}"' in command
    assert "git diff-tree --check --root --no-commit-id" in command


def test_ci_scans_agentcore_dockerfile_configuration() -> None:
    step = _workflow_step(
        "supply-chain",
        "Scan AgentCore Dockerfile configuration",
    )

    assert step["run"] == (
        "trivy config "
        "--disable-telemetry "
        "--skip-check-update "
        "--ignorefile .github/trivyignore.yaml "
        "--severity HIGH,CRITICAL "
        "--exit-code 1 "
        "infra/agentcore-image/Dockerfile"
    )


def test_ci_scans_standalone_arm64_dockerfile_configuration() -> None:
    step = _workflow_step(
        "supply-chain",
        "Scan standalone ARM64 Dockerfile configuration",
    )

    assert step["run"] == (
        "trivy config "
        "--disable-telemetry "
        "--skip-check-update "
        "--ignorefile .github/trivyignore.yaml "
        "--severity HIGH,CRITICAL "
        "--exit-code 1 "
        "infra/standalone-image/Dockerfile"
    )


def test_ci_scans_serverless_artifact_dockerfile_configuration() -> None:
    step = _workflow_step(
        "supply-chain",
        "Scan serverless artifact Dockerfile configuration",
    )

    assert step["run"] == (
        "trivy config "
        "--disable-telemetry "
        "--skip-check-update "
        "--ignorefile .github/trivyignore.yaml "
        "--severity HIGH,CRITICAL "
        "--exit-code 1 "
        "infra/serverless-control-artifacts/Dockerfile"
    )


def test_ci_builds_and_scans_arm64_serverless_artifacts() -> None:
    build = _workflow_step(
        "container",
        "Build and verify ARM64 serverless control artifacts",
    )
    scan = _workflow_step(
        "container",
        "Scan serverless control artifacts",
    )

    assert "build_serverless_control_artifacts.sh" in build["run"]
    assert '"${GITHUB_SHA}"' in build["run"]
    assert "serverless-control-artifacts.json" in scan["run"]
    assert ".artifacts.controlApi.fileName" in scan["run"]
    assert ".artifacts.staticAssets.fileName" in scan["run"]
    assert scan["run"].count("trivy fs") == 2
    assert "--scanners vuln,secret" in scan["run"]


def test_ci_builds_verifies_and_scans_standalone_arm64_image() -> None:
    build = _workflow_step("container", "Build standalone ARM64 image")
    verify = _workflow_step("container", "Verify standalone ARM64 image")
    scan = _workflow_step("container", "Scan standalone ARM64 image")

    assert "--platform linux/arm64" in build["run"]
    assert "--file infra/standalone-image/Dockerfile" in build["run"]
    assert "--build-context project=." in build["run"]
    assert "--load" in build["run"]
    assert "--platform linux/arm64" in verify["run"]
    assert "scripts/ci/verify_image.py" in verify["run"]
    assert "--platform linux/arm64" in scan["run"]
    assert "--scanners vuln,secret" in scan["run"]


def test_secret_baseline_contains_only_verified_fingerprints() -> None:
    lines = [
        line for line in (ROOT / ".github/gitleaksignore").read_text().splitlines() if line and not line.startswith("#")
    ]
    fingerprint = re.compile(r"^[0-9a-f]{40}:README\.md:curl-auth-header:[0-9]+$")

    assert len(lines) == 4
    assert all(fingerprint.fullmatch(line) for line in lines)


def test_trivy_exception_is_narrow_and_expires() -> None:
    ignore = yaml.safe_load((ROOT / ".github/trivyignore.yaml").read_text(encoding="utf-8"))

    assert ignore == {
        "misconfigurations": [
            {
                "id": "AVD-DS-0031",
                "paths": ["Dockerfile"],
                "statement": "AXON_AUTH_MODE is a non-secret enforcement setting.",
                "expired_at": "2027-08-07T00:00:00Z",
            },
            {
                "id": "AWS-0013",
                "paths": [
                    "AxonLLMControlPlaneStack.template.json",
                    "AxonLLMServerlessControlPlaneStack.template.json",
                ],
                "statement": (
                    "The AWS-managed cloudfront.net certificate does not "
                    "permit a custom minimum viewer TLS policy; strict TLS "
                    "1.2 enforcement requires custom-domain mode."
                ),
                "expired_at": "2027-08-13T00:00:00Z",
            },
            {
                "id": "AWS-0132",
                "paths": [
                    "AxonLLMStack.template.json",
                    "AxonLLMControlPlaneStack.template.json",
                ],
                "statement": (
                    "ALB and CloudFront access-log delivery require SSE-S3; "
                    "the bucket cannot use a customer-managed KMS key."
                ),
                "expired_at": "2027-08-07T00:00:00Z",
            },
            {
                "id": "AWS-0096",
                "paths": [
                    "AxonLLMServerlessWorkersStack.template.json",
                ],
                "statement": (
                    "The export queues set KmsMasterKeyId to the "
                    "parameterized customer-managed "
                    "ApplicationStateDataKeyArn; Trivy cannot resolve the "
                    "CloudFormation parameter."
                ),
                "expired_at": "2027-08-17T00:00:00Z",
            },
            {
                "id": "AWS-0132",
                "paths": [
                    "AxonLLMServerlessWorkersStack.template.json",
                ],
                "statement": (
                    "The export bucket sets KMSMasterKeyID to the "
                    "parameterized customer-managed "
                    "ApplicationStateDataKeyArn; Trivy cannot resolve the "
                    "CloudFormation parameter."
                ),
                "expired_at": "2027-08-17T00:00:00Z",
            },
            {
                "id": "AWS-0035",
                "paths": [
                    "AxonLLMStack.template.json",
                    "AxonLLMControlPlaneStack.template.json",
                    "AxonLLMLaunchWorkersStack-managed.template.json",
                ],
                "statement": (
                    "The task volume is ephemeral Fargate scratch storage "
                    "mounted at /tmp; no EFSVolumeConfiguration exists, so "
                    "EFS transit encryption does not apply."
                ),
                "expired_at": "2027-08-07T00:00:00Z",
            },
            {
                "id": "AWS-0036",
                "paths": ["AxonLLMLaunchWorkersStack-managed.template.json"],
                "statement": (
                    "The environment value is an exact Secrets Manager ARN "
                    "used to fetch credentials at runtime; secret material is "
                    "never placed in the task environment."
                ),
                "expired_at": "2027-08-12T00:00:00Z",
            },
            {
                "id": "AWS-0053",
                "paths": ["AxonLLMControlPlaneStack.template.json"],
                "statement": (
                    "The public control-plane ALB accepts HTTPS only from an "
                    "approved managed prefix list and requires Cognito "
                    "authentication."
                ),
                "expired_at": "2027-08-11T00:00:00Z",
            },
        ]
    }


def test_pip_audit_exception_is_narrow_and_current() -> None:
    exception_file = ROOT / ".github/pip-audit-ignore.txt"
    lines = exception_file.read_text(encoding="utf-8").splitlines()
    vulnerability_ids = [line for line in lines if line and not line.startswith("#")]
    expiry_line = next(line for line in lines if line.startswith("# expires: "))
    expiry = dt.date.fromisoformat(expiry_line.removeprefix("# expires: "))

    assert vulnerability_ids == ["PYSEC-2026-1325"]
    assert expiry > dt.date.today()


def test_ecdsa_exception_remains_verification_only() -> None:
    runtime_paths = [
        ROOT / "agentcore_agent.py",
        *(path for path in (ROOT / "src").rglob("*.py") if "node_modules" not in path.parts),
    ]
    prohibited: list[str] = []

    for path in runtime_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        jose_jwt_aliases: set[str] = set()
        jose_encode_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = node.module if isinstance(node, ast.ImportFrom) else ""
                imported = [alias.name if not module else f"{module}.{alias.name}" for alias in node.names]
                if any(name == "ecdsa" or name.startswith("ecdsa.") for name in imported):
                    prohibited.append(f"{path}: imports python-ecdsa")
                if isinstance(node, ast.ImportFrom) and node.module == "jose":
                    jose_jwt_aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "jwt")
                if isinstance(node, ast.ImportFrom) and node.module == "jose.jwt":
                    jose_encode_aliases.update(
                        alias.asname or alias.name for alias in node.names if alias.name == "encode"
                    )
            elif isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "encode"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in jose_jwt_aliases
                ):
                    prohibited.append(f"{path}: signs with jose.jwt.encode")
                if isinstance(node.func, ast.Name) and node.func.id in jose_encode_aliases:
                    prohibited.append(f"{path}: signs with jose.jwt.encode")

    assert prohibited == []


def _make_cdk_output(tmp_path: Path) -> Path:
    out_dir = tmp_path / "cdk.out"
    asset_dir = out_dir / "asset.test"
    asset_dir.mkdir(parents=True)
    for relative in REQUIRED_PATHS:
        path = asset_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "artifacts": {
                    "assets": {
                        "type": "cdk:asset-manifest",
                        "properties": {"file": "assets.json"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "assets.json").write_text(
        json.dumps({"dockerImages": {"image": {"source": {"directory": asset_dir.name}}}}),
        encoding="utf-8",
    )
    return out_dir


def test_cdk_asset_verifier_accepts_minimal_safe_context(tmp_path: Path) -> None:
    out_dir = _make_cdk_output(tmp_path)

    assert verify_cdk_asset(out_dir) == len(REQUIRED_PATHS)


@pytest.mark.parametrize(
    "relative_path",
    [
        ".env",
        ".env.production",
        ".bedrock_agentcore.yaml",
        ".aws/credentials",
        ".git/config",
        ".venv/bin/python",
        "config/providers.yaml",
        "certs/client.pem",
        "tests/test_private.py",
    ],
)
def test_cdk_asset_verifier_rejects_sensitive_paths(tmp_path: Path, relative_path: str) -> None:
    out_dir = _make_cdk_output(tmp_path)
    prohibited = out_dir / "asset.test" / relative_path
    prohibited.parent.mkdir(parents=True, exist_ok=True)
    prohibited.write_text("CI canary, not a credential", encoding="utf-8")

    with pytest.raises(AssetVerificationError, match="prohibited paths"):
        verify_cdk_asset(out_dir)


def test_deployment_requirements_are_hash_locked() -> None:
    for relative_path in ("requirements.txt", "infra/requirements.txt"):
        requirements = (ROOT / relative_path).read_text(encoding="utf-8")
        package_lines = [line for line in requirements.splitlines() if line and not line.startswith((" ", "#", "-"))]

        assert package_lines
        assert all("==" in line for line in package_lines)
        assert "--hash=sha256:" in requirements


def test_isolated_build_backend_is_exactly_pinned() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build_requirements = pyproject["build-system"]["requires"]

    assert build_requirements
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^=<>!~]+", item) for item in build_requirements)


def test_oidc_runtime_extra_contains_its_network_client() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    oidc_dependencies = pyproject["project"]["optional-dependencies"]["oidc"]

    assert any(dependency.startswith("httpx") for dependency in oidc_dependencies)
    assert any(dependency.startswith("python-jose") for dependency in oidc_dependencies)


def test_embedded_install_excludes_host_and_aws_dependencies() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    base_dependencies = project["dependencies"]
    extras = project["optional-dependencies"]

    excluded = (
        "boto3",
        "google-auth",
        "sqlglot",
        "starlette",
        "uvicorn",
    )
    assert not any(dependency.startswith(excluded) for dependency in base_dependencies)

    expected = {
        "bedrock": ("boto3",),
        "google": ("google-auth",),
        "server": (
            "boto3",
            "google-auth",
            "sqlglot",
            "starlette",
            "uvicorn",
        ),
        "aws-control": ("boto3", "sqlglot", "starlette"),
        "agentcore": (
            "bedrock-agentcore",
            "boto3",
            "google-auth",
            "sqlglot",
            "starlette",
        ),
    }
    for extra, required in expected.items():
        dependencies = extras[extra]
        for package in required:
            assert any(dependency.startswith(package) for dependency in dependencies), f"{extra} is missing {package}"
