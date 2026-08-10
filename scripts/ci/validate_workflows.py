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
ALLOWED_WRITE_PERMISSIONS = {"id-token", "security-events"}


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
