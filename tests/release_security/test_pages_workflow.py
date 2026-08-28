from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from scripts.ci import validate_workflows


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "pages.yml"
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def _load(path: Path) -> dict[str, Any]:
    loaded = yaml.load(
        path.read_text(encoding="utf-8"),
        Loader=validate_workflows.WorkflowLoader,
    )
    assert isinstance(loaded, dict)
    return loaded


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_pages_workflow_is_pinned_and_scoped_to_the_canonical_site() -> None:
    workflow = _load(WORKFLOW_PATH)

    assert workflow["on"] == {
        "push": {
            "branches": ["main"],
            "paths": [
                ".github/workflows/pages.yml",
                "scripts/build_public_site.mjs",
                "scripts/test_public_site.mjs",
                "site/**",
                "src/gateway/resources/runtime/site/**",
            ],
        },
        "workflow_dispatch": None,
    }
    assert workflow["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    assert validate_workflows.validate_workflow(WORKFLOW_PATH) == 5

    job = workflow["jobs"]["deploy"]
    assert job["environment"] == {
        "name": "github-pages",
        "url": "${{ steps.deployment.outputs.page_url }}",
    }
    checkout = _step(job, "Check out source")
    assert checkout["uses"] == (
        "actions/checkout@"
        + validate_workflows.APPROVED_ACTION_PINS["actions/checkout"]
    )
    assert checkout["with"]["persist-credentials"] in (False, "false")
    assert _step(job, "Set up Node")["with"]["node-version"] == "22.23.2"
    assert _step(job, "Build canonical public site")["run"] == (
        'node scripts/build_public_site.mjs "${RUNNER_TEMP}/axonllm-public-site"'
    )
    assert _step(
        job,
        "Test pages, animation, audio, and media in Chrome",
    )["run"] == (
        'node scripts/test_public_site.mjs "${RUNNER_TEMP}/axonllm-public-site"'
    )
    assert _step(job, "Upload GitHub Pages artifact")["with"]["path"] == (
        "${{ runner.temp }}/axonllm-public-site"
    )


def test_pull_requests_run_the_same_browser_gate_before_pages_can_deploy() -> None:
    workflow = _load(CI_PATH)
    job = workflow["jobs"]["public-site"]

    assert job["name"] == "Public site (Chrome)"
    assert _step(job, "Set up Node")["with"]["node-version"] == "22.23.2"
    assert _step(job, "Build canonical public site")["run"] == (
        'node scripts/build_public_site.mjs "${RUNNER_TEMP}/axonllm-public-site"'
    )
    assert _step(
        job,
        "Test pages, animation, audio, and media in Chrome",
    )["run"] == (
        'node scripts/test_public_site.mjs "${RUNNER_TEMP}/axonllm-public-site"'
    )


def test_public_builder_filters_deployment_code_and_checks_runtime_parity() -> None:
    source = (ROOT / "scripts" / "build_public_site.mjs").read_text(
        encoding="utf-8"
    )

    assert 'entry.name === "deploy.sh"' in source
    assert 'entry.name === "infra"' in source
    assert "assertPackagedSiteMatches(sourceFiles)" in source
    assert '"src",' in source
    assert '"resources",' in source
    assert '"runtime",' in source
    assert '"site",' in source
    assert 'await writeFile(join(outputRoot, ".nojekyll")' in source
