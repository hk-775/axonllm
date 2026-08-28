#!/usr/bin/env python3
"""Enforce the repository's GitHub Actions supply-chain policy."""

from __future__ import annotations

import argparse
import copy
import re
import sys
from pathlib import Path
from typing import Any

import yaml


PINNED_ACTION = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}$"
)
VALID_PERMISSION = {"read", "write", "none"}
ALLOWED_WRITE_PERMISSIONS = {"id-token", "pages", "security-events"}
APPROVED_ACTION_PINS = {
    "actions/checkout": "fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09",
    "actions/configure-pages": "45bfe0192ca1faeb007ade9deae92b16b8254a0d",
    "actions/deploy-pages": "cd2ce8fcbc39b97be8ca5fce6e763baed58fa128",
    "actions/download-artifact": "37930b1c2abaa49bbe596cd826c3c89aef350131",
    "actions/setup-node": "a0853c24544627f65ddf259abe73b1d18a591444",
    "actions/setup-python": "ece7cb06caefa5fff74198d8649806c4678c61a1",
    "actions/upload-artifact": "b7c566a772e6b6bfb58ed0dc250532a479d7789f",
    "actions/upload-pages-artifact": "fc324d3547104276b827a68afc52ff2a11cc49c9",
    "aws-actions/configure-aws-credentials": (
        "e6de054238d6b7531b4efff3b6587d9aade6a06c"
    ),
    "docker/setup-buildx-action": "bb05f3f5519dd87d3ba754cc423b652a5edd6d2c",
    "docker/setup-qemu-action": "96fe6ef7f33517b61c61be40b68a1882f3264fb8",
    "pypa/gh-action-pypi-publish": "dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
}


class WorkflowPolicyError(RuntimeError):
    """Raised when a workflow violates repository policy."""


class WorkflowLoader(yaml.SafeLoader):
    """YAML loader that does not reinterpret the workflow key `on` as boolean."""


WorkflowLoader.yaml_implicit_resolvers = copy.deepcopy(
    yaml.SafeLoader.yaml_implicit_resolvers
)
for first_character, resolvers in list(WorkflowLoader.yaml_implicit_resolvers.items()):
    WorkflowLoader.yaml_implicit_resolvers[first_character] = [
        resolver
        for resolver in resolvers
        if resolver[0] != "tag:yaml.org,2002:bool"
    ]


def _permissions(value: Any, location: str) -> None:
    if not isinstance(value, dict) or not value:
        raise WorkflowPolicyError(f"{location}: permissions must be a non-empty map")
    for name, access in value.items():
        if access not in VALID_PERMISSION:
            raise WorkflowPolicyError(
                f"{location}: invalid permission {name!r}: {access!r}"
            )
        if access == "write" and name not in ALLOWED_WRITE_PERMISSIONS:
            raise WorkflowPolicyError(
                f"{location}: write permission is not allowlisted: {name}"
            )


def _action(action: Any, location: str, settings: Any = None) -> None:
    if not isinstance(action, str):
        raise WorkflowPolicyError(f"{location}: uses must be a string")
    if action.startswith("./"):
        return
    if not PINNED_ACTION.fullmatch(action):
        raise WorkflowPolicyError(
            f"{location}: external action is not pinned to a full commit SHA: {action}"
        )
    action_name, commit = action.rsplit("@", maxsplit=1)
    approved_commit = APPROVED_ACTION_PINS.get(action_name)
    if approved_commit is None:
        raise WorkflowPolicyError(
            f"{location}: external action is not approved: {action_name}"
        )
    if commit != approved_commit:
        raise WorkflowPolicyError(
            f"{location}: external action pin is not approved: {action}"
        )
    if action.startswith("actions/checkout@") and (
        not isinstance(settings, dict)
        or settings.get("persist-credentials") not in (False, "false")
    ):
        raise WorkflowPolicyError(
            f"{location}: checkout must set persist-credentials: false"
        )


def validate_workflow(path: Path) -> int:
    try:
        workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=WorkflowLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise WorkflowPolicyError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(workflow, dict):
        raise WorkflowPolicyError(f"{path}: workflow root must be a map")
    triggers = workflow.get("on")
    if isinstance(triggers, dict) and "pull_request_target" in triggers:
        raise WorkflowPolicyError(f"{path}: pull_request_target is prohibited")

    _permissions(workflow.get("permissions"), f"{path}: top level")
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        raise WorkflowPolicyError(f"{path}: jobs must be a non-empty map")

    action_count = 0
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            raise WorkflowPolicyError(f"{path}: job {job_name!r} must be a map")
        if "permissions" in job:
            _permissions(job["permissions"], f"{path}: job {job_name}")
        if "uses" in job:
            action_count += 1
            _action(
                job["uses"],
                f"{path}: reusable job {job_name}",
                job.get("with"),
            )
        steps = job.get("steps", [])
        if not isinstance(steps, list):
            raise WorkflowPolicyError(f"{path}: job {job_name!r} steps must be a list")
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict) or "uses" not in step:
                continue
            action_count += 1
            location = f"{path}: job {job_name} step {index}"
            _action(step["uses"], location, step.get("with"))
    return action_count


def validate_directory(workflow_dir: Path) -> tuple[int, int]:
    paths = sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")))
    if not paths:
        raise WorkflowPolicyError(f"{workflow_dir}: no workflow files found")
    return len(paths), sum(validate_workflow(path) for path in paths)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "workflow_dir",
        nargs="?",
        type=Path,
        default=Path(".github/workflows"),
    )
    args = parser.parse_args()
    try:
        workflow_count, action_count = validate_directory(args.workflow_dir)
    except WorkflowPolicyError as exc:
        print(f"workflow policy failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"workflow policy verified: {workflow_count} workflows, "
        f"{action_count} pinned action uses"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
