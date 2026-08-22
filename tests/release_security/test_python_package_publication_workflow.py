from __future__ import annotations

import json
from pathlib import Path
import sys
import tomllib
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))

import validate_workflows  # noqa: E402


WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-python-package.yml"


def _load() -> dict[str, Any]:
    loaded = yaml.load(
        WORKFLOW.read_text(encoding="utf-8"),
        Loader=validate_workflows.WorkflowLoader,
    )
    assert isinstance(loaded, dict)
    return loaded


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_release_version_is_consistent() -> None:
    project = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    package = json.loads(
        (REPO_ROOT / "src/gateway/deployment/infra/package.json").read_text(
            encoding="utf-8"
        )
    )
    package_lock = json.loads(
        (REPO_ROOT / "src/gateway/deployment/infra/package-lock.json").read_text(
            encoding="utf-8"
        )
    )

    version = project["project"]["version"]
    locked_project = next(
        item for item in lock["package"] if item["name"] == "axon-llm"
    )
    assert locked_project["version"] == version
    assert package["version"] == version
    assert package_lock["version"] == version
    assert package_lock["packages"][""]["version"] == version
    assert f"## [{version}]" in (
        REPO_ROOT / "CHANGELOG.md"
    ).read_text(encoding="utf-8")


def test_python_package_workflow_is_pinned_and_tokenless() -> None:
    workflow = _load()

    assert validate_workflows.validate_workflow(WORKFLOW) == 5
    assert workflow["on"]["release"]["types"] == ["published"]
    assert workflow["on"]["workflow_dispatch"]["inputs"]["release_tag"]["required"] == "true"
    assert workflow["permissions"] == {
        "actions": "read",
        "contents": "read",
    }

    build = workflow["jobs"]["build"]
    checkout = _step(build, "Check out exact release source")
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["persist-credentials"] == "false"
    assert "release_tag" in checkout["with"]["ref"]

    assert "github.ref == 'refs/heads/main'" in build["if"]
    lineage = _step(build, "Verify immutable release lineage")["run"]
    assert "git cat-file -t" in lineage
    assert "git merge-base --is-ancestor" in lineage
    assert "releases/tags/${RELEASE_TAG}" in lineage
    assert ".prerelease == false" in lineage
    assert "actions/workflows/ci.yml/runs" in lineage
    assert "actions/workflows/release-security.yml/runs" in lineage
    assert '.event == "push"' in lineage
    assert '.conclusion == "success"' in lineage

    package_build = _step(build, "Build wheel and source distribution")["run"]
    assert package_build == "uv build --wheel --sdist --out-dir dist"
    upload = _step(build, "Upload verified distributions")["with"]
    assert upload["retention-days"] == 1

    publish = workflow["jobs"]["publish"]
    assert publish["environment"]["name"] == "pypi"
    assert publish["permissions"] == {
        "actions": "read",
        "id-token": "write",
    }
    publisher = _step(
        publish,
        "Publish distributions with Trusted Publishing",
    )
    assert publisher["uses"] == (
        "pypa/gh-action-pypi-publish@"
        "dc37677b2e1c63e2034f94d8a5b11f265b73ba33"
    )
    assert publisher["with"] == {
        "attestations": "true",
        "packages-dir": "dist/",
        "print-hash": "true",
        "skip-existing": "false",
    }
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")
