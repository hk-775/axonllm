"""Regression tests for the release and supply-chain gates."""

from __future__ import annotations

import ast
import datetime as dt
import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

from scripts.ci.verify_cdk_asset import (
    AssetVerificationError,
    REQUIRED_PATHS,
    verify_cdk_asset,
)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"


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
        "ruff check",
        "pytest tests",
        "gitleaks git",
        "audit_python_dependencies.sh",
        "trivy config",
        "synthesize_and_verify_cdk.sh",
        "docker build",
        "verify_image.py",
        "trivy image",
    )

    missing = [command for command in required_commands if command not in workflow]
    assert missing == []
    assert "--extra agentcore" in workflow


def test_secret_baseline_contains_only_verified_fingerprints() -> None:
    lines = [
        line
        for line in (ROOT / ".github/gitleaksignore").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    fingerprint = re.compile(
        r"^[0-9a-f]{40}:README\.md:curl-auth-header:[0-9]+$"
    )

    assert len(lines) == 4
    assert all(fingerprint.fullmatch(line) for line in lines)


def test_trivy_exception_is_narrow_and_expires() -> None:
    ignore = yaml.safe_load(
        (ROOT / ".github/trivyignore.yaml").read_text(encoding="utf-8")
    )

    assert ignore == {
        "misconfigurations": [
            {
                "id": "AVD-DS-0031",
                "paths": ["Dockerfile"],
                "statement": "AXON_AUTH_MODE is a non-secret enforcement setting.",
                "expired_at": "2027-08-07T00:00:00Z",
            },
            {
                "id": "AWS-0132",
                "paths": ["AxonLLMStack.template.json"],
                "statement": (
                    "ALB and CloudFront access-log delivery require SSE-S3; "
                    "the bucket cannot use a customer-managed KMS key."
                ),
                "expired_at": "2027-08-07T00:00:00Z",
            },
        ]
    }


def test_pip_audit_exception_is_narrow_and_current() -> None:
    exception_file = ROOT / ".github/pip-audit-ignore.txt"
    lines = exception_file.read_text(encoding="utf-8").splitlines()
    vulnerability_ids = [
        line for line in lines if line and not line.startswith("#")
    ]
    expiry_line = next(line for line in lines if line.startswith("# expires: "))
    expiry = dt.date.fromisoformat(expiry_line.removeprefix("# expires: "))

    assert vulnerability_ids == ["PYSEC-2026-1325"]
    assert expiry > dt.date.today()


def test_ecdsa_exception_remains_verification_only() -> None:
    runtime_paths = [ROOT / "agentcore_agent.py", *(ROOT / "src").rglob("*.py")]
    prohibited: list[str] = []

    for path in runtime_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        jose_jwt_aliases: set[str] = set()
        jose_encode_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = node.module if isinstance(node, ast.ImportFrom) else ""
                imported = [
                    alias.name if not module else f"{module}.{alias.name}"
                    for alias in node.names
                ]
                if any(name == "ecdsa" or name.startswith("ecdsa.") for name in imported):
                    prohibited.append(f"{path}: imports python-ecdsa")
                if isinstance(node, ast.ImportFrom) and node.module == "jose":
                    jose_jwt_aliases.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name == "jwt"
                    )
                if isinstance(node, ast.ImportFrom) and node.module == "jose.jwt":
                    jose_encode_aliases.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name == "encode"
                    )
            elif isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "encode"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in jose_jwt_aliases
                ):
                    prohibited.append(f"{path}: signs with jose.jwt.encode")
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id in jose_encode_aliases
                ):
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
        json.dumps(
            {
                "dockerImages": {
                    "image": {"source": {"directory": asset_dir.name}}
                }
            }
        ),
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
def test_cdk_asset_verifier_rejects_sensitive_paths(
    tmp_path: Path, relative_path: str
) -> None:
    out_dir = _make_cdk_output(tmp_path)
    prohibited = out_dir / "asset.test" / relative_path
    prohibited.parent.mkdir(parents=True, exist_ok=True)
    prohibited.write_text("CI canary, not a credential", encoding="utf-8")

    with pytest.raises(AssetVerificationError, match="prohibited paths"):
        verify_cdk_asset(out_dir)


def test_deployment_requirements_are_hash_locked() -> None:
    for relative_path in ("requirements.txt", "infra/requirements.txt"):
        requirements = (ROOT / relative_path).read_text(encoding="utf-8")
        package_lines = [
            line
            for line in requirements.splitlines()
            if line and not line.startswith((" ", "#", "-"))
        ]

        assert package_lines
        assert all("==" in line for line in package_lines)
        assert "--hash=sha256:" in requirements


def test_isolated_build_backend_is_exactly_pinned() -> None:
    pyproject = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    build_requirements = pyproject["build-system"]["requires"]

    assert build_requirements
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^=<>!~]+", item) for item in build_requirements)


def test_oidc_runtime_extra_contains_its_network_client() -> None:
    pyproject = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    oidc_dependencies = pyproject["project"]["optional-dependencies"]["oidc"]

    assert any(dependency.startswith("httpx") for dependency in oidc_dependencies)
    assert any(
        dependency.startswith("python-jose")
        for dependency in oidc_dependencies
    )
